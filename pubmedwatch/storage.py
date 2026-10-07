"""Canonical SQLite store: one row per article / trial, topic links, run history.

The API, the report and the n8n webhook all read the same dictionaries (see `article_dict`).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Article, Trial
from .openaccess import AUTO_DOWNLOAD_SOURCES, PDF_SOURCE_RANK, OaLinks, PreprintLinks

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',     -- running | ok | error
    baseline INTEGER NOT NULL DEFAULT 0,
    window_from TEXT NOT NULL,
    window_to TEXT NOT NULL,
    new_articles INTEGER NOT NULL DEFAULT 0,
    new_trials INTEGER NOT NULL DEFAULT 0,
    mail_sent INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS articles (
    pmid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '[]',        -- JSON [[label, text], ...]
    authors TEXT NOT NULL DEFAULT '[]',
    journal TEXT NOT NULL DEFAULT '',
    journal_abbrev TEXT NOT NULL DEFAULT '',
    pub_year TEXT NOT NULL DEFAULT '',
    pub_date TEXT NOT NULL DEFAULT '',
    entrez_date TEXT NOT NULL DEFAULT '',
    pub_types TEXT NOT NULL DEFAULT '[]',
    mesh TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT '',
    doi TEXT NOT NULL DEFAULT '',
    pmcid TEXT NOT NULL DEFAULT '',
    publication_status TEXT NOT NULL DEFAULT '',
    volume TEXT NOT NULL DEFAULT '',
    issue TEXT NOT NULL DEFAULT '',
    bibl_checked_at TEXT NOT NULL DEFAULT '',
    author_emails TEXT NOT NULL DEFAULT '[]',      -- JSON [{name, email}] from PubMed affiliations
    kind TEXT NOT NULL DEFAULT 'other',
    is_update INTEGER NOT NULL DEFAULT 0,
    section TEXT NOT NULL DEFAULT '',
    oa INTEGER NOT NULL DEFAULT 0,
    url_fulltext TEXT NOT NULL DEFAULT '',
    url_pdf TEXT NOT NULL DEFAULT '',
    pdf_source TEXT NOT NULL DEFAULT '',
    oa_status TEXT NOT NULL DEFAULT '',             -- gold | green | hybrid | bronze | diamond | closed
    pdf_license TEXT NOT NULL DEFAULT '',
    pdf_version TEXT NOT NULL DEFAULT '',           -- publishedVersion | acceptedVersion | submittedVersion
    openalex_id TEXT NOT NULL DEFAULT '',
    preprint_id TEXT NOT NULL DEFAULT '',           -- Europe PMC PPR id
    preprint_doi TEXT NOT NULL DEFAULT '',
    preprint_pdf TEXT NOT NULL DEFAULT '',
    preprint_source TEXT NOT NULL DEFAULT '',       -- preprint (scriptable) | preprint-web (people only)
    oa_checked_at TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_first_seen ON articles(first_seen_at);
CREATE INDEX IF NOT EXISTS articles_updated ON articles(updated_at);
CREATE TABLE IF NOT EXISTS article_topics (
    pmid TEXT NOT NULL REFERENCES articles(pmid),
    topic_id TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    PRIMARY KEY (pmid, topic_id)
);
CREATE TABLE IF NOT EXISTS trials (
    nct_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    official_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    study_type TEXT NOT NULL DEFAULT '',
    phases TEXT NOT NULL DEFAULT '[]',
    sponsor TEXT NOT NULL DEFAULT '',
    countries TEXT NOT NULL DEFAULT '[]',
    enrollment INTEGER,
    conditions TEXT NOT NULL DEFAULT '[]',
    interventions TEXT NOT NULL DEFAULT '[]',
    min_age TEXT NOT NULL DEFAULT '',
    max_age TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    primary_completion TEXT NOT NULL DEFAULT '',
    first_posted TEXT NOT NULL DEFAULT '',
    last_update TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trials_first_seen ON trials(first_seen_at);
-- What happened to each PDF download. `ok` rows are permanent: an article is never downloaded twice.
CREATE TABLE IF NOT EXISTS downloads (
    pmid TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'vor',        -- vor (published article) | preprint
    status TEXT NOT NULL,                       -- ok | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    first_attempt_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL DEFAULT '',     -- failed rows: when the next monthly try is due
    last_error TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    bytes INTEGER,
    saved_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',            -- n8n (reported) | disk (found in the PDF folder)
    PRIMARY KEY (pmid, version)
);
"""

JSON_ARTICLE = ("abstract", "authors", "pub_types", "mesh", "keywords", "author_emails")
NEW_ARTICLE_COLUMNS = ("volume", "issue", "bibl_checked_at", "author_emails", "oa_status", "pdf_license",
                       "pdf_version", "openalex_id", "preprint_id", "preprint_doi", "preprint_pdf", "preprint_source")
DOWNLOAD_COLUMNS = ("pmid", "version", "status", "attempts", "first_attempt_at", "last_attempt_at", "next_retry_at",
                    "last_error", "path", "bytes", "saved_at", "source")
JSON_TRIAL = ("phases", "countries", "conditions", "interventions")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Storage:
    def __init__(self, data_dir: Path | str):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path / "pubmed.db", timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(articles)")}
        for name in NEW_ARTICLE_COLUMNS:
            if name not in columns:
                default = "'[]'" if name == "author_emails" else "''"
                self.db.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")
        if "author_emails" not in columns:
            # the e-mail addresses come with the PubMed record: re-read every article once
            self.db.execute("UPDATE articles SET bibl_checked_at=''")
        ledger = {r[1] for r in self.db.execute("PRAGMA table_info(downloads)")}
        if ledger and "version" not in ledger:  # one row per article -> one row per article and version
            self.db.executescript(
                "ALTER TABLE downloads RENAME TO downloads_v1;"
                + SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS downloads"):]
                + "INSERT INTO downloads (pmid, version, status, attempts, first_attempt_at, last_attempt_at, "
                  "next_retry_at, last_error, path, bytes, saved_at, source) SELECT pmid, 'vor', status, attempts, "
                  "first_attempt_at, last_attempt_at, next_retry_at, last_error, path, bytes, saved_at, source "
                  "FROM downloads_v1; DROP TABLE downloads_v1;")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- runs -------------------------------------------------------------------------------
    def start_run(self, window_from: str, window_to: str, baseline: bool) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at, baseline, window_from, window_to) VALUES (?, ?, ?, ?)",
                              (now_iso(), int(baseline), window_from, window_to))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, new_articles: int = 0, new_trials: int = 0,
                   mail_sent: bool = False, message: str = "") -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=?, new_articles=?, new_trials=?, mail_sent=?, message=? "
                        "WHERE id=?", (now_iso(), status, new_articles, new_trials, int(mail_sent), message, run_id))
        self.db.commit()

    def last_ok_run(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()

    def has_completed_run_on(self, day_iso: str) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE status='ok' AND substr(started_at, 1, 10)=?",
                               (day_iso,)).fetchone() is not None

    def runs(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # --- articles ---------------------------------------------------------------------------
    def known_pmids(self, pmids: Iterable[str]) -> set[str]:
        pmids = list(pmids)
        known: set[str] = set()
        for start in range(0, len(pmids), 500):
            batch = pmids[start:start + 500]
            marks = ",".join("?" * len(batch))
            known.update(r[0] for r in self.db.execute(f"SELECT pmid FROM articles WHERE pmid IN ({marks})", batch))
        return known

    def topics_of(self, pmid: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT topic_id FROM article_topics WHERE pmid=? ORDER BY rowid", (pmid,))]

    def insert_article(self, a: Article, section: str, run_id: int) -> None:
        ts = now_iso()
        self.db.execute(
            "INSERT INTO articles (pmid, title, abstract, authors, journal, journal_abbrev, pub_year, pub_date, "
            "entrez_date, pub_types, mesh, keywords, language, doi, pmcid, publication_status, volume, issue, "
            "bibl_checked_at, author_emails, kind, is_update, section, oa, url_fulltext, url_pdf, pdf_source, "
            "first_seen_at, first_seen_run, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.pmid, a.title, json.dumps(a.abstract, ensure_ascii=False), json.dumps(a.authors, ensure_ascii=False),
             a.journal, a.journal_abbrev, a.pub_year, a.pub_date, a.entrez_date,
             json.dumps(a.pub_types), json.dumps(a.mesh, ensure_ascii=False), json.dumps(a.keywords, ensure_ascii=False),
             a.language, a.doi, a.pmcid, a.publication_status, a.volume, a.issue, ts,
             json.dumps(a.author_emails, ensure_ascii=False), a.kind, int(a.is_update),
             section, int(a.oa),
             a.url_fulltext, a.url_pdf, a.pdf_source, ts, run_id, ts))

    def add_topics(self, pmid: str, topic_ids: Iterable[str], run_id: int) -> list[str]:
        """Links the article to topics; returns the topics that were new for it."""
        added = []
        for topic in topic_ids:
            cur = self.db.execute("INSERT OR IGNORE INTO article_topics (pmid, topic_id, first_seen_run) VALUES (?,?,?)",
                                  (pmid, topic, run_id))
            if cur.rowcount:
                added.append(topic)
        return added

    def set_section(self, pmid: str, section: str) -> None:
        self.db.execute("UPDATE articles SET section=? WHERE pmid=?", (section, pmid))

    def kind_of(self, pmid: str) -> str:
        row = self.db.execute("SELECT kind FROM articles WHERE pmid=?", (pmid,)).fetchone()
        return row[0] if row else "other"

    def pending_oa(self, since_iso: str, today_iso: str, force: bool = False) -> list[tuple[str, str, str, str]]:
        """(pmid, doi, pmcid, stored pdf url) of recent articles whose PDF link can still improve: none yet, or
        only a link for people. Back-off by the article's age: checked daily during the first 7 days, weekly up to
        30 days, then monthly (until `since_iso`). `force` ignores the back-off and the checked-today rule."""
        sql = ("SELECT pmid, doi, pmcid, url_pdf FROM articles "
               "WHERE pdf_source IN ('', 'europepmc', 'unpaywall-web') AND first_seen_at>=?")
        args: list = [since_iso]
        if not force:
            age = "(julianday(?) - julianday(substr(first_seen_at, 1, 10)))"
            since_check = "(julianday(?) - julianday(substr(oa_checked_at, 1, 10)))"
            sql += (f" AND (oa_checked_at='' OR {since_check} >= "
                    f"CASE WHEN {age} <= 7 THEN 1 WHEN {age} <= 30 THEN 7 ELSE 30 END)")
            args += [today_iso, today_iso, today_iso]
        return [(r[0], r[1], r[2], r[3]) for r in self.db.execute(sql, args)]

    def update_oa(self, pmid: str, links: OaLinks) -> bool:
        """Stores newly found links and provenance; a better PDF source replaces a weaker one (licence and version
        travel with the PDF they describe). True if something changed (then `updated_at` moves)."""
        row = self.db.execute("SELECT oa, pmcid, url_fulltext, url_pdf, pdf_source, oa_status, pdf_license, "
                              "pdf_version, openalex_id, preprint_id FROM articles WHERE pmid=?", (pmid,)).fetchone()
        if row is None:
            return False
        better_pdf = bool(links.url_pdf) and (PDF_SOURCE_RANK.get(links.pdf_source, 9)
                                              < PDF_SOURCE_RANK.get(row["pdf_source"], 9) or not row["url_pdf"])
        describes_pdf = better_pdf or (bool(links.url_pdf) and links.url_pdf == row["url_pdf"])
        new = (
            int(links.oa or row["oa"]),
            links.pmcid or row["pmcid"],
            row["url_fulltext"] or links.url_fulltext,
            links.url_pdf if better_pdf else row["url_pdf"],
            links.pdf_source if better_pdf else row["pdf_source"],
            # a definite OA status beats none; "closed" never overrides an open one found elsewhere
            links.oa_status if links.oa_status and (not row["oa_status"] or row["oa_status"] == "closed") else row["oa_status"],
            links.license if describes_pdf and links.license else row["pdf_license"],
            links.version if describes_pdf and links.version else row["pdf_version"],
            links.openalex_id or row["openalex_id"],
            links.preprint_id or row["preprint_id"],
        )
        if new == tuple(row):
            return False
        self.db.execute("UPDATE articles SET oa=?, pmcid=?, url_fulltext=?, url_pdf=?, pdf_source=?, oa_status=?, "
                        "pdf_license=?, pdf_version=?, openalex_id=?, preprint_id=?, updated_at=? WHERE pmid=?",
                        (*new, now_iso(), pmid))
        return True

    def missing_provenance(self, since_iso: str) -> list[tuple[str, str, str, str, str]]:
        """(pmid, doi, pmcid, url_pdf, pdf_source) of articles with a downloadable PDF but no licence or OA status yet
        (found before provenance was recorded)."""
        marks = ",".join("?" * len(AUTO_DOWNLOAD_SOURCES))
        rows = self.db.execute(f"SELECT pmid, doi, pmcid, url_pdf, pdf_source FROM articles WHERE pdf_source IN ({marks}) "
                               f"AND (pdf_license='' OR oa_status='') AND first_seen_at>=?", (*AUTO_DOWNLOAD_SOURCES, since_iso))
        return [tuple(r) for r in rows]

    def without_oa_status(self, pmids: Iterable[str]) -> list[str]:
        out = []
        for pmid in pmids:
            row = self.db.execute("SELECT oa_status FROM articles WHERE pmid=?", (pmid,)).fetchone()
            if row is not None and not row[0]:
                out.append(pmid)
        return out

    def pdf_links(self, pmids: Iterable[str]) -> dict[str, str]:
        """pmid -> the stored PDF link (for proxy sources the link a person can open)."""
        out: dict[str, str] = {}
        for pmid in pmids:
            row = self.db.execute("SELECT url_pdf FROM articles WHERE pmid=?", (pmid,)).fetchone()
            if row and row[0]:
                out[pmid] = row[0]
        return out

    def preprint_ids(self, pmids: Iterable[str]) -> dict[str, str]:
        """pmid -> Europe PMC preprint id, for articles whose preprint is not yet known to be downloadable."""
        out: dict[str, str] = {}
        for pmid in pmids:
            row = self.db.execute("SELECT preprint_id FROM articles WHERE pmid=? AND preprint_id<>'' "
                                  "AND preprint_source<>'preprint'", (pmid,)).fetchone()
            if row:
                out[pmid] = row[0]
        return out

    def update_preprint(self, pmid: str, links: PreprintLinks) -> bool:
        """Stores the preprint of an article. `updated_at` moves only when a downloadable preprint appears, so the
        next download run sees it."""
        row = self.db.execute("SELECT preprint_id, preprint_doi, preprint_pdf, preprint_source FROM articles "
                              "WHERE pmid=?", (pmid,)).fetchone()
        if row is None:
            return False
        if links.source != "preprint" and row["preprint_source"] == "preprint":
            return False  # keep the downloadable copy found earlier
        new = (links.preprint_id or row["preprint_id"], links.doi or row["preprint_doi"],
               links.url_pdf or row["preprint_pdf"], links.source or row["preprint_source"])
        if new == tuple(row):
            return False
        newly_downloadable = new[3] == "preprint" and row["preprint_source"] != "preprint"
        self.db.execute("UPDATE articles SET preprint_id=?, preprint_doi=?, preprint_pdf=?, preprint_source=?"
                        + (", updated_at=?" if newly_downloadable else "") + " WHERE pmid=?",
                        (*new, *((now_iso(),) if newly_downloadable else ()), pmid))
        return True

    def mark_oa_checked(self, pmids: Iterable[str]) -> None:
        ts = now_iso()
        self.db.executemany("UPDATE articles SET oa_checked_at=? WHERE pmid=?", [(ts, p) for p in pmids])

    def pending_bibliography(self, today_iso: str, limit: int = 400, horizon_days: int = 180,
                             every_days: int = 7) -> list[str]:
        """PMIDs whose citation data should be (re)read from PubMed: never read yet (older databases), or still
        without volume/issue, i.e. online ahead of print, re-read weekly for `horizon_days`."""
        today = date.fromisoformat(today_iso)
        rows = self.db.execute(
            "SELECT pmid FROM articles WHERE bibl_checked_at='' OR (volume='' AND issue='' AND "
            "substr(first_seen_at, 1, 10)>=? AND (julianday(?) - julianday(substr(bibl_checked_at, 1, 10))) >= ?) "
            "ORDER BY first_seen_at DESC LIMIT ?",
            ((today - timedelta(days=horizon_days)).isoformat(), today_iso, every_days, limit))
        return [r[0] for r in rows]

    def update_bibliography(self, a: Article) -> None:
        self.db.execute("UPDATE articles SET volume=?, issue=?, pub_date=?, pub_year=?, publication_status=?, "
                        "author_emails=?, bibl_checked_at=? WHERE pmid=?",
                        (a.volume, a.issue, a.pub_date, a.pub_year, a.publication_status,
                         json.dumps(a.author_emails, ensure_ascii=False), now_iso(), a.pmid))

    def mark_bibliography_checked(self, pmids: Iterable[str]) -> None:
        ts = now_iso()
        self.db.executemany("UPDATE articles SET bibl_checked_at=? WHERE pmid=?", [(ts, p) for p in pmids])

    # --- downloads ledger ---------------------------------------------------------------------
    def downloads_due(self, now: datetime, hours: int, retry_for_days: int, path_for,
                      sections: list[str] | None = None, kinds: list[str] | None = None,
                      preprints: bool = True) -> list[dict]:
        """What an automated download should be attempted for right now, newest first.

        Published version (vor), from a source a script may fetch:
        - new: never attempted, the PDF appeared (or was found) within `hours`;
        - retry: failed earlier and the monthly retry is due (within `retry_for_days` of the first attempt);
        - relinked: failed earlier, but the PDF link changed since, so it deserves a new try.
        Preprint (only when no downloadable published version exists and it was not downloaded): same rules.
        Anything downloaded once (`ok`) is never offered again. `sections` / `kinds` narrow the choice (empty = all).
        `path_for(article, version)` -> {"path", "final_path", "url"}."""
        stamp = lambda dt: dt.isoformat(timespec="seconds")  # noqa: E731
        marks = ",".join("?" * len(AUTO_DOWNLOAD_SOURCES))
        scope, scope_args = "", []
        if sections:
            scope += f" AND a.section IN ({','.join('?' * len(sections))})"
            scope_args += list(sections)
        if kinds:
            scope += f" AND a.kind IN ({','.join('?' * len(kinds))})"
            scope_args += list(kinds)
        window = stamp(now - timedelta(hours=hours))
        horizon = stamp(now - timedelta(days=retry_for_days))
        channels = [("vor", f"a.url_pdf<>'' AND a.pdf_source IN ({marks})", list(AUTO_DOWNLOAD_SOURCES))]
        if preprints:
            channels.append(("preprint",
                             f"a.preprint_source='preprint' AND a.preprint_pdf<>'' AND a.pdf_source NOT IN ({marks}) "
                             f"AND NOT EXISTS (SELECT 1 FROM downloads v WHERE v.pmid=a.pmid AND v.version='vor' "
                             f"AND v.status='ok')", list(AUTO_DOWNLOAD_SOURCES)))
        due: list[dict] = []
        for version, has_link, link_args in channels:
            for row in self.db.execute(
                    f"SELECT a.* FROM articles a LEFT JOIN downloads d ON d.pmid=a.pmid AND d.version=? "
                    f"WHERE d.pmid IS NULL AND {has_link} AND datetime(a.updated_at) >= datetime(?){scope} "
                    f"ORDER BY a.updated_at DESC, a.pmid", (version, *link_args, window, *scope_args)):
                due.append(self._with_download(row, version, "new", 0, "", path_for))
            for row in self.db.execute(
                    f"SELECT a.*, d.attempts AS d_attempts, d.last_error AS d_error, "
                    f"(datetime(d.next_retry_at) <= datetime(?)) AS d_monthly FROM downloads d "
                    f"JOIN articles a ON a.pmid=d.pmid WHERE d.version=? AND d.status='failed' AND {has_link} "
                    f"AND datetime(d.first_attempt_at) >= datetime(?) AND (datetime(d.next_retry_at) <= datetime(?) "
                    f"OR datetime(a.updated_at) > datetime(d.last_attempt_at)){scope} ORDER BY a.pmid",
                    (stamp(now), version, *link_args, horizon, stamp(now), *scope_args)):
                due.append(self._with_download(row, version, "retry" if row["d_monthly"] else "relinked",
                                               row["d_attempts"], row["d_error"], path_for))
        return due

    def _with_download(self, row: sqlite3.Row, version: str, reason: str, attempts: int, last_error: str,
                       path_for) -> dict:
        article = self.article_dict(row)
        for extra in ("d_attempts", "d_error", "d_monthly"):
            article.pop(extra, None)
        article["download"] = {"version": version, "reason": reason, "attempts": attempts, "last_error": last_error,
                               **path_for(article, version)}
        return article

    def _ledger_row(self, pmid: str, version: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM downloads WHERE pmid=? AND version=?", (pmid, version)).fetchone()

    def _ledger_write(self, values: dict) -> None:
        self.db.execute(f"INSERT OR REPLACE INTO downloads ({', '.join(DOWNLOAD_COLUMNS)}) "
                        f"VALUES ({', '.join('?' * len(DOWNLOAD_COLUMNS))})", [values[c] for c in DOWNLOAD_COLUMNS])

    def report_download(self, pmid: str, status: str, now: datetime, retry_every_days: int, path: str = "",
                        size: int | None = None, error: str = "", retry_in_days: int | None = None,
                        version: str = "vor") -> dict:
        """Records the outcome of one attempt. `ok` is final; `failed` schedules the next try: monthly by default
        (`retry_every_days`), or after `retry_in_days` for local problems such as an unwritable folder."""
        ts = now.isoformat(timespec="seconds")
        row = self._ledger_row(pmid, version)
        if row is not None and row["status"] == "ok":
            return dict(row)  # a late failure report must never undo a success
        values = {"pmid": pmid, "version": version, "attempts": (row["attempts"] if row else 0) + 1,
                  "first_attempt_at": row["first_attempt_at"] if row else ts, "last_attempt_at": ts, "source": "n8n"}
        if status == "ok":
            values.update(status="ok", next_retry_at="", last_error="", path=path, bytes=size, saved_at=ts)
        else:
            retry_at = (now + timedelta(days=retry_in_days or retry_every_days)).isoformat(timespec="seconds")
            values.update(status="failed", next_retry_at=retry_at, last_error=error[:300], path="", bytes=None,
                          saved_at="")
        self._ledger_write(values)
        self.db.commit()
        return dict(self._ledger_row(pmid, version))

    def reconcile_downloads(self, files: Iterable[tuple[str, str, str, int, str]], now: datetime) -> int:
        """Marks articles `ok` whose PDF is already in the PDF folder: (pmid, version, path, size, modified) as found
        by the file names. Returns how many were newly marked. Second protection against downloading twice,
        independent of n8n's reports."""
        ts = now.isoformat(timespec="seconds")
        marked = 0
        for pmid, version, relpath, size, modified in files:
            row = self._ledger_row(pmid, version)
            if row is not None and row["status"] == "ok":
                continue
            self._ledger_write({"pmid": pmid, "version": version, "status": "ok",
                                "attempts": row["attempts"] if row else 0,
                                "first_attempt_at": row["first_attempt_at"] if row else modified,
                                "last_attempt_at": ts, "next_retry_at": "", "last_error": "", "path": relpath,
                                "bytes": size, "saved_at": modified, "source": "disk"})
            marked += 1
        if marked:
            self.db.commit()
        return marked

    def downloads(self, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        sql = ("SELECT d.*, a.title, a.journal_abbrev, a.pdf_source, a.section FROM downloads d "
               "LEFT JOIN articles a ON a.pmid=d.pmid")
        args: list = []
        if status:
            sql += " WHERE d.status=?"
            args.append(status)
        sql += " ORDER BY d.last_attempt_at DESC, d.pmid LIMIT ? OFFSET ?"
        return [dict(r) for r in self.db.execute(sql, (*args, limit, offset))]

    def downloads_to_organize(self, only_pmid: str | None = None) -> list[tuple[str, str, str, dict]]:
        """(pmid, version, path the ledger says the PDF is at, article) for downloaded articles we know."""
        sql = ("SELECT a.*, d.path AS d_path, d.version AS d_version FROM downloads d JOIN articles a ON a.pmid=d.pmid "
               "WHERE d.status='ok' AND d.path<>''")
        args: list = []
        if only_pmid:
            sql += " AND d.pmid=?"
            args.append(only_pmid)
        out = []
        for row in self.db.execute(sql, args):
            current, version = row["d_path"], row["d_version"]
            article = self.article_dict(row)
            article.pop("d_path", None)
            article.pop("d_version", None)
            out.append((article["pmid"], version, current, article))
        return out

    def set_download_path(self, pmid: str, path: str, version: str = "vor") -> None:
        self.db.execute("UPDATE downloads SET path=? WHERE pmid=? AND version=?", (path, pmid, version))

    def has_article(self, pmid: str) -> bool:
        return self.db.execute("SELECT 1 FROM articles WHERE pmid=?", (pmid,)).fetchone() is not None

    def commit(self) -> None:
        self.db.commit()

    def article_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in JSON_ARTICLE:
            d[key] = json.loads(d[key])
        d["abstract"] = [{"label": label, "text": text} for label, text in d["abstract"]]
        d["is_update"] = bool(d["is_update"])
        d["oa"] = bool(d["oa"])
        d["topics"] = self.topics_of(d["pmid"])
        d["links"] = {
            "pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{d['pmid']}/",
            "doi": f"https://doi.org/{d['doi']}" if d["doi"] else "",
            "fulltext": d.pop("url_fulltext"),
            "pdf": d.pop("url_pdf"),
        }
        if "preprint_id" in d:
            d["preprint"] = {"id": d.pop("preprint_id"), "doi": d.pop("preprint_doi"), "pdf": d.pop("preprint_pdf"),
                             "source": d.pop("preprint_source")}
            d["links"]["preprint_pdf"] = d["preprint"]["pdf"]
        return d

    def articles(self, *, run_id: int | None = None, since: str | None = None, updated_since: str | None = None,
                 topic: str | None = None, section: str | None = None, kind: str | None = None,
                 has_pdf: bool | None = None, entrez_since: str | None = None, pdf_source: str | None = None,
                 limit: int = 100, offset: int = 0) -> list[dict]:
        where, args = [], []
        if run_id is not None:
            where.append("a.first_seen_run=?"); args.append(run_id)
        if entrez_since:
            where.append("a.entrez_date>=?"); args.append(entrez_since)
        if pdf_source:
            where.append("a.pdf_source=?"); args.append(pdf_source)
        if since:
            where.append("a.first_seen_at>=?"); args.append(since)
        if updated_since:
            where.append("a.updated_at>=?"); args.append(updated_since)
        if topic:
            where.append("EXISTS (SELECT 1 FROM article_topics t WHERE t.pmid=a.pmid AND t.topic_id=?)"); args.append(topic)
        if section:
            where.append("a.section=?"); args.append(section)
        if kind:
            where.append("a.kind=?"); args.append(kind)
        if has_pdf is not None:
            where.append("a.url_pdf<>''" if has_pdf else "a.url_pdf=''")
        sql = "SELECT * FROM articles a" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY a.first_seen_at DESC, a.pmid DESC LIMIT ? OFFSET ?"
        return [self.article_dict(r) for r in self.db.execute(sql, (*args, limit, offset))]

    def article(self, pmid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM articles WHERE pmid=?", (pmid,)).fetchone()
        return self.article_dict(row) if row else None

    # --- trials -----------------------------------------------------------------------------
    def known_trials(self, nct_ids: Iterable[str]) -> set[str]:
        ids = list(nct_ids)
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0] for r in self.db.execute(f"SELECT nct_id FROM trials WHERE nct_id IN ({marks})", ids)}

    def insert_trial(self, t: Trial, run_id: int) -> None:
        ts = now_iso()
        self.db.execute(
            "INSERT INTO trials (nct_id, title, official_title, status, study_type, phases, sponsor, countries, enrollment, "
            "conditions, interventions, min_age, max_age, start_date, primary_completion, first_posted, last_update, "
            "summary, first_seen_at, first_seen_run, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.nct_id, t.title, t.official_title, t.status, t.study_type, json.dumps(t.phases), t.sponsor,
             json.dumps(t.countries, ensure_ascii=False), t.enrollment, json.dumps(t.conditions, ensure_ascii=False),
             json.dumps(t.interventions, ensure_ascii=False), t.min_age, t.max_age, t.start_date, t.primary_completion,
             t.first_posted, t.last_update, t.summary, ts, run_id, ts))

    @staticmethod
    def trial_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in JSON_TRIAL:
            d[key] = json.loads(d[key])
        d["url"] = f"https://clinicaltrials.gov/study/{d['nct_id']}"
        return d

    def trials(self, *, run_id: int | None = None, since: str | None = None, status: str | None = None,
               posted_since: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        where, args = [], []
        if posted_since:
            where.append("first_posted>=?"); args.append(posted_since)
        if run_id is not None:
            where.append("first_seen_run=?"); args.append(run_id)
        if since:
            where.append("first_seen_at>=?"); args.append(since)
        if status:
            where.append("status=?"); args.append(status)
        sql = "SELECT * FROM trials" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY first_seen_at DESC, nct_id LIMIT ? OFFSET ?"
        return [self.trial_dict(r) for r in self.db.execute(sql, (*args, limit, offset))]

    def trial(self, nct_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM trials WHERE nct_id=?", (nct_id,)).fetchone()
        return self.trial_dict(row) if row else None

    def counts(self) -> dict[str, int]:
        return {
            "articles": self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "articles_with_pdf": self.db.execute("SELECT COUNT(*) FROM articles WHERE url_pdf<>''").fetchone()[0],
            "trials": self.db.execute("SELECT COUNT(*) FROM trials").fetchone()[0],
            "runs": self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "downloads_ok": self.db.execute("SELECT COUNT(*) FROM downloads WHERE status='ok'").fetchone()[0],
            "downloads_failed": self.db.execute("SELECT COUNT(*) FROM downloads WHERE status='failed'").fetchone()[0],
        }
