"""Article type, "updated guideline" flag and which report section an article belongs to."""
from __future__ import annotations

import re

from .config import GUIDELINE_SECTION, Config
from .models import Article

GUIDELINE_PT = {"Guideline", "Practice Guideline", "Consensus Development Conference",
                "Consensus Development Conference, NIH"}
SR_PT = {"Systematic Review", "Meta-Analysis"}
RCT_PT = {"Randomized Controlled Trial"}
# keep these in step with the `guideline` / `systematic_review` / `trial_protocol` blocks in config.yaml
GUIDELINE_TI = re.compile(
    r"guideline|consensus (statement|guideline|recommendation|document|report)|expert consensus|consensus-based"
    r"|position (statement|paper)|practice parameter|recommendations (for|on)\b", re.I)
# studies *about* guidelines (adherence audits etc.) are not guidelines themselves
ABOUT_GUIDELINE_TI = re.compile(
    r"\b(adheren\w*|complian\w*|concordan\w*|implement\w*|uptake|awareness|knowledge|audit\w*|deviation\w*"
    r"|followed|following|impact of)\b[^:]{0,60}\bguideline", re.I)
SR_TI = re.compile(r"systematic review|meta-analys[ie]s|scoping review|umbrella review", re.I)
PROTOCOL_TI = re.compile(r"\bprotocol\b", re.I)
STUDY_PROTOCOL_TI = re.compile(r"\b(study|trial) protocol\b", re.I)
TRIAL_WORDS = re.compile(r"\btrial\b|randomi[sz]", re.I)
UPDATE_TI = re.compile(r"\b(update[sd]?|revised|revision)\b", re.I)


def kind_of(article: Article) -> str:
    pt = set(article.pub_types)
    title = article.title
    if pt & GUIDELINE_PT or (GUIDELINE_TI.search(title) and not ABOUT_GUIDELINE_TI.search(title)):
        return "guideline"
    if pt & SR_PT or SR_TI.search(title):
        return "systematic_review"
    if ("Clinical Trial Protocol" in pt or STUDY_PROTOCOL_TI.search(title)
            or (PROTOCOL_TI.search(title) and TRIAL_WORDS.search(title + " " + article.abstract_text))):
        return "protocol"
    if pt & RCT_PT:
        return "rct"
    if "Review" in pt:
        return "review"
    return "other"


def is_update(article: Article) -> bool:
    return bool(UPDATE_TI.search(article.title))


def classify(article: Article) -> Article:
    article.kind = kind_of(article)
    article.is_update = is_update(article)
    return article


def section_of(cfg: Config, kind: str, topic_ids: list[str] | set[str]) -> str:
    """Guidelines get their own section; otherwise the first matching topic (config order) decides."""
    if kind == "guideline" and cfg.section(GUIDELINE_SECTION):
        return GUIDELINE_SECTION
    for topic in cfg.topics:
        if topic.id in topic_ids:
            return topic.section
    return cfg.topics[-1].section
