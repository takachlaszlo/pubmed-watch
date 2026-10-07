"""JSON API over the canonical database for n8n's HTTP Request node (read-only, except for the download report).

GET /health                       service + database counts
GET /topics                       topics and report sections from config.yaml
GET /runs?limit=                  run history (newest first)
GET /runs/latest                  last successful run
GET /articles?...                 filters: run_id, since, updated_since (ISO date/time), topic, section,
                                  kind, has_pdf (true/false), pdf_source (pmc-s3 | unpaywall | europepmc), limit (max 1000), offset
GET /articles/<pmid>
GET /trials?...                   filters: run_id, since, status, limit, offset
GET /trials/<nct_id>
GET /reports/latest               the last e-mail digest as HTML
GET /downloads/due?hours=         what the n8n workflow should download now (see Storage.downloads_due); each item
                                  carries download.url (what to fetch), download.path (where to save it, relative
                                  to the PDF folder), download.version (vor | preprint)
GET /downloads/file/<pmid>        the PDF of a source that needs a key (Elsevier, Wiley, OpenAlex), fetched by this
                                  service so that the key never leaves the NAS
GET /downloads?status=            the download ledger (ok | failed)
POST /downloads/report            {"pmid", "version", "status": "ok"|"failed", "path", "error", "stage"}: n8n reports
                                  each attempt (a saved PDF is then filed into its journal/issue folder);
                                  the only write the API accepts
"""
from __future__ import annotations

import hmac
import json
import logging
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .config import Config
from .downloads import inbox_relpath, organize_all, pdf_filename, pdf_relpath, safe_relpath, scan_pdf_dir
from .http import redact
from .openaccess import (ELSEVIER_ARTICLE, PROXY_SOURCES, elsevier_headers, openalex_pdf_url, wiley_headers,
                         wiley_url)
from .storage import Storage

log = logging.getLogger(__name__)

MAX_LIMIT = 1000
MAX_BODY = 65536


def download_paths(cfg: Config, article: dict, version: str) -> dict:
    """Where n8n fetches the PDF from and where it saves it (the inbox), and where it will be filed."""
    if version == "preprint":
        url = (article.get("preprint") or {}).get("pdf", "")
    elif article.get("pdf_source") in PROXY_SOURCES:
        url = f"{cfg.api.public_url}/downloads/file/{article['pmid']}"
    else:
        url = article["links"]["pdf"]
    return {"url": url, "path": inbox_relpath(article, cfg.downloads.inbox, version),
            "final_path": pdf_relpath(article, cfg.downloads.folders, version)}


def upstream_for(cfg: Config, article: dict) -> tuple[str, dict]:
    """The official PDF address (and auth headers) of a proxy source. Raises ApiError if it cannot be served."""
    source, doi, src = article.get("pdf_source"), article.get("doi") or "", cfg.sources
    if source == "openalex" and article.get("openalex_id") and src.openalex_api_key:
        return openalex_pdf_url(article["openalex_id"], src.openalex_api_key), {}
    if source == "elsevier" and doi and src.elsevier_api_key:
        return ELSEVIER_ARTICLE + quote(doi, safe="/"), elsevier_headers(src.elsevier_api_key, src.elsevier_insttoken,
                                                                          accept="application/pdf")
    if source == "wiley" and doi and src.wiley_tdm_token:
        return wiley_url(doi), wiley_headers(src.wiley_tdm_token)
    raise ApiError(400, "ehhez a cikkhez nincs kulcsos forrás (vagy hiányzik a kulcs)")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _int(params: dict, name: str, default: int | None = None, maximum: int | None = None) -> int | None:
    if name not in params:
        return default
    try:
        value = int(params[name])
    except ValueError:
        raise ApiError(400, f"a(z) {name} paraméter egész szám legyen")
    return min(value, maximum) if maximum else value


def _bool(params: dict, name: str) -> bool | None:
    if name not in params:
        return None
    return params[name].lower() in ("1", "true", "yes", "igen")


def handle(cfg: Config, storage: Storage, path: str, params: dict[str, str]) -> tuple[int, str, bytes]:
    """Routes one request; returns (status, content type, body). Kept free of HTTP plumbing for tests."""
    parts = [p for p in path.split("/") if p]
    limit = _int(params, "limit", 100, MAX_LIMIT)
    offset = _int(params, "offset", 0)

    def ok(payload) -> tuple[int, str, bytes]:
        return 200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode()

    if parts == ["health"]:
        last = storage.last_ok_run()
        return ok({"status": "ok", "counts": storage.counts(), "last_ok_run": dict(last) if last else None})
    if parts == ["topics"]:
        return ok({"topics": [{"id": t.id, "label": t.label, "section": t.section, "query": t.query} for t in cfg.topics],
                   "sections": [{"id": s.id, "title": s.title, "style": s.style} for s in cfg.sections]})
    if parts == ["runs"]:
        return ok(storage.runs(limit or 30))
    if parts == ["runs", "latest"]:
        last = storage.last_ok_run()
        if last is None:
            raise ApiError(404, "még nem volt sikeres futás")
        return ok(dict(last))
    if parts == ["articles"]:
        return ok(storage.articles(run_id=_int(params, "run_id"), since=params.get("since"),
                                   updated_since=params.get("updated_since"), topic=params.get("topic"),
                                   section=params.get("section"), kind=params.get("kind"),
                                   has_pdf=_bool(params, "has_pdf"), pdf_source=params.get("pdf_source"),
                                   entrez_since=params.get("entrez_since"), limit=limit, offset=offset))
    if len(parts) == 2 and parts[0] == "articles":
        article = storage.article(parts[1])
        if article is None:
            raise ApiError(404, "nincs ilyen cikk")
        return ok(article)
    if parts == ["trials"]:
        return ok(storage.trials(run_id=_int(params, "run_id"), since=params.get("since"),
                                 status=params.get("status"), limit=limit, offset=offset))
    if len(parts) == 2 and parts[0] == "trials":
        trial = storage.trial(parts[1])
        if trial is None:
            raise ApiError(404, "nincs ilyen vizsgálat")
        return ok(trial)
    if parts == ["downloads", "due"]:
        now = datetime.now().astimezone()
        storage.reconcile_downloads(scan_pdf_dir(cfg.api.pdf_dir), now)  # whatever is on disk is not offered again
        organize_all(storage, cfg.api.pdf_dir, cfg.downloads.folders, cfg.downloads.inbox)  # strays in the inbox
        hours = _int(params, "hours", cfg.downloads.window_hours, 24 * 400)
        dl = cfg.downloads
        return ok(storage.downloads_due(now, hours, dl.retry_for_days,
                                        lambda article, version: download_paths(cfg, article, version),
                                        sections=dl.sections, kinds=dl.kinds, preprints=dl.preprints,
                                        max_retries=dl.max_retries_per_run or None))
    if parts == ["downloads"]:
        return ok(storage.downloads(params.get("status"), limit or 100, offset or 0))
    if parts == ["reports", "latest"]:
        report = cfg.data_dir / "last_report.html"
        if not report.exists():
            raise ApiError(404, "még nincs jelentés")
        return 200, "text/html; charset=utf-8", report.read_bytes()
    raise ApiError(404, "ismeretlen útvonal")


def handle_post(cfg: Config, storage: Storage, path: str, payload: object) -> tuple[int, str, bytes]:
    """The one write the API accepts: n8n reporting the outcome of a PDF download attempt."""
    parts = [p for p in path.split("/") if p]
    if parts != ["downloads", "report"]:
        raise ApiError(404, "ismeretlen útvonal")
    if not isinstance(payload, dict):
        raise ApiError(400, "JSON objektum kell")
    pmid = str(payload.get("pmid", "")).strip()
    status = payload.get("status")
    if not pmid.isdigit():
        raise ApiError(400, "a pmid számokból álljon")
    if status not in ("ok", "failed"):
        raise ApiError(400, 'a status "ok" vagy "failed" legyen')
    if not storage.has_article(pmid):
        raise ApiError(404, "nincs ilyen cikk")
    version = payload.get("version") or "vor"
    if version not in ("vor", "preprint"):
        raise ApiError(400, 'a version "vor" vagy "preprint" legyen')
    size = payload.get("bytes")
    path = str(payload.get("path", "")).strip()[:300]
    if status == "ok" and path and not safe_relpath(path):
        raise ApiError(400, "a path a PDF-mappához képest relatív, .pdf végű útvonal legyen, '..' nélkül")
    # a problem on our side (the folder is not writable) says nothing about the article: try again tomorrow
    local = payload.get("stage") == "save"
    row = storage.report_download(
        pmid, status, datetime.now().astimezone(), cfg.downloads.retry_every_days, path=path,
        size=size if isinstance(size, int) else None, error=str(payload.get("error", "")),
        retry_in_days=1 if local else None, version=version)
    if status == "ok":
        organize_all(storage, cfg.api.pdf_dir, cfg.downloads.folders, cfg.downloads.inbox, only_pmid=pmid)
        row = dict(storage.db.execute("SELECT * FROM downloads WHERE pmid=? AND version=?", (pmid, version)).fetchone())
    return 200, "application/json; charset=utf-8", json.dumps(row, ensure_ascii=False).encode()


def make_server(cfg: Config, host: str = "0.0.0.0", http=None) -> ThreadingHTTPServer:
    storage = Storage(cfg.data_dir)
    lock = threading.Lock()  # one shared connection; requests are tiny
    if http is None:
        from .runner import make_http  # late import: runner pulls in most of the package
        http = make_http(cfg)

    class Handler(BaseHTTPRequestHandler):
        def _json_error(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy_pdf(self, pmid: str) -> None:
            """Streams the PDF of a key-protected source. The database lock is held only for the lookup."""
            with lock:
                article = storage.article(pmid)
            if article is None:
                raise ApiError(404, "nincs ilyen cikk")
            url, headers = upstream_for(cfg, article)
            try:
                # one try only: n8n waits at most two minutes, and a failed try comes back at the next daily run
                resp = http.stream(url, headers=headers, attempts=1)
            except Exception as exc:
                raise ApiError(502, f"a forrás nem adta ki a PDF-et: {redact(exc)}")
            try:
                chunks = resp.iter_content(65536)
                first = next(chunks, b"")
                if not first.startswith(b"%PDF"):
                    raise ApiError(502, "a forrás nem PDF-et adott vissza")
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{pdf_filename(article)}"')
                self.end_headers()
                try:
                    self.wfile.write(first)
                    for chunk in chunks:
                        self.wfile.write(chunk)
                except Exception as exc:  # headers are out: nothing sensible to send any more
                    log.warning("a PDF továbbítása megszakadt (%s): %s", pmid, redact(exc))
            finally:
                resp.close()

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            try:
                if cfg.api.token and not hmac.compare_digest(self.headers.get("X-API-Key", ""), cfg.api.token):
                    raise ApiError(401, "hiányzó vagy hibás X-API-Key fejléc")
                if len(parts) == 3 and parts[:2] == ["downloads", "file"] and parts[2].isdigit():
                    self._proxy_pdf(parts[2])
                    return
                with lock:
                    try:
                        status, ctype, body = handle(cfg, storage, url.path, params)
                    except Exception as exc:
                        # a failed write leaves the connection inside a transaction with an old snapshot, which
                        # would refuse every later write ("database is locked") until the next restart
                        storage.db.rollback()
                        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                            raise ApiError(503, "az adatbázis épp foglalt, próbáld újra később")
                        raise
            except ApiError as exc:
                self._json_error(exc.status, str(exc))
                return
            except Exception:
                log.exception("API-hiba: %s", redact(self.path))
                self._json_error(500, "belso hiba")
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            try:
                if cfg.api.token and not hmac.compare_digest(self.headers.get("X-API-Key", ""), cfg.api.token):
                    raise ApiError(401, "hiányzó vagy hibás X-API-Key fejléc")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ApiError(413, "túl nagy kérés")
                try:
                    payload = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    raise ApiError(400, "érvénytelen JSON")
                with lock:
                    try:
                        status, ctype, body = handle_post(cfg, storage, url.path, payload)
                    except Exception as exc:
                        storage.db.rollback()  # see do_GET: never keep a half-done transaction around
                        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                            raise ApiError(503, "az adatbázis épp foglalt, próbáld újra később")
                        raise
            except ApiError as exc:
                status, ctype = exc.status, "application/json; charset=utf-8"
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            except Exception:
                log.exception("API-hiba (POST): %s", self.path)
                status, ctype, body = 500, "application/json; charset=utf-8", b'{"error": "belso hiba"}'
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            log.debug("API %s - %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, cfg.api.port), Handler)


def serve_in_background(cfg: Config) -> ThreadingHTTPServer | None:
    if not cfg.api.port:
        return None
    server = make_server(cfg)
    threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
    log.info("API elérhető a(z) %d porton%s", cfg.api.port, " (X-API-Key védelemmel)" if cfg.api.token else "")
    return server
