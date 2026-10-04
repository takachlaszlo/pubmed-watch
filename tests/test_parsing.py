from datetime import date

from conftest import fixture_bytes, fixture_json

from pubmedwatch.clinicaltrials import new_trials, parse_study
from pubmedwatch.config import TrialsConfig
from pubmedwatch.openaccess import parse_europepmc, parse_pmc_listing, parse_unpaywall
from pubmedwatch.pubmed import parse_efetch


def articles():
    return {a.pmid: a for a in parse_efetch(fixture_bytes("efetch.xml"))}


def test_efetch_fields():
    a = articles()["42825830"]
    assert a.title.startswith("Early-onset neonatal sepsis and neonatal antibiotic exposure in Hungary")
    assert not a.title.endswith(".")
    assert a.journal_abbrev == "Eur J Pediatr"
    assert a.entrez_date == "2026-10-02"
    assert a.doi.startswith("10.")
    assert a.authors[0] == "Mari J"
    assert any(label.upper().startswith("CONCLUSION") for label, _ in a.abstract)
    assert a.language == "eng"


def test_efetch_all_records_and_ids():
    parsed = articles()
    assert len(parsed) == 8
    assert parsed["42814651"].pmcid.startswith("PMC")
    assert parsed["42805644"].authors[-1] == "Collaborator Group P"  # collective author kept
    for a in parsed.values():
        assert len(a.entrez_date) == 10 and a.pub_year.isdigit()


def test_europepmc_links():
    results = {r["pmid"]: parse_europepmc(r) for r in fixture_json("europepmc.json")["resultList"]["result"]}
    oa = results["42814651"]
    assert oa.oa and oa.pmcid.startswith("PMC")
    assert oa.url_pdf.endswith("?pdf=render") and oa.pdf_source == "europepmc"
    assert oa.url_fulltext.startswith("https://europepmc.org/articles/PMC")
    closed = results["42825830"]
    assert not closed.url_pdf


def test_unpaywall_parse_basic():
    links, candidates = parse_unpaywall({"is_oa": True, "best_oa_location": {"url_for_pdf": "https://x.org/a.pdf",
                                                                          "url_for_landing_page": "https://x.org/a"},
                                         "oa_locations": []})
    assert (links.oa, links.url_pdf, links.url_fulltext, candidates) == (True, "https://x.org/a.pdf", "https://x.org/a",
                                                                          ["https://x.org/a.pdf"])
    links, candidates = parse_unpaywall({"is_oa": False, "best_oa_location": None})
    assert (links.oa, links.url_pdf, candidates) == (False, "", [])


def test_clinicaltrials_study():
    t = parse_study(fixture_json("ctgov.json")["studies"][0])
    assert t.nct_id == "NCT07854847"
    assert t.status and t.first_posted.startswith("2026-")
    assert t.url == "https://clinicaltrials.gov/study/NCT07854847"
    assert "NA" not in t.phases


class PagedHttp:
    def __init__(self, pages):
        self.pages, self.params = pages, []

    def get_json(self, url, params=None):
        self.params.append(params)
        return self.pages[params.get("pageToken", "first")]


def test_clinicaltrials_paging_and_dedupe():
    studies = fixture_json("ctgov.json")["studies"]
    pages = {"first": {"studies": studies[:2], "nextPageToken": "p2"}, "p2": {"studies": studies[1:]}}
    http = PagedHttp(pages)
    cfg = TrialsConfig(enabled=True, max_age="21 years", statuses=["RECRUITING"], conditions="sepsis", terms="stewardship")
    trials = new_trials(http, cfg, date(2026, 9, 27), date(2026, 10, 4))
    assert [t.nct_id for t in trials] == sorted({s["protocolSection"]["identificationModule"]["nctId"] for s in studies})
    advanced = http.params[0]["filter.advanced"]
    assert "RANGE[2026-09-27, 2026-10-04]" in advanced
    assert "AREA[OverallStatus](RECRUITING)" in advanced and "AREA[MaximumAge]RANGE[MIN, 21 years]" in advanced
    assert len(http.params) == 4  # two pages for the condition search, two for the free-text search


def test_pmc_s3_listing():
    xml = fixture_bytes("pmc_s3_listing.xml").decode("utf-8")
    assert parse_pmc_listing(xml) == "https://pmc-oa-opendata.s3.amazonaws.com/PMC13625194.1/PMC13625194.1.pdf"
    assert parse_pmc_listing("<ListBucketResult><KeyCount>0</KeyCount></ListBucketResult>") == ""
