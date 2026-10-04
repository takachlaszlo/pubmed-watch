"""PubMed through the NCBI E-utilities: esearch for new PMIDs, efetch for the full records."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date

from .http import HttpClient
from .models import Article

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
FETCH_BATCH = 200
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class PubMed:
    def __init__(self, http: HttpClient, api_key: str = "", email: str = ""):
        self.http = http
        self.common = {"tool": "pubmed-watch"}
        if api_key:
            self.common["api_key"] = api_key
        if email:
            self.common["email"] = email

    def search(self, query: str, since: date, until: date) -> list[str]:
        """PMIDs that entered PubMed (Entrez date) between the two days, inclusive."""
        data = {**self.common, "db": "pubmed", "term": query, "datetype": "edat",
                "mindate": since.strftime("%Y/%m/%d"), "maxdate": until.strftime("%Y/%m/%d"),
                "retmode": "json", "retmax": 9999}
        # POST: the topic queries are too long for a URL
        result = self.http.request("POST", EUTILS + "esearch.fcgi", data=data).json()["esearchresult"]
        if "ERROR" in result:
            raise RuntimeError(f"PubMed-keresési hiba: {result['ERROR']}")
        for problem in ("phrasesnotfound", "fieldsnotfound"):
            if result.get("errorlist", {}).get(problem):
                log.warning("PubMed nem értette a lekérdezés egy részét (%s): %s", problem, result["errorlist"][problem])
        ids = result.get("idlist", [])
        if int(result.get("count", 0)) > len(ids):
            log.warning("a találatok száma (%s) meghaladja a lekérhetőt (%d)", result["count"], len(ids))
        return ids

    def count(self, query: str, since: date, until: date) -> int:
        data = {**self.common, "db": "pubmed", "term": query, "datetype": "edat",
                "mindate": since.strftime("%Y/%m/%d"), "maxdate": until.strftime("%Y/%m/%d"),
                "retmode": "json", "retmax": 0}
        return int(self.http.request("POST", EUTILS + "esearch.fcgi", data=data).json()["esearchresult"]["count"])

    def fetch(self, pmids: list[str]) -> list[Article]:
        articles: list[Article] = []
        for start in range(0, len(pmids), FETCH_BATCH):
            batch = pmids[start:start + FETCH_BATCH]
            data = {**self.common, "db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
            xml = self.http.request("POST", EUTILS + "efetch.fcgi", data=data).content
            articles.extend(parse_efetch(xml))
        return articles


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _date(el: ET.Element | None) -> str:
    """YYYY-MM-DD (or shorter) from a PubMed <Year><Month><Day> element."""
    if el is None:
        return ""
    year = el.findtext("Year") or ""
    if not year:
        medline = el.findtext("MedlineDate") or ""
        return medline[:4]
    month = (el.findtext("Month") or "").strip()
    if month and not month.isdigit():
        month = str(MONTHS.get(month[:3].lower(), ""))
    day = (el.findtext("Day") or "").strip()
    parts = [year] + ([month.zfill(2)] if month else []) + ([day.zfill(2)] if month and day else [])
    return "-".join(parts)


def parse_article(node: ET.Element) -> Article | None:
    cit = node.find("MedlineCitation")
    art = cit.find("Article") if cit is not None else None
    if cit is None or art is None:
        return None
    pmid = cit.findtext("PMID") or ""

    authors = []
    for a in art.findall("AuthorList/Author"):
        if a.find("CollectiveName") is not None:
            authors.append(_text(a.find("CollectiveName")))
        elif a.findtext("LastName"):
            authors.append(f"{a.findtext('LastName')} {a.findtext('Initials') or ''}".strip())

    journal = art.find("Journal")
    issue_date = _date(journal.find("JournalIssue/PubDate")) if journal is not None else ""
    article_date = _date(art.find("ArticleDate"))
    pub_date = article_date or issue_date

    ids = {i.get("IdType"): (i.text or "").strip() for i in node.findall("PubmedData/ArticleIdList/ArticleId")}
    entrez = next((_date(d) for d in node.findall("PubmedData/History/PubMedPubDate") if d.get("PubStatus") == "entrez"), "")

    return Article(
        pmid=pmid,
        title=_text(art.find("ArticleTitle")).rstrip("."),
        abstract=[(t.get("Label") or "", _text(t)) for t in art.findall("Abstract/AbstractText") if _text(t)],
        authors=authors,
        journal=_text(journal.find("Title")) if journal is not None else "",
        journal_abbrev=cit.findtext("MedlineJournalInfo/MedlineTA") or (journal.findtext("ISOAbbreviation") if journal is not None else "") or "",
        pub_year=(pub_date or issue_date)[:4],
        pub_date=pub_date,
        entrez_date=entrez,
        pub_types=[_text(p) for p in art.findall("PublicationTypeList/PublicationType")],
        mesh=[_text(m) for m in cit.findall("MeshHeadingList/MeshHeading/DescriptorName")],
        keywords=[_text(k) for k in cit.findall("KeywordList/Keyword") if _text(k)],
        language=art.findtext("Language") or "",
        doi=ids.get("doi", ""),
        pmcid=ids.get("pmc", ""),
        publication_status=node.findtext("PubmedData/PublicationStatus") or "",
    )


def parse_efetch(xml: bytes) -> list[Article]:
    root = ET.fromstring(xml)
    articles = []
    for node in root.findall("PubmedArticle"):
        article = parse_article(node)
        if article and article.pmid:
            articles.append(article)
    return articles
