"""Open-access full text and PDF links, from official machine-access channels only.

PDF sources, best first (PDF_SOURCE_RANK): the PMC Article Datasets bucket (S3), Unpaywall copies that really serve a
script, CORE, the Elsevier and Wiley text-and-data-mining APIs, the OpenAlex PDF cache; then links for people only
(Europe PMC's web PDF and publisher PDFs behind a browser check, which we never work around). Preprints are tracked
separately and always labelled as preprints. Paywalled articles keep their PubMed/DOI links.

Licence, version (publishedVersion / acceptedVersion / submittedVersion) and OA status are kept as provenance.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .http import HttpClient, redact

log = logging.getLogger(__name__)

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UNPAYWALL = "https://api.unpaywall.org/v2/"
OPENALEX = "https://api.openalex.org/works"
OPENALEX_CONTENT = "https://content.openalex.org/works/"
ELSEVIER_ENTITLEMENT = "https://api.elsevier.com/content/article/entitlement/doi/"
ELSEVIER_ARTICLE = "https://api.elsevier.com/content/article/doi/"
WILEY_TDM = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"
CORE_SEARCH = "https://api.core.ac.uk/v3/search/works/"
BIORXIV_DETAILS = "https://api.biorxiv.org/details/"  # official bioRxiv / medRxiv API
BIORXIV_SERVERS = ("medrxiv", "biorxiv")
EPMC_BATCH = 100
OPENALEX_BATCH = 50
# PMC Article Datasets on AWS Open Data: public bucket meant for machine access, one prefix per article version
PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

# lower rank = preferred. The first six can be fetched by a script; the last two are links for people only.
PDF_SOURCE_RANK = {"pmc-s3": 0, "unpaywall": 1, "core": 2, "elsevier": 2, "wiley": 2, "openalex": 3,
                   "europepmc": 6, "unpaywall-web": 7, "": 9}
AUTO_DOWNLOAD_SOURCES = ("pmc-s3", "unpaywall", "core", "elsevier", "wiley", "openalex")
DIRECT_SOURCES = ("pmc-s3", "unpaywall", "core")  # the stored URL is the file itself
PROXY_SOURCES = ("elsevier", "wiley", "openalex")  # need a key: fetched through this service, the key stays here
ELSEVIER_PREFIXES = ("10.1016/",)
WILEY_PREFIXES = ("10.1002/", "10.1111/")
# Europe PMC / PMC web pages sit behind a browser check; Unpaywall and Europe PMC often list them as extra copies
GATED_HOSTS = ("europepmc.org", "ncbi.nlm.nih.gov")
MAX_PROBES_PER_ARTICLE = 3


@dataclass
class OaLinks:
    oa: bool = False
    pmcid: str = ""
    url_fulltext: str = ""
    url_pdf: str = ""  # for proxy sources: a link a person can open; the service fetches the file itself
    pdf_source: str = ""
    oa_status: str = ""  # gold | green | hybrid | bronze | diamond | closed
    license: str = ""  # e.g. cc-by, cc-by-nc-nd; empty = unknown / none stated
    version: str = ""  # publishedVersion | acceptedVersion | submittedVersion
    openalex_id: str = ""
    preprint_id: str = ""  # Europe PMC PPR id of a preprint of this article


@dataclass
class PreprintLinks:
    preprint_id: str
    doi: str = ""
    url_pdf: str = ""
    source: str = ""  # preprint (a script may fetch it) | preprint-web (link for people)


def _gated(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host.endswith(GATED_HOSTS)


def _is_pdf(status: int, head: bytes) -> bool:
    return status == 200 and head.startswith(b"%PDF")


def _norm_license(value: str | None) -> str:
    return re.sub(r"\s+", "-", (value or "").strip().lower())


# --- Europe PMC ------------------------------------------------------------------------------------
def parse_europepmc(result: dict) -> OaLinks:
    links = OaLinks(oa=result.get("isOpenAccess") == "Y", pmcid=result.get("pmcid") or "",
                    license=_norm_license(result.get("license")))
    for u in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
        if u.get("availabilityCode") not in ("OA", "F"):  # OA = open access, F = free to read
            continue
        if u.get("documentStyle") == "pdf" and not links.url_pdf:
            links.url_pdf, links.pdf_source = u.get("url", ""), "europepmc"
        elif u.get("documentStyle") == "html" and not links.url_fulltext:
            links.url_fulltext = u.get("url", "")
    if links.pmcid and not links.url_fulltext:
        links.url_fulltext = f"https://pmc.ncbi.nlm.nih.gov/articles/{links.pmcid}/"
    for cc in result.get("commentCorrectionList", {}).get("commentCorrection", []):
        if cc.get("source") == "PPR" and cc.get("type") == "Preprint in" and cc.get("id"):
            links.preprint_id = cc["id"]
            break
    return links


def europepmc_links(http: HttpClient, pmids: list[str]) -> dict[str, OaLinks]:
    found: dict[str, OaLinks] = {}
    for start in range(0, len(pmids), EPMC_BATCH):
        batch = pmids[start:start + EPMC_BATCH]
        query = "(" + " OR ".join(f"EXT_ID:{p}" for p in batch) + ") AND SRC:MED"
        try:
            data = http.get_json(EUROPEPMC, {"query": query, "resultType": "core", "format": "json", "pageSize": 1000})
        except Exception as exc:  # enrichment must never break the daily run
            log.warning("Europe PMC lekérdezés sikertelen: %s", redact(exc))
            continue
        for result in data.get("resultList", {}).get("result", []):
            if result.get("pmid"):
                found[result["pmid"]] = parse_europepmc(result)
    return found


def parse_preprint_record(result: dict) -> tuple[str, list[str], str]:
    """(preprint DOI, PDF candidates, server) of a Europe PMC preprint (SRC:PPR) record; server is "medrxiv" or
    "biorxiv" when the preprint is on one of those, else ''."""
    candidates = []
    for u in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
        url = u.get("url", "")
        if (u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F") and url
                and not _gated(url) and url not in candidates):
            candidates.append(url)
    publisher = ((result.get("bookOrReportDetails") or {}).get("publisher") or "").lower()
    server = publisher if publisher in BIORXIV_SERVERS else ""
    return (result.get("doi") or "").lower(), candidates, server


def biorxiv_pdf(http: HttpClient, server: str, doi: str) -> str:
    """Address of the latest version's PDF on medRxiv / bioRxiv, from their official details API ('' if unknown)."""
    try:
        data = http.get_json(f"{BIORXIV_DETAILS}{server}/{doi}")
    except Exception as exc:
        log.debug("%s API: nincs adat (%s): %s", server, doi, redact(exc))
        return ""
    versions = [int(c["version"]) for c in data.get("collection") or [] if str(c.get("version", "")).isdigit()]
    return f"https://www.{server}.org/content/{doi}v{max(versions)}.full.pdf" if versions else ""


def preprint_links(http: HttpClient, pmid_to_ppr: dict[str, str], unpaywall_email: str = "") -> dict[str, PreprintLinks]:
    """Preprints of published articles (Europe PMC links them with "Preprint in"). When Europe PMC lists no PDF for
    the preprint, Unpaywall is asked about the preprint's DOI. Each PDF candidate is tried once, as for Unpaywall:
    a real PDF may be downloaded by a script, anything else stays a link for people."""
    by_ppr: dict[str, tuple[str, list[str], str]] = {}
    ids = sorted(set(pmid_to_ppr.values()))
    for start in range(0, len(ids), EPMC_BATCH):
        query = "(" + " OR ".join(f"EXT_ID:{p}" for p in ids[start:start + EPMC_BATCH]) + ") AND SRC:PPR"
        try:
            data = http.get_json(EUROPEPMC, {"query": query, "resultType": "core", "format": "json", "pageSize": 1000})
        except Exception as exc:
            log.warning("Europe PMC preprint-lekérdezés sikertelen: %s", redact(exc))
            continue
        for result in data.get("resultList", {}).get("result", []):
            if result.get("id"):
                by_ppr[result["id"]] = parse_preprint_record(result)
    found: dict[str, PreprintLinks] = {}
    for pmid, ppr in pmid_to_ppr.items():
        if ppr not in by_ppr:
            continue
        doi, candidates, server = by_ppr[ppr]
        if not candidates and doi and server:
            candidates = [url for url in (biorxiv_pdf(http, server, doi),) if url]
        if not candidates and doi and unpaywall_email:
            try:
                data = http.get_json(UNPAYWALL + quote(doi, safe="/"), {"email": unpaywall_email})
                candidates = [c["url"] for c in parse_unpaywall(data)[1]]
            except Exception as exc:
                log.debug("Unpaywall (preprint): nincs adat (%s): %s", doi, redact(exc))
        links = PreprintLinks(preprint_id=ppr, doi=doi)
        if candidates:
            links.url_pdf, links.source = candidates[0], "preprint-web"
            for url in candidates[:2]:
                if _is_pdf(*http.probe(url)):
                    links.url_pdf, links.source = url, "preprint"
                    break
        found[pmid] = links
    return found


# --- PMC Article Datasets (S3) -----------------------------------------------------------------------
def parse_pmc_listing(xml: str) -> str:
    """URL of the PDF in an S3 ListObjectsV2 answer (first version that has one), or ''."""
    keys = re.findall(r"<Key>([^<]+)</Key>", xml)
    pdfs = sorted(k for k in keys if k.lower().endswith(".pdf"))
    return f"{PMC_BUCKET}/{pdfs[0]}" if pdfs else ""


def pmc_s3_links(http: HttpClient, pmcids: dict[str, str], licenses: dict[str, str] | None = None) -> dict[str, OaLinks]:
    """pmcids: pmid -> PMCID. Only licences that allow reuse are in the bucket; the rest keep other sources."""
    licenses = licenses or {}
    found: dict[str, OaLinks] = {}
    for pmid, pmcid in pmcids.items():
        try:
            xml = http.get_text(PMC_BUCKET + "/", {"list-type": "2", "prefix": pmcid + ".", "max-keys": "50"})
        except Exception as exc:
            log.debug("PMC S3: nincs adat (%s): %s", pmcid, redact(exc))
            continue
        url = parse_pmc_listing(xml)
        if url:
            found[pmid] = OaLinks(oa=True, pmcid=pmcid, url_pdf=url, pdf_source="pmc-s3",
                                  license=licenses.get(pmid, ""), version="publishedVersion")
    return found


# --- Unpaywall -----------------------------------------------------------------------------------------
def parse_unpaywall(data: dict) -> tuple[OaLinks, list[dict]]:
    """(links, PDF candidates). Candidates: best location first, then the others, minus the gated Europe PMC/PMC
    pages; each is {url, license, version}. `links.url_pdf` is the first candidate; whether a script may fetch it
    is decided by `unpaywall_links`."""
    best = data.get("best_oa_location") or {}
    locations = [best] + [loc for loc in data.get("oa_locations") or [] if loc != best]
    candidates: list[dict] = []
    for loc in locations:
        url = loc.get("url_for_pdf") or ""
        if url and not _gated(url) and url not in [c["url"] for c in candidates]:
            candidates.append({"url": url, "license": _norm_license(loc.get("license")), "version": loc.get("version") or ""})
    landing = best.get("url_for_landing_page") or best.get("url") or ""
    links = OaLinks(oa=bool(data.get("is_oa")), url_fulltext="" if _gated(landing) else landing,
                    url_pdf=candidates[0]["url"] if candidates else "", pdf_source="unpaywall" if candidates else "",
                    oa_status=data.get("oa_status") or "", license=_norm_license(best.get("license")),
                    version=best.get("version") or "")
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
            log.debug("Unpaywall: nincs adat (%s): %s", doi, redact(exc))
            continue
        links, candidates = parse_unpaywall(data)
        if not links.oa:
            if links.oa_status:  # closed: still worth keeping as provenance
                found[pmid] = OaLinks(oa_status=links.oa_status)
            continue
        if candidates and known_pdf.get(pmid) in [c["url"] for c in candidates]:
            links.url_pdf, links.pdf_source = "", ""  # stored and classified earlier: no new probe
        elif candidates:
            links.pdf_source = "unpaywall-web"
            for cand in candidates[:MAX_PROBES_PER_ARTICLE]:
                if _is_pdf(*http.probe(cand["url"])):
                    links.url_pdf, links.pdf_source = cand["url"], "unpaywall"
                    links.license, links.version = cand["license"], cand["version"]
                    break
        found[pmid] = links
    return found


def unpaywall_provenance(http: HttpClient, dois: dict[str, str], email: str,
                         stored_pdf: dict[str, str] | None = None) -> dict[str, OaLinks]:
    """OA status (and, for a PDF we already store, its licence and version) from Unpaywall, without trying any
    download: for articles whose PDF came from elsewhere, or was found before provenance was recorded."""
    stored_pdf = stored_pdf or {}
    found: dict[str, OaLinks] = {}
    for pmid, doi in dois.items():
        try:
            data = http.get_json(UNPAYWALL + quote(doi, safe="/"), {"email": email})
        except Exception as exc:
            log.debug("Unpaywall: nincs adat (%s): %s", doi, redact(exc))
            continue
        links, candidates = parse_unpaywall(data)
        info = OaLinks(oa=links.oa, oa_status=links.oa_status)
        same = next((c for c in candidates if c["url"] == stored_pdf.get(pmid)), None)
        if same:  # describes the PDF we keep: its licence / version may be recorded
            info.url_pdf, info.pdf_source = same["url"], "unpaywall"
            info.license, info.version = same["license"], same["version"]
        found[pmid] = info
    return found


# --- OpenAlex (needs a free API key since 2026) ------------------------------------------------------
def _bare_doi(value: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (value or "").strip(), flags=re.I).lower()


def parse_openalex_work(work: dict) -> OaLinks:
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    links = OaLinks(oa=bool(oa.get("is_oa")), oa_status=oa.get("oa_status") or "",
                    openalex_id=(work.get("id") or "").rsplit("/", 1)[-1],
                    license=_norm_license(best.get("license")), version=best.get("version") or "",
                    url_fulltext=best.get("landing_page_url") or "")
    if (work.get("has_content") or {}).get("pdf") and links.openalex_id:
        doi = _bare_doi(work.get("doi") or "")
        links.pdf_source = "openalex"
        links.url_pdf = best.get("pdf_url") or best.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else "")
    return links


def openalex_links(http: HttpClient, dois: dict[str, str], api_key: str) -> dict[str, OaLinks]:
    """pmid -> OpenAlex view of the article: OA status, licence, version and, where OpenAlex holds a cached PDF,
    the `openalex` source (the PDF itself is fetched through this service with the key)."""
    if not api_key or not dois:
        return {}
    by_doi = {d.lower(): p for p, d in dois.items()}
    found: dict[str, OaLinks] = {}
    items = list(by_doi)
    for start in range(0, len(items), OPENALEX_BATCH):
        batch = items[start:start + OPENALEX_BATCH]
        params = {"filter": "doi:" + "|".join(batch), "per-page": OPENALEX_BATCH, "api_key": api_key,
                  "select": "id,doi,has_content,open_access,best_oa_location"}
        try:
            data = http.get_json(OPENALEX, params)
        except Exception as exc:
            log.warning("OpenAlex lekérdezés sikertelen: %s", redact(exc))
            continue
        for work in data.get("results", []):
            pmid = by_doi.get(_bare_doi(work.get("doi") or ""))
            if pmid:
                found[pmid] = parse_openalex_work(work)
    return found


def openalex_pdf_url(openalex_id: str, api_key: str) -> str:
    return f"{OPENALEX_CONTENT}{openalex_id}.pdf?api_key={api_key}"


# --- Elsevier / Wiley text-and-data-mining APIs ---------------------------------------------------------
def elsevier_headers(api_key: str, insttoken: str = "", accept: str = "application/json") -> dict:
    headers = {"X-ELS-APIKey": api_key, "Accept": accept}
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    return headers


def parse_elsevier_entitlement(data: dict) -> bool:
    ent = (data.get("entitlement-response") or {}).get("document-entitlement") or {}
    if isinstance(ent, list):
        ent = ent[0] if ent else {}
    value = ent.get("entitled")
    return value is True or str(value).lower() == "true"


def elsevier_links(http: HttpClient, dois: dict[str, str], api_key: str, insttoken: str = "",
                   human: dict[str, str] | None = None) -> dict[str, OaLinks]:
    """Elsevier articles the key is entitled to (open access, or subscribed when an institution token is given)."""
    if not api_key:
        return {}
    human = human or {}
    found: dict[str, OaLinks] = {}
    for pmid, doi in dois.items():
        if not doi.lower().startswith(ELSEVIER_PREFIXES):
            continue
        try:
            data = http.get_json(ELSEVIER_ENTITLEMENT + quote(doi, safe="/"),
                                 headers=elsevier_headers(api_key, insttoken))
        except Exception as exc:
            log.debug("Elsevier: nincs jogosultsági adat (%s): %s", doi, redact(exc))
            continue
        if parse_elsevier_entitlement(data):
            found[pmid] = OaLinks(oa=True, pdf_source="elsevier", url_pdf=human.get(pmid) or f"https://doi.org/{doi}")
    return found


def wiley_headers(token: str) -> dict:
    return {"Wiley-TDM-Client-Token": token}


def wiley_url(doi: str) -> str:
    return WILEY_TDM + quote(doi, safe="")


def wiley_links(http: HttpClient, dois: dict[str, str], token: str,
                human: dict[str, str] | None = None) -> dict[str, OaLinks]:
    """Wiley articles the TDM token may download (one polite probe each; Wiley allows 60 requests / 10 minutes)."""
    if not token:
        return {}
    human = human or {}
    found: dict[str, OaLinks] = {}
    for pmid, doi in dois.items():
        if not doi.lower().startswith(WILEY_PREFIXES):
            continue
        if _is_pdf(*http.probe(wiley_url(doi), headers=wiley_headers(token))):
            found[pmid] = OaLinks(oa=True, pdf_source="wiley", url_pdf=human.get(pmid) or f"https://doi.org/{doi}")
    return found


# --- CORE (repository copies; free API key) ------------------------------------------------------------
def parse_core_results(data: dict) -> list[str]:
    urls: list[str] = []
    for work in data.get("results") or []:
        candidates = [work.get("downloadUrl") or ""]
        candidates += [link.get("url", "") for link in work.get("links") or [] if link.get("type") == "download"]
        for url in candidates:
            if url and not _gated(url) and url not in urls:
                urls.append(url)
    return urls


def core_links(http: HttpClient, dois: dict[str, str], api_key: str, limit: int = 40) -> dict[str, OaLinks]:
    """Repository copies (often accepted manuscripts) that CORE knows of; tried once each like Unpaywall's."""
    if not api_key:
        return {}
    found: dict[str, OaLinks] = {}
    for pmid, doi in list(dois.items())[:limit]:
        try:
            data = http.get_json(CORE_SEARCH, {"q": f'doi:"{doi}"', "limit": 3},
                                 headers={"Authorization": f"Bearer {api_key}"})
        except Exception as exc:
            log.debug("CORE: nincs adat (%s): %s", doi, redact(exc))
            continue
        for url in parse_core_results(data)[:2]:
            if _is_pdf(*http.probe(url)):
                found[pmid] = OaLinks(oa=True, pdf_source="core", url_pdf=url)
                break
    return found
