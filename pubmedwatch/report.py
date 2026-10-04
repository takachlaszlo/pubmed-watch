"""Daily e-mail: a clean, single-column HTML digest (inline styles for mail clients) plus plain text."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date

from .config import GUIDELINE_SECTION, Config, Section

MONTHS_HU = ("január", "február", "március", "április", "május", "június", "július", "augusztus",
             "szeptember", "október", "november", "december")
WEEKDAYS_HU = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
PT_HU = {"Practice Guideline": "Irányelv", "Guideline": "Irányelv", "Consensus Development Conference": "Konszenzus",
         "Systematic Review": "Szisztematikus review", "Meta-Analysis": "Metaanalízis",
         "Randomized Controlled Trial": "RCT", "Clinical Trial Protocol": "Protokoll",
         "Multicenter Study": "Multicentrikus", "Observational Study": "Obszervációs"}
KIND_HU = {"guideline": "Irányelv", "systematic_review": "Szisztematikus review", "protocol": "Protokoll", "rct": "RCT"}
STATUS_HU = {"RECRUITING": "Toboroz", "NOT_YET_RECRUITING": "Még nem toboroz",
             "ACTIVE_NOT_RECRUITING": "Aktív, nem toboroz", "ENROLLING_BY_INVITATION": "Meghívásos"}
PHASE_HU = {"EARLY_PHASE1": "Korai I. fázis", "PHASE1": "I. fázis", "PHASE2": "II. fázis", "PHASE3": "III. fázis",
            "PHASE4": "IV. fázis"}
STUDY_TYPE_HU = {"INTERVENTIONAL": "Intervenciós", "OBSERVATIONAL": "Obszervációs", "EXPANDED_ACCESS": "Kiterjesztett hozzáférés"}
CONCLUSION_LABEL = re.compile(r"^(CONCLUSION|INTERPRETATION|IMPLICATION)", re.I)

C = dict(ink="#1f2328", muted="#656d76", line="#e6e8eb", accent="#0b5c63", bg="#f6f7f8", tag="#eef4f4",
         warn="#8a4b00", warnbg="#fdf3e6")
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


@dataclass
class Digest:
    subject: str
    html: str
    text: str


def hu_date(d: date) -> str:
    return f"{d.year}. {MONTHS_HU[d.month - 1]} {d.day}., {WEEKDAYS_HU[d.weekday()]}"


def conclusion(abstract: list[dict], limit: int = 340) -> str:
    """The conclusion of a structured abstract, else the last two sentences."""
    if not abstract:
        return ""
    labelled = next((p["text"] for p in abstract if CONCLUSION_LABEL.match(p["label"] or "")), None)
    if labelled is None:
        sentences = re.split(r"(?<=[.!?])\s+", abstract[-1]["text"].strip())
        labelled = " ".join(sentences[-2:])
    text = labelled.strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def first_author(authors: list[str]) -> str:
    return "" if not authors else authors[0] + (" et al." if len(authors) > 1 else "")


def type_labels(a: dict) -> list[str]:
    labels = []
    for p in a["pub_types"]:
        h = PT_HU.get(p)
        if h and h not in labels:
            labels.append(h)
    kind_label = KIND_HU.get(a["kind"])
    if kind_label and kind_label not in labels and not (kind_label == "Irányelv" and "Konszenzus" in labels):
        labels.append(kind_label)
    return labels


def group(cfg: Config, articles: list[dict], trials: list[dict]) -> dict[str, dict]:
    groups = {s.id: {"articles": [], "trials": []} for s in cfg.sections}
    for a in articles:
        groups.setdefault(a["section"], {"articles": [], "trials": []})["articles"].append(a)
    trial_section = next((s.id for s in cfg.sections if s.style == "trials"), None)
    if trial_section:
        groups[trial_section]["trials"] = trials
    for g in groups.values():  # guidelines flagged as updates first, then by journal
        g["articles"].sort(key=lambda a: (not a["is_update"], a["journal_abbrev"].lower(), a["title"].lower()))
    return groups


# --- HTML ------------------------------------------------------------------------------------
def _e(value) -> str:
    return html.escape(str(value or ""))


def _tag(text: str, warn: bool = False) -> str:
    fg, bg = (C["warn"], C["warnbg"]) if warn else (C["accent"], C["tag"])
    return (f'<span style="display:inline-block;font-size:11px;line-height:16px;padding:0 6px;margin:0 4px 2px 0;'
            f'border-radius:3px;color:{fg};background:{bg};">{_e(text)}</span>')


def _link(href: str, text: str, strong: bool = False) -> str:
    style = f"color:{C['accent']};font-weight:600;" if strong else f"color:{C['muted']};"
    return f'<a href="{_e(href)}" style="{style}">{_e(text)}</a>'


def _links(a: dict, size: int) -> str:
    links = a["links"]
    parts = [_link(links["pubmed"], "PubMed")]
    if links["doi"]:
        parts.append(_link(links["doi"], "DOI"))
    if links["fulltext"]:
        parts.append(_link(links["fulltext"], "Teljes szöveg"))
    if links["pdf"]:
        parts.append(_link(links["pdf"], "PDF ↓", strong=True))
    return f'<div style="font-size:{size}px;margin-top:4px;">{" · ".join(parts)}</div>'


def _row(inner: str, pad: str = "14px 0") -> str:
    return f'<tr><td style="padding:{pad};border-top:1px solid {C["line"]};">{inner}</td></tr>'


def _full_item(cfg: Config, a: dict, show_topics: bool) -> str:
    meta = " · ".join(x for x in (_e(first_author(a["authors"])), f'<i>{_e(a["journal_abbrev"] or a["journal"])}</i>',
                                  _e(a["pub_year"])) if x)
    tags = _tag("Frissített", warn=True) if a["is_update"] else ""
    tags += "".join(_tag(t) for t in type_labels(a))
    if show_topics:
        trial_sections = {s.id for s in cfg.sections if s.style == "trials"}
        tags += "".join(_tag(t.label) for t in cfg.topics if t.id in a["topics"] and t.section not in trial_sections)
    concl = conclusion(a["abstract"])
    inner = (f'<a href="{_e(a["links"]["pubmed"])}" style="color:{C["ink"]};text-decoration:none;font-weight:600;'
             f'font-size:15px;line-height:21px;">{_e(a["title"])}</a>'
             f'<div style="font-size:13px;color:{C["muted"]};margin-top:3px;">{meta}</div>')
    if tags:
        inner += f'<div style="margin-top:6px;">{tags}</div>'
    if concl:
        inner += f'<div style="font-size:13px;line-height:19px;color:{C["ink"]};margin-top:7px;">{_e(concl)}</div>'
    return _row(inner + _links(a, 12))


def _compact_item(a: dict) -> str:
    extra = [t for t in type_labels(a) if t in ("Metaanalízis", "Irányelv")]
    meta = f'<i>{_e(a["journal_abbrev"] or a["journal"])}</i>' + ("".join(f" · {_e(t)}" for t in extra))
    inner = (f'<a href="{_e(a["links"]["pubmed"])}" style="color:{C["ink"]};text-decoration:none;font-size:14px;'
             f'line-height:19px;">{_e(a["title"])}</a>'
             f'<div style="font-size:12px;color:{C["muted"]};margin-top:2px;">{meta}</div>')
    return _row(inner + _links(a, 11), "8px 0")


def _trial_item(t: dict) -> str:
    countries = t["countries"]
    ctry = ", ".join(countries[:3]) + (f" +{len(countries) - 3}" if len(countries) > 3 else "")
    end = (t["primary_completion"] or "")[:7]
    meta = " · ".join(x for x in (_e(t["nct_id"]), _e(t["sponsor"]), _e(ctry),
                                  f"n={t['enrollment']}" if t["enrollment"] else "",
                                  f"várható zárás {_e(end)}" if end else "") if x)
    tags = _tag(STATUS_HU.get(t["status"], t["status"]), warn=t["status"] == "RECRUITING")
    if t["study_type"]:
        tags += _tag(STUDY_TYPE_HU.get(t["study_type"], t["study_type"]))
    phases = ", ".join(PHASE_HU.get(p, p) for p in t["phases"])
    if phases:
        tags += _tag(phases)
    inner = (f'<a href="{_e(t["url"])}" style="color:{C["ink"]};text-decoration:none;font-weight:600;font-size:14px;'
             f'line-height:20px;">{_e(t["title"])}</a>'
             f'<div style="font-size:12px;color:{C["muted"]};margin-top:3px;">{meta}</div>'
             f'<div style="margin-top:6px;">{tags}</div>'
             f'<div style="font-size:12px;margin-top:4px;">{_link(t["url"], "ClinicalTrials.gov")}</div>')
    return _row(inner, "12px 0")


def _section_html(cfg: Config, s: Section, g: dict) -> str:
    count = len(g["articles"]) + len(g["trials"])
    if s.style == "full":
        rows = "".join(_full_item(cfg, a, show_topics=s.id == GUIDELINE_SECTION) for a in g["articles"])
    elif s.style == "compact":
        rows = "".join(_compact_item(a) for a in g["articles"])
    else:
        rows = "".join(_trial_item(t) for t in g["trials"]) + "".join(_full_item(cfg, a, False) for a in g["articles"])
    if not rows:
        rows = _row(f'<span style="font-size:13px;color:{C["muted"]};">Ma nincs új tétel.</span>', "10px 0")
    head = (f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{C["accent"]};'
            f'font-weight:700;">{_e(s.title)} <span style="color:{C["muted"]};font-weight:400;">· {count}</span></div>')
    if s.subtitle:
        head += f'<div style="font-size:12px;color:{C["muted"]};margin-top:2px;">{_e(s.subtitle)}</div>'
    return f'<tr><td style="padding:28px 0 6px 0;" id="{_e(s.id)}">{head}</td></tr>{rows}'


def _page(title: str, header: str, body: str, footer: str) -> str:
    return (f'<!doctype html><html lang="hu"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title></head>'
            f'<body style="margin:0;padding:0;background:{C["bg"]};">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C["bg"]};">'
            f'<tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;'
            f'background:#ffffff;border:1px solid {C["line"]};border-radius:6px;font-family:{FONT};color:{C["ink"]};">'
            f'<tr><td style="padding:24px 28px 8px 28px;">{header}</td></tr>'
            f'<tr><td style="padding:0 28px 24px 28px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{body}</table></td></tr>'
            f'<tr><td style="padding:16px 28px;border-top:1px solid {C["line"]};font-size:11px;line-height:16px;'
            f'color:{C["muted"]};">{footer}</td></tr></table></td></tr></table></body></html>')


def _summary_table(cfg: Config, groups: dict[str, dict]) -> str:
    rows = ""
    for s in cfg.sections:
        n = len(groups[s.id]["articles"]) + len(groups[s.id]["trials"])
        weight, color = ("600", C["ink"]) if n else ("400", C["muted"])
        rows += (f'<tr><td style="padding:4px 0;font-size:13px;"><a href="#{_e(s.id)}" style="color:{C["ink"]};'
                 f'text-decoration:none;">{_e(s.title)}</a></td><td style="padding:4px 0;font-size:13px;text-align:right;'
                 f'color:{color};font-weight:{weight};">{n}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;'
            f'border-top:1px solid {C["line"]};border-bottom:1px solid {C["line"]};">{rows}</table>')


def build(cfg: Config, day: date, window: tuple[date, date], articles: list[dict], trials: list[dict],
          baseline: bool = False, baseline_total: int | None = None, subject: str | None = None,
          lead: str | None = None) -> Digest:
    """`baseline`: first run; `articles`/`trials` are then the recent subset worth showing in full and
    `baseline_total` the number of items loaded into the database."""
    groups = group(cfg, articles, trials)
    total = len(articles) + len(trials)
    span = f"{window[0]:%m.%d.} – {window[1]:%m.%d.}"
    if baseline:
        subject = subject or f"PubMed-figyelő elindult – az utolsó napok {total} tétele"
        lead = lead or (f"Első futás: {baseline_total if baseline_total is not None else total} tétel került az adatbázisba "
                        f"({window[0]:%Y.%m.%d.} óta). Alább az utolsó napok {total} tétele látható teljes tartalommal, "
                        "a többi az adatbázisban és az API-n érhető el. Holnaptól naponta csak az új tételekről kapsz összesítőt.")
    else:
        subject = subject or f"PubMed-figyelő {day:%Y.%m.%d.} – {total} új tétel"
        lead = lead or f"{total} új tétel · PubMed-be került {span}"
    header = (f'<div style="font-size:12px;color:{C["muted"]};">PubMed-figyelő · '
              f'{"első futás" if baseline else "napi összesítő"}</div>'
              f'<div style="font-size:22px;font-weight:700;margin-top:4px;">{_e(hu_date(day))}</div>'
              f'<div style="font-size:14px;line-height:20px;color:{C["muted"]};margin-top:4px;">{_e(lead)}</div>'
              + _summary_table(cfg, groups))
    body = "".join(_section_html(cfg, s, groups[s.id]) for s in cfg.sections)
    footer = ("Forrás: PubMed (Entrez-dátum szerint) és ClinicalTrials.gov (új regisztrációk, nem lezárt státusz). "
              "Minden tétel teljes adata (abstract, MeSH, linkek) az adatbázisban és az API-n érhető el.")
    return Digest(subject=subject, html=_page(subject, header, body, footer),
                  text=build_text(cfg, day, groups, lead))


# --- plain text ------------------------------------------------------------------------------
def build_text(cfg: Config, day: date, groups: dict[str, dict], lead: str) -> str:
    lines = [f"PubMed-figyelő – {hu_date(day)}", lead, ""]
    for s in cfg.sections:
        g = groups[s.id]
        lines.append(f"{s.title}: {len(g['articles']) + len(g['trials'])}")
    for s in cfg.sections:
        g = groups[s.id]
        if not g["articles"] and not g["trials"]:
            continue
        lines += ["", "=" * 60, f"{s.title.upper()} ({len(g['articles']) + len(g['trials'])})", "=" * 60]
        for t in g["trials"]:
            lines += ["", f"* {t['title']}",
                      f"  {t['nct_id']} · {STATUS_HU.get(t['status'], t['status'])} · {t['sponsor']}", f"  {t['url']}"]
        for a in g["articles"]:
            flag = "[FRISSÍTETT] " if a["is_update"] else ""
            lines += ["", f"* {flag}{a['title']}", f"  {first_author(a['authors'])} · {a['journal_abbrev']} · {a['pub_year']}"]
            for name, key in (("PubMed", "pubmed"), ("DOI", "doi"), ("PDF", "pdf")):
                if a["links"][key]:
                    lines.append(f"  {name}: {a['links'][key]}")
    return "\n".join(lines) + "\n"
