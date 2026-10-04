"""Configuration: config.yaml for what to watch, environment for how to deliver."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
    digest_on_start_days: int  # 0 = off; N = once per distinct value, mail the last N days from the database

    @classmethod
    def from_env(cls) -> "ScheduleConfig":
        run_at = _env("RUN_AT", "06:30")
        if not re.fullmatch(r"\d{1,2}:\d{2}", run_at):
            raise ValueError(f"RUN_AT formátuma ÓÓ:PP legyen, nem {run_at!r}")
        return cls(run_at=run_at, run_on_start=_env_bool("RUN_ON_START", False),
                   catch_up=_env_bool("CATCH_UP", True), digest_on_start_days=int(_env("DIGEST_ON_START_DAYS", "0") or 0))


@dataclass
class ApiConfig:
    port: int  # 0 = disabled
    token: str  # empty = no authentication (LAN only)
    webhook_url: str  # n8n webhook called after each run; empty = off

    @classmethod
    def from_env(cls) -> "ApiConfig":
        return cls(port=int(_env("API_PORT", "8765") or 0), token=_env("API_TOKEN"),
                   webhook_url=_env("N8N_WEBHOOK_URL"))


@dataclass
class SourceConfig:
    ncbi_api_key: str
    ncbi_email: str
    unpaywall_email: str  # empty = Unpaywall lookup off
    user_agent: str
    timeout: float

    @classmethod
    def from_env(cls) -> "SourceConfig":
        return cls(
            ncbi_api_key=_env("NCBI_API_KEY"),
            ncbi_email=_env("NCBI_EMAIL"),
            unpaywall_email=_env("UNPAYWALL_EMAIL"),
            user_agent=_env("USER_AGENT", "pubmed-watch/1.0 (+https://github.com/takachlaszlo/pubmed-watch)"),
            timeout=float(_env("HTTP_TIMEOUT", "60")),
        )


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
class Config:
    topics: list[Topic]
    sections: list[Section]
    trials: TrialsConfig
    lookback_days: int
    baseline_days: int
    oa_recheck_days: int
    baseline_digest_days: int
    send_empty: bool
    data_dir: Path
    mail: MailConfig = field(default_factory=MailConfig.from_env)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig.from_env)
    api: ApiConfig = field(default_factory=ApiConfig.from_env)
    sources: SourceConfig = field(default_factory=SourceConfig.from_env)

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
    return Config(
        topics=topics,
        sections=sections,
        trials=trials,
        lookback_days=int(raw.get("lookback_days", 2)),
        baseline_days=int(raw.get("baseline_days", 7)),
        oa_recheck_days=int(raw.get("oa_recheck_days", 30)),
        baseline_digest_days=int(raw.get("baseline_digest_days", 2)),
        send_empty=bool(raw.get("send_empty", False)),
        data_dir=Path(_env("PUBMEDWATCH_DATA", "/data")),
    )
