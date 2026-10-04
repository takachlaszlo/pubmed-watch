"""Open-access full text and PDF links: Europe PMC first, Unpaywall (optional) for the rest.

Only legal open-access copies are linked; paywalled articles keep their PubMed/DOI links.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from .http import HttpClient

log = logging.getLogger(__name__)

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UNPAYWALL = "https://api.unpaywall.org/v2/"
EPMC_BATCH = 100


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


def parse_unpaywall(data: dict) -> OaLinks:
    best = data.get("best_oa_location") or {}
    return OaLinks(oa=bool(data.get("is_oa")), url_fulltext=best.get("url_for_landing_page") or "",
                   url_pdf=best.get("url_for_pdf") or "", pdf_source="unpaywall" if best.get("url_for_pdf") else "")


def unpaywall_links(http: HttpClient, dois: dict[str, str], email: str) -> dict[str, OaLinks]:
    """dois: pmid -> DOI. Unpaywall asks for a contact e-mail with every request."""
    found: dict[str, OaLinks] = {}
    for pmid, doi in dois.items():
        try:
            data = http.get_json(UNPAYWALL + quote(doi, safe="/"), {"email": email})
        except Exception as exc:
            log.debug("Unpaywall: nincs adat (%s): %s", doi, exc)
            continue
        links = parse_unpaywall(data)
        if links.oa:
            found[pmid] = links
    return found
