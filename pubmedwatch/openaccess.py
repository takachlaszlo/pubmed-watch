"""Open-access full text and PDF links: Europe PMC first, Unpaywall (optional) for the rest.

Only legal open-access copies are linked; paywalled articles keep their PubMed/DOI links.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .http import HttpClient

log = logging.getLogger(__name__)

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UNPAYWALL = "https://api.unpaywall.org/v2/"
EPMC_BATCH = 100
# PMC Article Datasets on AWS Open Data: public bucket meant for machine access, one prefix per article version
PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
# lower rank = preferred. Only the first two can be fetched by a script. The others are links for people: Europe
# PMC's web PDF and many publisher PDFs sit behind a browser check that must not be bypassed.
PDF_SOURCE_RANK = {"pmc-s3": 0, "unpaywall": 1, "europepmc": 2, "unpaywall-web": 3, "": 9}
AUTO_DOWNLOAD_SOURCES = ("pmc-s3", "unpaywall")
# Unpaywall often lists these as extra copies; they are the browser-gated Europe PMC / PMC pages we already link
GATED_HOSTS = ("europepmc.org", "ncbi.nlm.nih.gov")
MAX_PROBES_PER_ARTICLE = 3


@dataclass
class OaLinks:
    oa: bool = False
    pmcid: str = ""
    url_fulltext: str = ""
    url_pdf: str = ""
    pdf_source: str = ""


def parse_europepmc(result: dict) -> OaLinks:
    links = OaLinks(oa=result.get("isOpenAccess") == "Y", pmcid=result.get("pmcid") or "")
    for u in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
        if u.get("availabilityCode") not in ("OA", "F"):  # OA = open access, F = free to read
            continue
        if u.get("documentStyle") == "pdf" and not links.url_pdf:
            links.url_pdf, links.pdf_source = u.get("url", ""), "europepmc"
        elif u.get("documentStyle") == "html" and not links.url_fulltext:
            links.url_fulltext = u.get("url", "")
    if links.pmcid and not links.url_fulltext:
        links.url_fulltext = f"https://pmc.ncbi.nlm.nih.gov/articles/{links.pmcid}/"
    return links


def europepmc_links(http: HttpClient, pmids: list[str]) -> dict[str, OaLinks]:
    found: dict[str, OaLinks] = {}
    for start in range(0, len(pmids), EPMC_BATCH):
        batch = pmids[start:start + EPMC_BATCH]
        query = "(" + " OR ".join(f"EXT_ID:{p}" for p in batch) + ") AND SRC:MED"
        try:
            data = http.get_json(EUROPEPMC, {"query": query, "resultType": "core", "format": "json", "pageSize": 1000})
        except Exception as exc:  # enrichment must never break the daily run
            log.warning("Europe PMC lekérdezés sikertelen: %s", exc)
            continue
        for result in data.get("resultList", {}).get("result", []):
            if result.get("pmid"):
                found[result["pmid"]] = parse_europepmc(result)
    return found


def _gated(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host.endswith(GATED_HOSTS)


def parse_unpaywall(data: dict) -> tuple[OaLinks, list[str]]:
    """(links, PDF candidates). Candidates: best location first, then the others, minus the gated Europe PMC/PMC
    pages. `links.url_pdf` is the first candidate; whether a script may fetch it is decided by `unpaywall_links`."""
    best = data.get("best_oa_location") or {}
    locations = [best] + [loc for loc in data.get("oa_locations") or [] if loc != best]
    candidates: list[str] = []
    for loc in locations:
        url = loc.get("url_for_pdf") or ""
        if url and not _gated(url) and url not in candidates:
            candidates.append(url)
    landing = best.get("url_for_landing_page") or best.get("url") or ""
    links = OaLinks(oa=bool(data.get("is_oa")), url_fulltext="" if _gated(landing) else landing,
                    url_pdf=candidates[0] if candidates else "", pdf_source="unpaywall" if candidates else "")
    return links, candidates


def unpaywall_links(http: HttpClient, dois: dict[str, str], email: str,
                    known_pdf: dict[str, str] | None = None) -> dict[str, OaLinks]:
    """dois: pmid -> DOI. Unpaywall asks for a contact e-mail with every request.

    Each new PDF candidate is tried once: if it really returns a PDF it is marked `unpaywall` (scripts may download
    it), otherwise `unpaywall-web` (a link for people only; we never work around a refusal)."""
    known_pdf = known_pdf or {}
    found: dict[str, OaLinks] = {}
    for pmid, doi in dois.items():
        try:
            data = http.get_json(UNPAYWALL + quote(doi, safe="/"), {"email": email})
        except Exception as exc:
            log.debug("Unpaywall: nincs adat (%s): %s", doi, exc)
            continue
        links, candidates = parse_unpaywall(data)
        if not links.oa:
            continue
        if candidates and known_pdf.get(pmid) in candidates:
            links.url_pdf, links.pdf_source = "", ""  # stored and classified earlier: no new probe
        elif candidates:
            links.pdf_source = "unpaywall-web"
            for url in candidates[:MAX_PROBES_PER_ARTICLE]:
                status, head = http.probe(url)
                if status == 200 and head.startswith(b"%PDF"):
                    links.url_pdf, links.pdf_source = url, "unpaywall"
                    break
        found[pmid] = links
    return found


def parse_pmc_listing(xml: str) -> str:
    """URL of the PDF in an S3 ListObjectsV2 answer (first version that has one), or ''."""
    keys = re.findall(r"<Key>([^<]+)</Key>", xml)
    pdfs = sorted(k for k in keys if k.lower().endswith(".pdf"))
    return f"{PMC_BUCKET}/{pdfs[0]}" if pdfs else ""


def pmc_s3_links(http: HttpClient, pmcids: dict[str, str]) -> dict[str, OaLinks]:
    """pmcids: pmid -> PMCID. Only licences that allow reuse are in the bucket; the rest keep other sources."""
    found: dict[str, OaLinks] = {}
    for pmid, pmcid in pmcids.items():
        try:
            xml = http.get_text(PMC_BUCKET + "/", {"list-type": "2", "prefix": pmcid + ".", "max-keys": "50"})
        except Exception as exc:
            log.debug("PMC S3: nincs adat (%s): %s", pmcid, exc)
            continue
        url = parse_pmc_listing(xml)
        if url:
            found[pmid] = OaLinks(oa=True, pmcid=pmcid, url_pdf=url, pdf_source="pmc-s3")
    return found
