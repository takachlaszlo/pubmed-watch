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
from .http import HttpClient
from .openaccess import europepmc_links, unpaywall_links
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
                       "clinicaltrials.gov": 0.5})


def search_window(storage: Storage, cfg: Config, today: date) -> tuple[date, date, bool]:
    last = storage.last_ok_run()
    if last is None:
        return today - timedelta(days=cfg.baseline_days), today, True
    last_to = min(date.fromisoformat(last["window_to"]), today)
    since = max(last_to - timedelta(days=cfg.lookback_days), today - timedelta(days=MAX_CATCH_UP_DAYS))
    return since, today, False


def enrich_links(storage: Storage, http: HttpClient, cfg: Config, today: date) -> int:
    """Looks up open-access full text / PDF for recent articles that have none yet."""
    pending = storage.pending_oa((today - timedelta(days=cfg.oa_recheck_days)).isoformat(), today.isoformat())
    if not pending:
        return 0
    changed = 0
    found = europepmc_links(http, [p for p, _ in pending])
    for pmid, links in found.items():
        changed += storage.update_oa(pmid, links)
    if cfg.sources.unpaywall_email:
        missing = {p: doi for p, doi in pending if doi and not (found.get(p) and found[p].url_pdf)}
        for pmid, links in unpaywall_links(http, missing, cfg.sources.unpaywall_email).items():
            changed += storage.update_oa(pmid, links)
    storage.mark_oa_checked(p for p, _ in pending)
    storage.commit()
    log.info("OA-linkek: %d cikk ellenőrizve, %d frissült", len(pending), changed)
    return changed


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
        digest = build(cfg, today, (since, until), report_articles, report_trials, baseline=baseline)
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
