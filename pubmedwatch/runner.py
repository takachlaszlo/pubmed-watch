"""One daily run: search, store, enrich links, build the digest, mail it, notify n8n."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from . import mailer
from .classify import classify, section_of
from .clinicaltrials import new_trials
from .config import Config
from .downloads import organize_all
from .http import HttpClient
from .openaccess import europepmc_links, pmc_s3_links, unpaywall_links
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
                       "clinicaltrials.gov": 0.5, "pmc-oa-opendata.s3.amazonaws.com": 0.1})


def search_window(storage: Storage, cfg: Config, today: date) -> tuple[date, date, bool]:
    last = storage.last_ok_run()
    if last is None:
        return today - timedelta(days=cfg.baseline_days), today, True
    last_to = min(date.fromisoformat(last["window_to"]), today)
    since = max(last_to - timedelta(days=cfg.lookback_days), today - timedelta(days=MAX_CATCH_UP_DAYS))
    return since, today, False


def enrich_links(storage: Storage, http: HttpClient, cfg: Config, today: date, force: bool = False) -> int:
    """Open-access full text / PDF links for recent articles whose PDF can still improve.

    Order of preference: PMC Article Datasets (S3, scriptable) > Unpaywall (optional) > Europe PMC web link."""
    pending = storage.pending_oa((today - timedelta(days=cfg.oa_recheck_days)).isoformat(), today.isoformat(), force)
    if not pending:
        return 0
    changed = 0
    found = europepmc_links(http, [p for p, _, _, _ in pending])
    for pmid, links in found.items():
        changed += storage.update_oa(pmid, links)
    pmcids = {pmid: (found[pmid].pmcid if pmid in found and found[pmid].pmcid else pmcid)
              for pmid, _, pmcid, _ in pending}
    s3 = pmc_s3_links(http, {pmid: pmcid for pmid, pmcid in pmcids.items() if pmcid})
    for pmid, links in s3.items():
        changed += storage.update_oa(pmid, links)
    if cfg.sources.unpaywall_email:
        missing = {p: doi for p, doi, _, _ in pending if doi and p not in s3}
        known_pdf = {p: url for p, _, _, url in pending if url}
        for pmid, links in unpaywall_links(http, missing, cfg.sources.unpaywall_email, known_pdf).items():
            changed += storage.update_oa(pmid, links)
    storage.mark_oa_checked(p for p, _, _, _ in pending)
    storage.commit()
    log.info("OA-linkek: %d cikk ellenőrizve, ebből PMC S3: %d%s, frissült: %d", len(pending), len(s3),
             ", Unpaywall is" if cfg.sources.unpaywall_email else "", changed)
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
