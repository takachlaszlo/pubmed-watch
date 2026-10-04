"""Polite HTTP client: honest User-Agent, per-host pacing, retries on transient errors."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, user_agent: str, timeout: float = 60.0, min_delay: dict[str, float] | None = None,
                 attempts: int = 4, backoff: float = 5.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.min_delay = min_delay or {}
        self.attempts = attempts
        self.backoff = backoff
        self._last: dict[str, float] = {}

    def _pace(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        delay = self.min_delay.get(host, 0.5)
        wait = self._last.get(host, 0.0) + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(1, self.attempts + 1):
            self._pace(url)
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise
                log.warning("hálózati hiba (%s), újrapróbálom: %s", urlsplit(url).hostname, exc)
            else:
                if resp.status_code not in RETRY_STATUS or attempt == self.attempts:
                    resp.raise_for_status()
                    return resp
                log.warning("%s válasza %d, újrapróbálom", urlsplit(url).hostname, resp.status_code)
            time.sleep(self.backoff * attempt)
        raise RuntimeError("unreachable")

    def get_json(self, url: str, params: dict | None = None) -> dict:
        return self.request("GET", url, params=params).json()

    def post_json(self, url: str, payload: dict) -> requests.Response:
        return self.request("POST", url, json=payload)
