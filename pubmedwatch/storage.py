"""Canonical SQLite store: one row per article / trial, topic links, run history.

The API, the report and the n8n webhook all read the same dictionaries (see `article_dict`).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import Article, Trial
from .openaccess import PDF_SOURCE_RANK, OaLinks

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
            "entrez_date, pub_types, mesh, keywords, language, doi, pmcid, publication_status, kind, is_update, "
            "section, oa, url_fulltext, url_pdf, pdf_source, first_seen_at, first_seen_run, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.pmid, a.title, json.dumps(a.abstract, ensure_ascii=False), json.dumps(a.authors, ensure_ascii=False),
             a.journal, a.journal_abbrev, a.pub_year, a.pub_date, a.entrez_date,
             json.dumps(a.pub_types), json.dumps(a.mesh, ensure_ascii=False), json.dumps(a.keywords, ensure_ascii=False),
             a.language, a.doi, a.pmcid, a.publication_status, a.kind, int(a.is_update), section, int(a.oa),
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

    def pending_oa(self, since_iso: str, today_iso: str, force: bool = False) -> list[tuple[str, str, str]]:
        """(pmid, doi, pmcid) of recent articles whose PDF link can still improve (none yet, or only the
        Europe PMC web link); skips the ones already checked today unless `force`."""
        sql = ("SELECT pmid, doi, pmcid FROM articles WHERE pdf_source IN ('', 'europepmc') AND first_seen_at>=?")
        args: list = [since_iso]
        if not force:
            sql += " AND substr(oa_checked_at, 1, 10)<>?"
            args.append(today_iso)
        return [(r[0], r[1], r[2]) for r in self.db.execute(sql, args)]

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
        }
