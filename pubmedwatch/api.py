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
GET /downloads/due?hours=         articles the n8n workflow should download now (see Storage.downloads_due);
                                  each carries download.path, the relative PDF path to save to
GET /downloads?status=            the download ledger (ok | failed)
POST /downloads/report            {"pmid", "status": "ok"|"failed", "path", "error", "stage": "download"|"save"}:
                                  n8n reports each attempt (a saved PDF is then filed into its journal/issue folder);
                                  the only write the API accepts
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .downloads import inbox_relpath, organize_all, pdf_relpath, safe_relpath, scan_pdf_dir
from .storage import Storage

log = logging.getLogger(__name__)

MAX_LIMIT = 1000
MAX_BODY = 65536


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
        folders, inbox = cfg.downloads.folders, cfg.downloads.inbox
        return ok(storage.downloads_due(
            now, hours, cfg.downloads.retry_for_days,
            lambda article: {"path": inbox_relpath(article, inbox), "final_path": pdf_relpath(article, folders)}))
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
    size = payload.get("bytes")
    path = str(payload.get("path", "")).strip()[:300]
    if status == "ok" and path and not safe_relpath(path):
        raise ApiError(400, "a path a PDF-mappához képest relatív, .pdf végű útvonal legyen, '..' nélkül")
    # a problem on our side (the folder is not writable) says nothing about the article: try again tomorrow
    local = payload.get("stage") == "save"
    row = storage.report_download(
        pmid, status, datetime.now().astimezone(), cfg.downloads.retry_every_days, path=path,
        size=size if isinstance(size, int) else None, error=str(payload.get("error", "")),
        retry_in_days=1 if local else None)
    if status == "ok":
        organize_all(storage, cfg.api.pdf_dir, cfg.downloads.folders, cfg.downloads.inbox, only_pmid=pmid)
        row = dict(storage.db.execute("SELECT * FROM downloads WHERE pmid=?", (pmid,)).fetchone())
    return 200, "application/json; charset=utf-8", json.dumps(row, ensure_ascii=False).encode()


def make_server(cfg: Config, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    storage = Storage(cfg.data_dir)
    lock = threading.Lock()  # one shared connection; requests are tiny

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if cfg.api.token and not hmac.compare_digest(self.headers.get("X-API-Key", ""), cfg.api.token):
                    raise ApiError(401, "hiányzó vagy hibás X-API-Key fejléc")
                with lock:
                    status, ctype, body = handle(cfg, storage, url.path, params)
            except ApiError as exc:
                status, ctype = exc.status, "application/json; charset=utf-8"
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            except Exception:
                log.exception("API-hiba: %s", self.path)
                status, ctype, body = 500, "application/json; charset=utf-8", b'{"error": "belso hiba"}'
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
                    status, ctype, body = handle_post(cfg, storage, url.path, payload)
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
