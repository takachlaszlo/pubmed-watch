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
from .openaccess import AUTO_DOWNLOAD_SOURCES, PDF_SOURCE_RANK, OaLinks

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
    kind TEXT NOT NULL DEFAULT 'other',
    is_update INTEGER NOT NULL DEFAULT 0,
    section TEXT NOT NULL DEFAULT '',
    oa INTEGER NOT NULL DEFAULT 0,
    url_fulltext TEXT NOT NULL DEFAULT '',
    url_pdf TEXT NOT NULL DEFAULT '',
    pdf_source TEXT NOT NULL DEFAULT '',
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
    pmid TEXT PRIMARY KEY,
    status TEXT NOT NULL,                       -- ok | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    first_attempt_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL DEFAULT '',     -- failed rows: when the next monthly try is due
    last_error TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    bytes INTEGER,
    saved_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''             -- n8n (reported) | disk (found in the PDF folder)
);
"""

JSON_ARTICLE = ("abstract", "authors", "pub_types", "mesh", "keywords")
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
        for name in ("volume", "issue", "bibl_checked_at"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
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
            "bibl_checked_at, kind, is_update, section, oa, url_fulltext, url_pdf, pdf_source, first_seen_at, "
            "first_seen_run, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.pmid, a.title, json.dumps(a.abstract, ensure_ascii=False), json.dumps(a.authors, ensure_ascii=False),
             a.journal, a.journal_abbrev, a.pub_year, a.pub_date, a.entrez_date,
             json.dumps(a.pub_types), json.dumps(a.mesh, ensure_ascii=False), json.dumps(a.keywords, ensure_ascii=False),
             a.language, a.doi, a.pmcid, a.publication_status, a.volume, a.issue, ts, a.kind, int(a.is_update),
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
        """Stores newly found links; a better PDF source replaces a weaker one. True if something changed."""
        row = self.db.execute("SELECT oa, pmcid, url_fulltext, url_pdf, pdf_source FROM articles WHERE pmid=?",
                              (pmid,)).fetchone()
        if row is None:
            return False
        better_pdf = bool(links.url_pdf) and (PDF_SOURCE_RANK.get(links.pdf_source, 9)
                                              < PDF_SOURCE_RANK.get(row["pdf_source"], 9) or not row["url_pdf"])
        url_pdf = links.url_pdf if better_pdf else row["url_pdf"]
        pdf_source = links.pdf_source if better_pdf else row["pdf_source"]
        new = (int(links.oa or row["oa"]), links.pmcid or row["pmcid"], row["url_fulltext"] or links.url_fulltext,
               url_pdf, pdf_source)
        if new == tuple(row):
            return False
        self.db.execute("UPDATE articles SET oa=?, pmcid=?, url_fulltext=?, url_pdf=?, pdf_source=?, updated_at=? "
                        "WHERE pmid=?", (*new, now_iso(), pmid))
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
                        "bibl_checked_at=? WHERE pmid=?",
                        (a.volume, a.issue, a.pub_date, a.pub_year, a.publication_status, now_iso(), a.pmid))

    def mark_bibliography_checked(self, pmids: Iterable[str]) -> None:
        ts = now_iso()
        self.db.executemany("UPDATE articles SET bibl_checked_at=? WHERE pmid=?", [(ts, p) for p in pmids])

    # --- downloads ledger ---------------------------------------------------------------------
    def downloads_due(self, now: datetime, hours: int, retry_for_days: int, path_for) -> list[dict]:
        """Articles an automated download should be attempted for right now.

        - new: a PDF link a script may fetch, never attempted, that appeared (or was found) within `hours`;
        - retry: attempted and failed, and the monthly retry is due (within `retry_for_days` of the first try);
        - relinked: failed earlier, but the PDF link changed since the last attempt, so it deserves a new try.
        Anything already downloaded (`ok`) is never offered again."""
        marks = ",".join("?" * len(AUTO_DOWNLOAD_SOURCES))
        stamp = lambda dt: dt.isoformat(timespec="seconds")  # noqa: E731
        # path_for(article) -> {"path": where n8n saves it (inbox), "final_path": where it ends up}
        due: list[dict] = []
        new_rows = self.db.execute(
            f"SELECT a.* FROM articles a LEFT JOIN downloads d ON d.pmid=a.pmid WHERE d.pmid IS NULL "
            f"AND a.url_pdf<>'' AND a.pdf_source IN ({marks}) AND datetime(a.updated_at) >= datetime(?) "
            f"ORDER BY a.pmid", (*AUTO_DOWNLOAD_SOURCES, stamp(now - timedelta(hours=hours))))
        for row in new_rows:
            due.append(self._with_download(row, "new", 0, "", path_for))
        retry_rows = self.db.execute(
            f"SELECT a.*, d.attempts AS d_attempts, d.last_error AS d_error, "
            f"(datetime(d.next_retry_at) <= datetime(?)) AS d_monthly "
            f"FROM downloads d JOIN articles a ON a.pmid=d.pmid WHERE d.status='failed' AND a.url_pdf<>'' "
            f"AND a.pdf_source IN ({marks}) AND datetime(d.first_attempt_at) >= datetime(?) "
            f"AND (datetime(d.next_retry_at) <= datetime(?) OR datetime(a.updated_at) > datetime(d.last_attempt_at)) "
            f"ORDER BY a.pmid", (stamp(now), *AUTO_DOWNLOAD_SOURCES, stamp(now - timedelta(days=retry_for_days)),
                                 stamp(now)))
        for row in retry_rows:
            due.append(self._with_download(row, "retry" if row["d_monthly"] else "relinked", row["d_attempts"],
                                           row["d_error"], path_for))
        return due

    def _with_download(self, row: sqlite3.Row, reason: str, attempts: int, last_error: str, path_for) -> dict:
        article = self.article_dict(row)
        for extra in ("d_attempts", "d_error", "d_monthly"):
            article.pop(extra, None)
        article["download"] = {"reason": reason, "attempts": attempts, "last_error": last_error,
                               **path_for(article)}
        return article

    def report_download(self, pmid: str, status: str, now: datetime, retry_every_days: int, path: str = "",
                        size: int | None = None, error: str = "", retry_in_days: int | None = None) -> dict:
        """Records the outcome of one attempt. `ok` is final; `failed` schedules the next try: monthly by default
        (`retry_every_days`), or after `retry_in_days` for local problems such as an unwritable folder."""
        ts = now.isoformat(timespec="seconds")
        row = self.db.execute("SELECT * FROM downloads WHERE pmid=?", (pmid,)).fetchone()
        if row is not None and row["status"] == "ok":
            return dict(row)  # a late failure report must never undo a success
        attempts = (row["attempts"] if row else 0) + 1
        first = row["first_attempt_at"] if row else ts
        if status == "ok":
            values = ("ok", attempts, first, ts, "", "", path, size, ts, "n8n")
        else:
            retry_at = (now + timedelta(days=retry_in_days or retry_every_days)).isoformat(timespec="seconds")
            values = ("failed", attempts, first, ts, retry_at, error[:300], "", None, "", "n8n")
        self.db.execute(
            "INSERT OR REPLACE INTO downloads (pmid, status, attempts, first_attempt_at, last_attempt_at, "
            "next_retry_at, last_error, path, bytes, saved_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pmid, *values))
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM downloads WHERE pmid=?", (pmid,)).fetchone())

    def reconcile_downloads(self, files: Iterable[tuple[str, str, int, str]], now: datetime) -> int:
        """Marks articles `ok` whose PDF is already in the PDF folder (name carries the PMID). Returns how many
        were newly marked. Second protection against downloading something twice, independent of n8n's reports."""
        ts = now.isoformat(timespec="seconds")
        marked = 0
        for pmid, relpath, size, modified in files:
            row = self.db.execute("SELECT status, attempts, first_attempt_at FROM downloads WHERE pmid=?",
                                  (pmid,)).fetchone()
            if row is not None and row["status"] == "ok":
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO downloads (pmid, status, attempts, first_attempt_at, last_attempt_at, "
                "next_retry_at, last_error, path, bytes, saved_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pmid, "ok", row["attempts"] if row else 0, row["first_attempt_at"] if row else modified, ts, "", "",
                 relpath, size, modified, "disk"))
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

    def downloads_to_organize(self, only_pmid: str | None = None) -> list[tuple[str, str, dict]]:
        """(pmid, path the ledger says the PDF is at, article) for downloaded articles we know the citation of."""
        sql = ("SELECT a.*, d.path AS d_path FROM downloads d JOIN articles a ON a.pmid=d.pmid "
               "WHERE d.status='ok' AND d.path<>''")
        args: list = []
        if only_pmid:
            sql += " AND d.pmid=?"
            args.append(only_pmid)
        out = []
        for row in self.db.execute(sql, args):
            current = row["d_path"]
            article = self.article_dict(row)
            article.pop("d_path", None)
            out.append((article["pmid"], current, article))
        return out

    def set_download_path(self, pmid: str, path: str) -> None:
        self.db.execute("UPDATE downloads SET path=? WHERE pmid=?", (path, pmid))

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
