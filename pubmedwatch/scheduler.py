"""Keeps the container alive: serves the API and starts one run per day at the configured time."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .api import serve_in_background
from .config import load_config
from .runner import run_once
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


def _run(config_path: str | None) -> None:
    try:
        run_once(load_config(config_path))  # config is re-read so edits apply without a restart
    except Exception:
        log.exception("a napi futás hibával leállt; holnap újrapróbálom (a kimaradt napokat pótolja)")


def daemon(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    serve_in_background(cfg)
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
        while (remaining := (target - datetime.now()).total_seconds()) > 0:
            time.sleep(min(remaining, 60))  # short naps survive clock changes and suspend
        if _done_today(config_path):
            log.info("ma már volt sikeres futás – kihagyom")
        else:
            _run(config_path)
        time.sleep(61)  # never fire twice within the same minute
