"""Keeps the container alive: serves the API and starts one run per day at the configured time."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from . import selfupdate
from .api import serve_in_background
from .config import load_config
from .downloads import organize_all, scan_pdf_dir
from .storage import Storage as _Storage
from .runner import (make_http, refresh_bibliography_job, refresh_links, repair_pmc_supplements, run_once,
                     send_digest)
from .storage import Storage

log = logging.getLogger(__name__)


def target_today(now: datetime, run_at: str) -> datetime:
    hour, minute = (int(part) for part in run_at.split(":", 1))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_target(now: datetime, run_at: str) -> datetime:
    target = target_today(now, run_at)
    return target if now < target else target + timedelta(days=1)


def _done_today(config_path: str | None) -> bool:
    storage = Storage(load_config(config_path).data_dir)
    try:
        return storage.has_completed_run_on(datetime.now().date().isoformat())
    finally:
        storage.close()


def _sleep_until(when: datetime) -> None:
    while (remaining := (when - datetime.now()).total_seconds()) > 0:
        time.sleep(min(remaining, 60))  # short naps survive clock changes and suspend


def _update_if_newer(config_path: str | None) -> None:
    """Exits the process when GitHub has a newer version; Docker starts it again with the new code."""
    try:
        cfg = load_config(config_path)
        sha = selfupdate.newer_version(make_http(cfg), cfg.update)
    except Exception:
        log.exception("önfrissítés: az ellenőrzés sikertelen, a mostani változattal megyek tovább")
        return
    if sha:
        log.info("önfrissítés: új változat a GitHubon (%s -> %s), újraindulok", selfupdate.running["sha"][:7], sha[:7])
        logging.shutdown()
        os._exit(0)  # the API thread must not keep the old process alive


def _run(config_path: str | None) -> None:
    try:
        run_once(load_config(config_path))  # config is re-read so edits apply without a restart
    except Exception:
        log.exception("a napi futás hibával leállt; holnap újrapróbálom (a kimaradt napokat pótolja)")


def _once(cfg, name: str, value: str, action) -> None:
    """Runs `action` once per distinct `value`: the value is remembered in a marker file in the data folder."""
    marker = cfg.data_dir / f"{name}.done"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == value:
        return
    action()
    marker.write_text(value, encoding="utf-8")


def sources_signature(cfg) -> str:
    """Which optional link sources are configured (names only, never the keys)."""
    src = cfg.sources
    enabled = {"unpaywall": src.unpaywall_email, "openalex": src.openalex_api_key, "elsevier": src.elsevier_api_key,
               "wiley": src.wiley_tdm_token, "core": src.core_api_key, "preprints": cfg.downloads.preprints}
    return "sources:" + ",".join(sorted(name for name, value in enabled.items() if value))


def _startup_tasks(config_path: str | None) -> None:
    """Catch up on link / citation data after downtime, plus the optional one-time actions."""
    cfg = load_config(config_path)
    for label, job in (("kiadványadatok", lambda: refresh_bibliography_job(cfg)),
                       ("linkek", lambda: refresh_links(cfg))):
        try:
            job()
        except Exception:
            log.exception("a(z) %s frissítése indításkor sikertelen; a napi futás megismétli", label)
    try:
        _once(cfg, "pmc_supplement_repair", "2026-10-07", lambda: repair_pmc_supplements(cfg))
    except Exception:
        log.exception("a PMC-linkek javítása sikertelen")
    try:
        storage = _Storage(cfg.data_dir)
        try:
            # PDFs already in the folder count as downloaded, and are filed into their journal / issue folder
            storage.reconcile_downloads(scan_pdf_dir(cfg.api.pdf_dir), datetime.now().astimezone())
            organize_all(storage, cfg.api.pdf_dir, cfg.downloads.folders, cfg.downloads.inbox)
            moved = storage.retry_transient_failures(datetime.now().astimezone())
            if moved:
                log.info("megszakadt letöltések: %d most újra letölthető", moved)
        finally:
            storage.close()
    except Exception:
        log.exception("a PDF-ek rendezése indításkor sikertelen")
    # a newly configured source (a key added to the compose) searches all recent articles once, automatically
    signature = sources_signature(cfg)
    try:
        _once(cfg, "link_refresh_sources", signature, lambda: refresh_links(cfg, force=True))
    except Exception:
        log.exception("a teljes linkfrissítés (új forrás) sikertelen")
    token = cfg.schedule.link_refresh_on_start
    if token:
        try:
            _once(cfg, "link_refresh_on_start", token, lambda: refresh_links(cfg, force=True))
        except Exception:
            log.exception("az egyszeri teljes linkfrissítés sikertelen")
    days = cfg.schedule.digest_on_start_days
    if days:
        try:
            _once(cfg, "digest_on_start", str(days), lambda: send_digest(cfg, days))
        except Exception:
            log.exception("az egyszeri összesítő levél küldése sikertelen")


def daemon(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    serve_in_background(cfg)  # first: the API must not wait for GitHub
    selfupdate.remember_start(make_http(cfg), cfg.update)
    _startup_tasks(config_path)
    now = datetime.now()
    log.info("ütemező elindult, napi futás: %s (helyi idő: %s)", cfg.schedule.run_at, now.strftime("%Y-%m-%d %H:%M"))

    if cfg.schedule.run_on_start:
        log.info("RUN_ON_START: azonnali futás")
        _run(config_path)
    elif cfg.schedule.catch_up and now >= target_today(now, cfg.schedule.run_at) and not _done_today(config_path):
        log.info("a mai futás elmaradt – pótlás most")
        _run(config_path)

    while True:
        target = next_target(datetime.now(), load_config(config_path).schedule.run_at)
        log.info("következő futás: %s", target.strftime("%Y-%m-%d %H:%M"))
        check_at = target - selfupdate.LEAD
        if datetime.now() < check_at:  # once a day, half an hour before the run
            _sleep_until(check_at)
            _update_if_newer(config_path)
        _sleep_until(target)
        if _done_today(config_path):
            log.info("ma már volt sikeres futás – kihagyom")
        else:
            _run(config_path)
        time.sleep(61)  # never fire twice within the same minute
