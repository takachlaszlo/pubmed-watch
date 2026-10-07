"""Polite HTTP client: honest User-Agent, per-host pacing, retries on transient errors."""
from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
_SECRET_IN_TEXT = re.compile(r"((?:api_?key|apikey|token|insttoken)=)[^&\s'\"]+", re.I)


def redact(text: object) -> str:
    """Hides API keys that some services expect in the URL, so they never reach a log or an error message."""
    return _SECRET_IN_TEXT.sub(r"\1***", str(text))


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
        self._pace_lock = threading.Lock()  # the API serves requests from several threads

    def _pace(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        delay = self.min_delay.get(host, 0.5)
        with self._pace_lock:
            wait = self._last.get(host, 0.0) + delay - time.monotonic()
            self._last[host] = time.monotonic() + max(wait, 0.0)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(1, self.attempts + 1):
            self._pace(url)
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise requests.RequestException(redact(exc)) from None
                log.warning("hálózati hiba (%s), újrapróbálom: %s", urlsplit(url).hostname, redact(exc))
            else:
                if resp.status_code not in RETRY_STATUS or attempt == self.attempts:
                    if resp.status_code >= 400:
                        resp.close()
                        raise requests.HTTPError(f"HTTP {resp.status_code} ({urlsplit(url).hostname})", response=resp)
                    return resp
                resp.close()
                log.warning("%s válasza %d, újrapróbálom", urlsplit(url).hostname, resp.status_code)
            time.sleep(self.backoff * attempt)
        raise RuntimeError("unreachable")

    def probe(self, url: str, nbytes: int = 8, headers: dict | None = None) -> tuple[int, bytes]:
        """One polite GET that reads only the first bytes: (status, head). (0, b"") if unreachable.
        No retries: a refusal (403/429) is an answer, not something to push against."""
        self._pace(url)
        try:
            resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True, headers=headers)
        except requests.RequestException as exc:
            log.debug("probe sikertelen (%s): %s", urlsplit(url).hostname, redact(exc))
            return 0, b""
        try:
            return resp.status_code, next(resp.iter_content(nbytes), b"")
        finally:
            resp.close()

    def stream(self, url: str, headers: dict | None = None, params: dict | None = None) -> requests.Response:
        """GET whose body is read by the caller in chunks (the caller must close it)."""
        return self.request("GET", url, headers=headers, params=params, stream=True)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        return self.request("GET", url, params=params, headers=headers).json()

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.request("GET", url, params=params).text

    def post_json(self, url: str, payload: dict) -> requests.Response:
        return self.request("POST", url, json=payload)
