"""One daily run: search, store, enrich links, build the digest, mail it, notify n8n."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import mailer
from .classify import classify, section_of
from .clinicaltrials import new_trials
from .config import Config
from .downloads import organize_all, organize_file
from .http import HttpClient
from .openaccess import (AUTO_DOWNLOAD_SOURCES, PMC_MAIN_PDF, core_links, elsevier_links, europepmc_links,
                         openalex_links, pmc_s3_links, preprint_links, unpaywall_links, unpaywall_provenance, wiley_links)
from .pubmed import PubMed
from .report import Digest, build
from .storage import Storage

log = logging.getLogger(__name__)

MAX_CATCH_UP_DAYS = 60


@dataclass
class RunResult:
    run_id: int
    baseline: bool
    window: tuple[date, date]
    new_articles: int
    new_trials: int
    mail_sent: bool
    digest: Digest


def make_http(cfg: Config) -> HttpClient:
    ncbi_delay = 0.12 if cfg.sources.ncbi_api_key else 0.4  # NCBI: 10 req/s with a key, 3 without
    return HttpClient(cfg.sources.user_agent, cfg.sources.timeout,
                      {"eutils.ncbi.nlm.nih.gov": ncbi_delay, "www.ebi.ac.uk": 0.3, "api.unpaywall.org": 0.2,
                       "clinicaltrials.gov": 0.5, "pmc-oa-opendata.s3.amazonaws.com": 0.1,
                       "api.openalex.org": 0.2, "content.openalex.org": 0.5, "api.elsevier.com": 0.3,
                       "api.wiley.com": 10.5,  # Wiley TDM allows 60 requests per 10 minutes
                       "api.core.ac.uk": 6.0, "www.medrxiv.org": 1.0, "www.biorxiv.org": 1.0, "api.biorxiv.org": 1.0})


def search_window(storage: Storage, cfg: Config, today: date) -> tuple[date, date, bool]:
    last = storage.last_ok_run()
    if last is None:
        return today - timedelta(days=cfg.baseline_days), today, True
    last_to = min(date.fromisoformat(last["window_to"]), today)
    since = max(last_to - timedelta(days=cfg.lookback_days), today - timedelta(days=MAX_CATCH_UP_DAYS))
    return since, today, False


def enrich_links(storage: Storage, http: HttpClient, cfg: Config, today: date, force: bool = False) -> int:
    """Open-access full text / PDF links, with their provenance, for recent articles whose PDF can still improve.

    Each step only looks at the articles still without a PDF a script may fetch: Europe PMC (links, licence,
    preprint), PMC S3, Unpaywall, the Elsevier and Wiley TDM APIs, CORE, the OpenAlex PDF cache (it spends the
    OpenAlex budget, so it comes last); finally the preprint of whatever is still missing. Sources without a key
    are skipped."""
    since = (today - timedelta(days=cfg.oa_recheck_days)).isoformat()
    pending = storage.pending_oa(since, today.isoformat(), force)
    # once (forced refresh): PDFs found before licence / OA status were recorded get their provenance filled in
    backfill = storage.missing_provenance(since) if force else []
    if not pending and not backfill:
        return 0
    src = cfg.sources
    dois = {p: doi for p, doi, _, _ in pending if doi}
    open_ = {p for p, *_ in pending}  # still without a PDF a script may fetch
    gained: Counter = Counter()
    changed = 0

    def apply(found: dict, label: str) -> None:
        nonlocal changed
        for pmid, links in found.items():
            changed += storage.update_oa(pmid, links)
            if links.pdf_source in AUTO_DOWNLOAD_SOURCES and pmid in open_:
                open_.discard(pmid)
                gained[label] += 1
        # commit after every source: the next one may take minutes on the network, and an open write transaction
        # would lock the API out (n8n's download reports could not be recorded)
        storage.commit()

    def still_open_dois() -> dict[str, str]:
        return {p: dois[p] for p in sorted(open_) if p in dois}

    epmc = europepmc_links(http, [p for p, *_ in pending] + [p for p, *_ in backfill])
    apply({p: links for p, links in epmc.items() if p in open_}, "europepmc")
    s3_rows = {p: c for p, _, c, _, source in backfill if source == "pmc-s3" and c}
    if s3_rows:  # same S3 address again, now with the licence Europe PMC states for that copy
        apply(pmc_s3_links(http, s3_rows, {p: links.license for p, links in epmc.items()}), "pmc-s3")
    pmcids = {p: (epmc[p].pmcid if p in epmc and epmc[p].pmcid else pmcid) for p, _, pmcid, _ in pending}
    apply(pmc_s3_links(http, {p: c for p, c in pmcids.items() if c and p in open_},
                       {p: links.license for p, links in epmc.items()}), "pmc-s3")
    if src.unpaywall_email:
        apply(unpaywall_links(http, still_open_dois(), src.unpaywall_email,
                              {p: url for p, _, _, url in pending if url}), "unpaywall")
    if src.elsevier_api_key:
        todo = still_open_dois()
        apply(elsevier_links(http, todo, src.elsevier_api_key, src.elsevier_insttoken, storage.pdf_links(todo)), "elsevier")
    if src.wiley_tdm_token:
        todo = still_open_dois()
        apply(wiley_links(http, todo, src.wiley_tdm_token, storage.pdf_links(todo)), "wiley")
    if src.core_api_key:
        apply(core_links(http, still_open_dois(), src.core_api_key), "core")
    if src.openalex_api_key:  # looked up for every pending article: OA status and licence are worth having
        found = openalex_links(http, {p: d for p, d in dois.items()}, src.openalex_api_key)
        for pmid, links in found.items():
            if pmid not in open_:  # a better PDF exists already: keep only the provenance
                links.url_pdf, links.pdf_source = "", ""
        apply(found, "openalex")
    if src.unpaywall_email:  # OA status for whatever still lacks it, without trying any download
        todo = storage.without_oa_status([p for p, *_ in pending] + [p for p, *_ in backfill])
        lookup = {**dois, **{p: d for p, d, *_ in backfill if d}}
        stored = storage.pdf_links(todo)
        for pmid, links in unpaywall_provenance(http, {p: lookup[p] for p in todo if p in lookup},
                                                src.unpaywall_email, stored).items():
            changed += storage.update_oa(pmid, links)
        storage.commit()
    if cfg.downloads.preprints:
        for pmid, links in preprint_links(http, storage.preprint_ids(sorted(open_)), src.unpaywall_email).items():
            if storage.update_preprint(pmid, links):
                changed += 1
                gained["preprint"] += links.source == "preprint"
        storage.commit()
    storage.mark_oa_checked(p for p, *_ in pending)
    storage.commit()
    if backfill:
        log.info("eredetadatok (licenc, OA-státusz) pótolva: %d cikknél", len(backfill))
    log.info("OA-linkek: %d cikk ellenőrizve, programból letölthetővé vált: %s, frissült: %d", len(pending),
             ", ".join(f"{k} {v}" for k, v in gained.items() if v) or "0", changed)
    return changed


def refresh_bibliography(storage: Storage, pubmed: PubMed, today: date, limit: int = 400) -> int:
    """Re-reads volume/issue (and dates) from PubMed for articles that have none yet. PubMed assigns the issue
    weeks after the online-first publication; the PDF folder name uses what is known at download time."""
    pmids = storage.pending_bibliography(today.isoformat(), limit)
    if not pmids:
        return 0
    changed = 0
    seen: set[str] = set()
    for article in pubmed.fetch(pmids):
        seen.add(article.pmid)
        storage.update_bibliography(article)
        changed += 1 if (article.volume or article.issue) else 0
    storage.mark_bibliography_checked(set(pmids) - seen)  # withdrawn / unavailable: do not ask again every day
    storage.commit()
    log.info("kiadványadatok: %d cikk újraolvasva, ebből kötet/szám ismert: %d", len(pmids), changed)
    return changed


def refresh_links(cfg: Config, http: HttpClient | None = None, today: date | None = None,
                  force: bool = False) -> int:
    """Link refresh on its own. `force` ignores the back-off schedule (used once after adding a link source)."""
    storage = Storage(cfg.data_dir)
    try:
        return enrich_links(storage, http or make_http(cfg), cfg, today or date.today(), force=force)
    finally:
        storage.close()


SUPPLEMENT_DIR = "_mellekletek"
SUPPLEMENT_NOTE = """Ezek cikkekhez tartozó mellékletek (adatlapok, függelékek). Egy hiba miatt a rendszer a cikk helyett
ezeket töltötte le a PMC-ből (2026. október). A cikkek saját PDF-jét a következő letöltés pótolja.
A fájlnév eleje a cikk PMID-je. A mappa nyugodtan törölhető.
"""


def repair_pmc_supplements(cfg: Config, http: HttpClient | None = None, now: datetime | None = None) -> int:
    """One-off: before the fix, a PMC link could point at a supplementary PDF of the article (the first PDF of the
    folder in alphabetical order, e.g. Data_Sheet_1.PDF) instead of the article itself. Those links get the article
    PDF (or none, if PMC has no PDF of the article); a supplement already downloaded under the article's name is
    moved to `_mellekletek/` and the article is offered for download again. Returns how many articles were fixed."""
    now = now or datetime.now().astimezone()
    storage = Storage(cfg.data_dir)
    try:
        wrong = [r for r in storage.pmc_links() if not PMC_MAIN_PDF.search(r["url_pdf"])]
        if not wrong:
            return 0
        pmcids = {r["pmid"]: r["pmcid"] or r["url_pdf"].rsplit("/", 2)[-2].split(".")[0] for r in wrong}
        fixed = pmc_s3_links(http or make_http(cfg), pmcids, {r["pmid"]: r["pdf_license"] for r in wrong})
        root = Path(cfg.api.pdf_dir) if cfg.api.pdf_dir else None
        ledger = {d["pmid"]: d for d in storage.downloads(status="ok", limit=100000) if d["version"] == "vor"}
        moved = 0
        for r in wrong:
            pmid = r["pmid"]
            links = fixed.get(pmid)
            storage.set_pdf_link(pmid, links.url_pdf if links else "", "pmc-s3" if links else "",
                                 "publishedVersion" if links else "")
            done = ledger.get(pmid)
            if done is None:
                continue
            if root is not None and done["path"]:
                name = f"{pmid}_{r['url_pdf'].rsplit('/', 1)[-1]}"  # never matches the YYYY-<pmid>-... pattern
                if organize_file(root, done["path"], f"{SUPPLEMENT_DIR}/{name}", keep_dirs=(cfg.downloads.inbox,)):
                    moved += 1
            storage.offer_again(pmid, "vor", now, "melléklet töltődött le a cikk helyett")
        storage.commit()
        if moved and root is not None:
            note = root / SUPPLEMENT_DIR / "OLVASS_EL.txt"
            if not note.exists():
                note.write_text(SUPPLEMENT_NOTE, encoding="utf-8")
        log.info("PMC-linkek javítva: %d cikk (a cikk helyett melléklet), ebből %d letöltött fájl áthelyezve a(z) "
                 "%s mappába", len(wrong), moved, SUPPLEMENT_DIR)
        return len(wrong)
    finally:
        storage.close()


def refresh_bibliography_job(cfg: Config, http: HttpClient | None = None, today: date | None = None) -> int:
    storage = Storage(cfg.data_dir)
    try:
        pubmed = PubMed(http or make_http(cfg), cfg.sources.ncbi_api_key, cfg.sources.ncbi_email)
        return refresh_bibliography(storage, pubmed, today or date.today())
    finally:
        storage.close()


def send_digest(cfg: Config, days: int, today: date | None = None, send_mail: bool = True) -> Digest:
    """Mails a digest of everything that entered PubMed in the last `days` days, straight from the database."""
    today = today or date.today()
    since = today - timedelta(days=days)
    storage = Storage(cfg.data_dir)
    try:
        articles = storage.articles(entrez_since=since.isoformat(), limit=100_000)
        trials = storage.trials(posted_since=since.isoformat(), limit=100_000)
    finally:
        storage.close()
    total = len(articles) + len(trials)
    digest = build(cfg, today, (since, today), articles, trials,
                   subject=f"PubMed-figyelő {today:%Y.%m.%d.} – összesítő az utolsó {days} napról ({total} tétel)",
                   lead=f"{total} tétel az adatbázisból · PubMed-be került {since:%m.%d.} óta")
    if send_mail:
        mailer.send_with_retry(cfg.mail, digest.subject, digest.html, digest.text)
        log.info("összesítő levél elküldve (%d nap, %d tétel)", days, total)
    return digest


def webhook_payload(result: RunResult, articles: list[dict], trials: list[dict]) -> dict:
    return {
        "event": "pubmed-watch.run",
        "run_id": result.run_id,
        "baseline": result.baseline,
        "window": {"from": result.window[0].isoformat(), "to": result.window[1].isoformat()},
        "counts": {"articles": len(articles), "trials": len(trials),
                   "articles_with_pdf": sum(1 for a in articles if a["links"]["pdf"])},
        "articles": articles,
        "trials": trials,
    }


def run_once(cfg: Config, send_mail: bool = True, today: date | None = None, http: HttpClient | None = None) -> RunResult:
    today = today or date.today()
    http = http or make_http(cfg)
    storage = Storage(cfg.data_dir)
    since, until, baseline = search_window(storage, cfg, today)
    run_id = storage.start_run(since.isoformat(), until.isoformat(), baseline)
    log.info("futás #%d indul: %s – %s%s", run_id, since, until, " (alapállapot)" if baseline else "")
    try:
        pubmed = PubMed(http, cfg.sources.ncbi_api_key, cfg.sources.ncbi_email)
        hits: dict[str, list[str]] = defaultdict(list)
        for topic in cfg.topics:
            pmids = pubmed.search(topic.query, since, until)
            log.info("  %-30s %4d találat", topic.id, len(pmids))
            for pmid in pmids:
                hits[pmid].append(topic.id)

        known = storage.known_pmids(hits)
        for pmid in known:
            storage.add_topics(pmid, hits[pmid], run_id)
        new_ids = [p for p in hits if p not in known]
        articles = pubmed.fetch(new_ids)
        for article in articles:
            classify(article)
            storage.insert_article(article, section_of(cfg, article.kind, hits[article.pmid]), run_id)
            storage.add_topics(article.pmid, hits[article.pmid], run_id)
        storage.commit()
        log.info("új cikk: %d (ismert: %d)", len(articles), len(known))

        enrich_links(storage, http, cfg, today)
        try:
            refresh_bibliography(storage, pubmed, today)
            # an issue assigned meanwhile may mean a better folder for a PDF filed as online-first
            organize_all(storage, cfg.api.pdf_dir, cfg.downloads.folders, cfg.downloads.inbox)
        except Exception:  # citation details are not worth failing the day for
            log.exception("a kiadványadatok frissítése / a PDF-ek rendezése sikertelen")

        trial_count = 0
        if cfg.trials.enabled:
            trials = new_trials(http, cfg.trials, since, until)
            known_trials = storage.known_trials(t.nct_id for t in trials)
            for trial in trials:
                if trial.nct_id not in known_trials:
                    storage.insert_trial(trial, run_id)
                    trial_count += 1
            storage.commit()
            log.info("új vizsgálat: %d (ismert: %d)", trial_count, len(known_trials))

        report_articles = storage.articles(run_id=run_id, limit=100_000)
        report_trials = storage.trials(run_id=run_id, limit=100_000)
        if baseline:  # the first mail shows the latest days in full; the rest is in the database
            recent = (until - timedelta(days=cfg.baseline_digest_days)).isoformat()
            digest = build(cfg, today, (since, until),
                           [a for a in report_articles if a["entrez_date"] >= recent],
                           [t for t in report_trials if t["first_posted"] >= recent],
                           baseline=True, baseline_total=len(report_articles) + len(report_trials))
        else:
            digest = build(cfg, today, (since, until), report_articles, report_trials)
        cfg.data_dir.joinpath("last_report.html").write_text(digest.html, encoding="utf-8")
        cfg.data_dir.joinpath("last_report.txt").write_text(digest.text, encoding="utf-8")

        mail_sent, message = False, ""
        if send_mail and (report_articles or report_trials or baseline or cfg.send_empty):
            try:
                mailer.send_with_retry(cfg.mail, digest.subject, digest.html, digest.text)
                mail_sent = True
                log.info("levél elküldve: %s", ", ".join(cfg.mail.recipients))
            except Exception as exc:  # the data is stored; a lost mail must not hide it from the next run
                message = f"levélküldés sikertelen: {exc}"
                log.error(message)

        result = RunResult(run_id, baseline, (since, until), len(report_articles), len(report_trials), mail_sent, digest)
        if cfg.api.webhook_url:
            try:
                http.post_json(cfg.api.webhook_url, webhook_payload(result, report_articles, report_trials))
                log.info("n8n-webhook értesítve")
            except Exception as exc:
                message = (message + "; " if message else "") + f"webhook sikertelen: {exc}"
                log.error("n8n-webhook sikertelen: %s", exc)

        storage.finish_run(run_id, "ok", len(report_articles), len(report_trials), mail_sent, message)
        return result
    except Exception as exc:
        storage.finish_run(run_id, "error", message=str(exc)[:500])
        raise
    finally:
        storage.close()
