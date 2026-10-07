"""Configuration: config.yaml for what to watch, environment for how to deliver."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .downloads import DEFAULT_INBOX, FOLDER_KINDS

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
SECTION_STYLES = ("full", "compact", "trials")
GUIDELINE_SECTION = "iranyelvek"
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on", "igen")


@dataclass
class MailConfig:
    host: str
    port: int
    security: str  # none | starttls | ssl
    user: str
    password: str
    verify_tls: bool
    sender: str
    recipients: list[str]
    sender_name: str

    @classmethod
    def from_env(cls) -> "MailConfig":
        sender = _env("MAIL_FROM", "technikai@mail.home.arpa")
        recipients = [r.strip() for r in _env("MAIL_TO", sender).replace(";", ",").split(",") if r.strip()]
        return cls(
            host=_env("SMTP_HOST", "mail.home.arpa"),
            port=int(_env("SMTP_PORT", "25")),
            security=_env("SMTP_SECURITY", "none").lower(),
            user=_env("SMTP_USER"),
            password=_env("SMTP_PASSWORD"),
            verify_tls=_env_bool("SMTP_VERIFY_TLS", False),
            sender=sender,
            recipients=recipients,
            sender_name=_env("MAIL_FROM_NAME", "PubMed-figyelő"),
        )


@dataclass
class ScheduleConfig:
    run_at: str  # HH:MM local time
    run_on_start: bool
    catch_up: bool
    link_refresh_on_start: str  # any new value forces one full PDF-link refresh at start (e.g. after adding a source)
    digest_on_start_days: int  # 0 = off; N = once per distinct value, mail the last N days from the database

    @classmethod
    def from_env(cls) -> "ScheduleConfig":
        run_at = _env("RUN_AT", "06:30")
        if not re.fullmatch(r"\d{1,2}:\d{2}", run_at):
            raise ValueError(f"RUN_AT formátuma ÓÓ:PP legyen, nem {run_at!r}")
        return cls(run_at=run_at, run_on_start=_env_bool("RUN_ON_START", False),
                   catch_up=_env_bool("CATCH_UP", True), link_refresh_on_start=_env("LINK_REFRESH_ON_START"),
                   digest_on_start_days=int(_env("DIGEST_ON_START_DAYS", "0") or 0))


@dataclass
class UpdateConfig:
    enabled: bool  # restart for a newer version on GitHub, once a day before the daily run
    repo: str  # owner/name on GitHub, the one compose.yaml downloads
    branch: str

    @classmethod
    def from_env(cls) -> "UpdateConfig":
        return cls(enabled=_env_bool("AUTO_UPDATE", True), repo=_env("UPDATE_REPO", "takachlaszlo/pubmed-watch"),
                   branch=_env("UPDATE_BRANCH", "main"))


@dataclass
class ApiConfig:
    port: int  # 0 = disabled
    token: str  # empty = no authentication (LAN only)
    webhook_url: str  # n8n webhook called after each run; empty = off
    pdf_dir: Path  # the folder the PDFs are saved to, as this container sees it
    public_url: str  # how n8n reaches this API (used in download links that go through the service)

    @classmethod
    def from_env(cls) -> "ApiConfig":
        return cls(port=int(_env("API_PORT", "8765") or 0), token=_env("API_TOKEN"),
                   webhook_url=_env("N8N_WEBHOOK_URL"), pdf_dir=Path(_env("PDF_DIR", "/pdfs")),
                   public_url=_env("API_PUBLIC_URL", "http://192.168.1.168:8765").rstrip("/"))


@dataclass
class SourceConfig:
    ncbi_api_key: str
    ncbi_email: str
    unpaywall_email: str  # empty = Unpaywall lookup off
    user_agent: str
    timeout: float
    # optional keys: each source is simply skipped while its key is empty
    openalex_api_key: str = ""
    elsevier_api_key: str = ""
    elsevier_insttoken: str = ""
    wiley_tdm_token: str = ""
    core_api_key: str = ""

    @classmethod
    def from_env(cls) -> "SourceConfig":
        return cls(
            ncbi_api_key=_env("NCBI_API_KEY"),
            ncbi_email=_env("NCBI_EMAIL"),
            unpaywall_email=_env("UNPAYWALL_EMAIL"),
            openalex_api_key=_env("OPENALEX_API_KEY"),
            elsevier_api_key=_env("ELSEVIER_API_KEY"),
            elsevier_insttoken=_env("ELSEVIER_INSTTOKEN"),
            wiley_tdm_token=_env("WILEY_TDM_TOKEN"),
            core_api_key=_env("CORE_API_KEY"),
            user_agent=_env("USER_AGENT", "pubmed-watch/1.0 (+https://github.com/takachlaszlo/pubmed-watch)"),
            timeout=float(_env("HTTP_TIMEOUT", "60")),
        )


@dataclass
class ReportConfig:
    library_link: str  # e.g. "https://proxy.example.org/login?url=https://doi.org/{doi}"; empty = no library link
    request_signature: str  # closing lines of the "ask the author" e-mail; empty = none

    @classmethod
    def from_env(cls) -> "ReportConfig":
        return cls(library_link=_env("LIBRARY_LINK_TEMPLATE"),
                   request_signature=_env("REQUEST_SIGNATURE").replace("\\n", "\n"))


@dataclass
class Topic:
    id: str
    label: str
    section: str
    query: str  # fully expanded PubMed query


@dataclass
class Section:
    id: str
    title: str
    style: str
    subtitle: str = ""


@dataclass
class TrialsConfig:
    enabled: bool
    max_age: str
    statuses: list[str]
    conditions: str
    terms: str


@dataclass
class DownloadsConfig:
    window_hours: int  # new / newly linked articles are offered for download within this many hours
    retry_every_days: int  # a failed download is tried again this often ...
    retry_for_days: int  # ... until this many days after the first attempt
    folders: list[str]  # PDF folder levels, outermost first: section | journal | issue | year
    inbox: str  # flat folder n8n saves into; the service files the PDFs from here
    sections: list[str]  # report sections whose PDFs are downloaded; empty = all
    kinds: list[str]  # article kinds to download (guideline, systematic_review, ...); empty = all
    preprints: bool  # also download the preprint of an article that has no downloadable published version
    max_retries_per_run: int  # earlier failures offered again in one run, longest-waiting first (0 = no limit)


@dataclass
class Config:
    topics: list[Topic]
    sections: list[Section]
    trials: TrialsConfig
    downloads: DownloadsConfig
    lookback_days: int
    baseline_days: int
    oa_recheck_days: int
    baseline_digest_days: int
    send_empty: bool
    data_dir: Path
    mail: MailConfig = field(default_factory=MailConfig.from_env)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig.from_env)
    update: UpdateConfig = field(default_factory=UpdateConfig.from_env)
    api: ApiConfig = field(default_factory=ApiConfig.from_env)
    sources: SourceConfig = field(default_factory=SourceConfig.from_env)
    report: ReportConfig = field(default_factory=ReportConfig.from_env)

    def topic(self, topic_id: str) -> Topic | None:
        return next((t for t in self.topics if t.id == topic_id), None)

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)


def expand(template: str, blocks: dict[str, str], _depth: int = 0) -> str:
    """Replace {name} placeholders with building blocks (blocks may reference each other)."""
    if _depth > 5:
        raise ValueError("túl mély (vagy körkörös) hivatkozás a blocks között")

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in blocks:
            raise ValueError(f"ismeretlen építőkő a lekérdezésben: {{{name}}}")
        return expand(blocks[name], blocks, _depth + 1)

    return " ".join(_PLACEHOLDER.sub(sub, template).split())


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or _env("PUBMEDWATCH_CONFIG") or DEFAULT_CONFIG)
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    blocks = {k: str(v) for k, v in (raw.get("blocks") or {}).items()}

    sections = [Section(id=s["id"], title=s["title"], style=s.get("style", "full"), subtitle=s.get("subtitle", ""))
                for s in raw.get("sections") or []]
    section_ids = {s.id for s in sections}
    for s in sections:
        if s.style not in SECTION_STYLES:
            raise ValueError(f"ismeretlen szekció-stílus: {s.style} ({s.id})")

    topics = []
    for t in raw.get("topics") or []:
        if t["section"] not in section_ids:
            raise ValueError(f"a(z) {t['id']} téma ismeretlen szekcióra mutat: {t['section']}")
        topics.append(Topic(id=t["id"], label=t.get("label", t["id"]), section=t["section"],
                            query=expand(t["query"], blocks)))
    if not topics:
        raise ValueError("a config.yaml nem tartalmaz témát")

    ct = raw.get("clinicaltrials") or {}
    trials = TrialsConfig(
        enabled=bool(ct.get("enabled", False)),
        max_age=str(ct.get("max_age", "21 years")),
        statuses=list(ct.get("statuses") or []),
        conditions=" ".join(str(ct.get("conditions", "")).split()),
        terms=" ".join(str(ct.get("terms", "")).split()),
    )
    dl = raw.get("downloads") or {}
    downloads = DownloadsConfig(
        window_hours=int(dl.get("window_hours", 24)),
        retry_every_days=int(dl.get("retry_every_days", 30)),
        retry_for_days=int(dl.get("retry_for_days", 365)),
        folders=[str(f) for f in dl.get("folders", ["journal", "issue"])],
        inbox=str(dl.get("inbox", DEFAULT_INBOX)),
        sections=[str(x) for x in dl.get("sections", [])],
        kinds=[str(x) for x in dl.get("kinds", [])],
        preprints=bool(dl.get("preprints", True)),
        max_retries_per_run=int(dl.get("max_retries_per_run", 100)),
    )
    for section_id in downloads.sections:
        if section_id not in section_ids:
            raise ValueError(f"ismeretlen szekció a downloads.sections-ben: {section_id}")
    for folder in downloads.folders:
        if folder not in FOLDER_KINDS:
            raise ValueError(f"ismeretlen mappa-típus a downloads.folders-ben: {folder} (lehet: {', '.join(FOLDER_KINDS)})")
    return Config(
        topics=topics,
        sections=sections,
        trials=trials,
        downloads=downloads,
        lookback_days=int(raw.get("lookback_days", 2)),
        baseline_days=int(raw.get("baseline_days", 7)),
        oa_recheck_days=int(raw.get("oa_recheck_days", 365)),
        baseline_digest_days=int(raw.get("baseline_digest_days", 2)),
        send_empty=bool(raw.get("send_empty", False)),
        data_dir=Path(_env("PUBMEDWATCH_DATA", "/data")),
    )
