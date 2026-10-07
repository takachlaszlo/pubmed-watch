import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pytest
import requests

from conftest import FakeHttp, fixture_json

from pubmedwatch import api, runner
from pubmedwatch.downloads import scan_pdf_dir
from pubmedwatch.http import redact
from pubmedwatch.models import Article
from pubmedwatch.openaccess import (OaLinks, PreprintLinks, core_links, elsevier_links, openalex_links,
                                    parse_core_results, parse_elsevier_entitlement, parse_europepmc,
                                    parse_openalex_work, parse_preprint_record, preprint_links, wiley_links)
from pubmedwatch.report import author_request_link, build, library_link
from pubmedwatch.storage import Storage

OA = fixture_json("openalex_works.json")
DOI_A, DOI_B, DOI_C = (w["doi"].replace("https://doi.org/", "") for w in OA["results"])
PREPRINT = fixture_json("europepmc_preprint_record.json")
TODAY = date.today()


# --- parsing each source ---------------------------------------------------------------------------
def test_openalex_work():
    links = parse_openalex_work(OA["results"][0])
    assert (links.pdf_source, links.openalex_id) == ("openalex", "W7213638009")
    assert (links.oa_status, links.license, links.version) == ("diamond", "cc-by-nc-nd", "publishedVersion")
    assert links.url_pdf.startswith("https://www.pediatr-neonatol.com/")  # what a person can open; not the key URL
    assert parse_openalex_work(dict(OA["results"][0], has_content={"pdf": False})).pdf_source == ""


def test_openalex_lookup_maps_back_and_needs_a_key(cfg):
    http = FakeHttp(cfg, openalex=OA)
    found = openalex_links(http, {"1": DOI_A, "2": DOI_B.upper(), "3": "10.1/unknown"}, "KEY")
    assert set(found) == {"1", "2"} and found["2"].openalex_id == "W7213985103"
    params = http.calls[-1][2]
    assert params["api_key"] == "KEY" and params["filter"].startswith("doi:")
    assert openalex_links(http, {"1": DOI_A}, "") == {}


def test_preprint_found_from_the_published_record(cfg):
    published = fixture_json("europepmc_published_with_preprint.json")["resultList"]["result"][0]
    assert parse_europepmc(published).preprint_id == "PPR179288"
    doi, candidates, server = parse_preprint_record(PREPRINT["resultList"]["result"][0])
    assert doi == "10.1101/2020.06.22.20137273" and candidates and server == "medrxiv"
    assert all("europepmc.org" not in c for c in candidates)  # Europe PMC's own pages are browser-gated
    found = preprint_links(FakeHttp(cfg, preprints=PREPRINT, pdf_hosts=("www.medrxiv.org",)), {"32678530": "PPR179288"})
    assert found["32678530"].source == "preprint" and "medrxiv" in found["32678530"].url_pdf
    refused = preprint_links(FakeHttp(cfg, preprints=PREPRINT), {"32678530": "PPR179288"})
    assert refused["32678530"].source == "preprint-web"  # a link for people, never pushed against


def test_elsevier_entitlement(cfg):
    yes = {"entitlement-response": {"document-entitlement": {"entitled": True}}}
    assert parse_elsevier_entitlement(yes)
    assert parse_elsevier_entitlement({"entitlement-response": {"document-entitlement": [{"entitled": "true"}]}})
    assert not parse_elsevier_entitlement({"entitlement-response": {"document-entitlement": {"entitled": False}}})
    assert not parse_elsevier_entitlement({})
    http = FakeHttp(cfg, elsevier_entitled=(DOI_A,))
    found = elsevier_links(http, {"1": DOI_A, "2": DOI_B, "3": "10.1002/x"}, "EKEY", human={"1": "https://pub.example/a.pdf"})
    assert set(found) == {"1"} and found["1"].pdf_source == "elsevier" and found["1"].url_pdf == "https://pub.example/a.pdf"
    assert all(h.get("X-ELS-APIKey") == "EKEY" for h in http.headers_seen)
    assert not any("10.1002" in c[1] for c in http.calls)  # only Elsevier DOIs are asked about
    assert elsevier_links(http, {"1": DOI_A}, "") == {}


def test_wiley_and_core(cfg):
    http = FakeHttp(cfg, pdf_hosts=("api.wiley.com",))
    found = wiley_links(http, {"1": "10.1002/abc.123", "2": "10.1016/x"}, "WTOKEN")
    assert set(found) == {"1"} and found["1"].pdf_source == "wiley"
    assert http.probes == ["https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002%2Fabc.123"]
    assert http.headers_seen[-1] == {"Wiley-TDM-Client-Token": "WTOKEN"}
    assert wiley_links(http, {"1": "10.1002/abc.123"}, "") == {}

    answer = {"results": [{"downloadUrl": "https://core.ac.uk/download/1.pdf",
                           "links": [{"type": "download", "url": "https://repo.example/2.pdf"}, {"type": "display", "url": "x"}]}]}
    assert parse_core_results(answer) == ["https://core.ac.uk/download/1.pdf", "https://repo.example/2.pdf"]
    http = FakeHttp(cfg, core={"10.1/c": answer}, pdf_hosts=("repo.example",))
    found = core_links(http, {"9": "10.1/c"}, "CKEY")
    assert found["9"].pdf_source == "core" and found["9"].url_pdf == "https://repo.example/2.pdf"
    assert http.headers_seen[0] == {"Authorization": "Bearer CKEY"}
    assert core_links(http, {"9": "10.1/c"}, "") == {}


def test_redact_hides_keys():
    assert redact("https://content.openalex.org/works/W1.pdf?api_key=SECRET&x=1") == \
        "https://content.openalex.org/works/W1.pdf?api_key=***&x=1"
    assert "SECRET" not in redact("error for apiKey=SECRET")


# --- the daily enrichment: order, budget, provenance ------------------------------------------------
@pytest.fixture
def store(cfg):
    storage = Storage(cfg.data_dir)
    storage.run_id = storage.start_run("2026-10-01", "2026-10-07", False)
    yield storage
    storage.close()


def put(storage, pmid, doi="", section="iranyelvek", kind="guideline", **kw):
    storage.insert_article(Article(pmid=pmid, title=f"Title {pmid}", doi=doi, journal_abbrev="Pediatr Neonatol",
                                   pub_year="2026", kind=kind, **kw), section, storage.run_id)
    storage.commit()


def test_enrichment_order_and_provenance(cfg, store):
    cfg.sources.elsevier_api_key, cfg.sources.openalex_api_key = "EKEY", "OKEY"
    put(store, "1", DOI_A)   # Elsevier says entitled: the publisher's own TDM copy wins over the OpenAlex cache
    put(store, "2", DOI_B)   # not entitled: OpenAlex has the PDF
    put(store, "3", "10.1/nowhere")
    http = FakeHttp(cfg, elsevier_entitled=(DOI_A,), openalex=OA)
    runner.refresh_links(cfg, http=http, today=TODAY, force=True)
    a1, a2, a3 = (store.article(p) for p in ("1", "2", "3"))
    assert a1["pdf_source"] == "elsevier" and a1["openalex_id"] == "W7213638009" and a1["oa_status"] == "diamond"
    assert a2["pdf_source"] == "openalex" and a2["pdf_license"] == "cc-by-nc-nd" and a2["pdf_version"] == "publishedVersion"
    assert a3["pdf_source"] == ""
    hosts = [c[1].split("/")[2] for c in http.calls]
    assert hosts.index("api.openalex.org") > max(i for i, h in enumerate(hosts) if h == "api.elsevier.com")


def test_unresolved_article_with_preprint_gets_it(cfg, store):
    put(store, "32678530", "10.1056/nejmoa2021436")
    published = fixture_json("europepmc_published_with_preprint.json")
    published["resultList"]["result"][0].pop("fullTextUrlList", None)  # pretend no free copy of the article itself
    published["resultList"]["result"][0]["pmcid"] = ""
    http = FakeHttp(cfg, europepmc=published, preprints=PREPRINT, pdf_hosts=("www.medrxiv.org",))
    runner.refresh_links(cfg, http=http, today=TODAY, force=True)
    a = store.article("32678530")
    assert a["preprint"]["source"] == "preprint" and a["preprint"]["doi"] == "10.1101/2020.06.22.20137273"
    assert "medrxiv" in a["links"]["preprint_pdf"] and a["links"]["pdf"] == ""


# --- downloads: preprint channel, scope, proxy ----------------------------------------------------
def due(cfg, storage, **kw):
    now = datetime.now().astimezone()
    return {(a["pmid"], a["download"]["version"]): a["download"]
            for a in storage.downloads_due(now, 24, 365, lambda art, v: api.download_paths(cfg, art, v), **kw)}


def test_preprint_downloaded_and_filed_as_preprint_then_published_version_too(cfg, store, tmp_path):
    cfg.api.pdf_dir = tmp_path / "pdfs"
    (cfg.api.pdf_dir / "_inbox").mkdir(parents=True)
    put(store, "7", "10.1/seven")
    store.update_preprint("7", PreprintLinks(preprint_id="PPR1", doi="10.1101/x", source="preprint",
                                             url_pdf="https://www.medrxiv.org/x.full.pdf"))
    store.commit()
    offered = due(cfg, store)
    assert set(offered) == {("7", "preprint")}
    d = offered[("7", "preprint")]
    assert d["url"] == "https://www.medrxiv.org/x.full.pdf" and d["path"].endswith("-PREPRINT.pdf")
    (cfg.api.pdf_dir / d["path"]).write_bytes(b"%PDF-1.5 preprint")
    row = json.loads(api.handle_post(cfg, store, "/downloads/report",
                                     {"pmid": "7", "version": "preprint", "status": "ok", "path": d["path"]})[2])
    assert row["path"].endswith("-PREPRINT.pdf") and "Pediatr Neonatol" in row["path"]
    assert (cfg.api.pdf_dir / row["path"]).is_file()
    assert due(cfg, store) == {}
    # later the published version becomes free: it is offered in addition, the preprint is not offered again
    store.update_oa("7", OaLinks(oa=True, pdf_source="pmc-s3", url_pdf="https://pmc-oa-opendata.s3.amazonaws.com/x.pdf"))
    store.commit()
    assert set(due(cfg, store)) == {("7", "vor")}
    assert [f[1] for f in scan_pdf_dir(cfg.api.pdf_dir)] == ["preprint"]  # the preprint file is recognised as one


def test_scope_by_section_and_kind(cfg, store):
    for pmid, section, kind in (("1", "iranyelvek", "guideline"), ("2", "innovacio", "other"),
                                ("3", "review-gyermek", "systematic_review")):
        put(store, pmid, section=section, kind=kind)
        store.update_oa(pmid, OaLinks(oa=True, pdf_source="pmc-s3", url_pdf=f"https://s3/{pmid}.pdf"))
    store.commit()
    assert {p for p, _ in due(cfg, store)} == {"1", "2", "3"}
    assert {p for p, _ in due(cfg, store, sections=cfg.downloads.sections)} == {"1", "3"}  # innovation is not in the list
    assert {p for p, _ in due(cfg, store, kinds=["guideline"])} == {"1"}


def test_key_protected_sources_go_through_the_service(cfg, store):
    put(store, "5", DOI_B)
    store.update_oa("5", OaLinks(oa=True, pdf_source="openalex", openalex_id="W5", url_pdf="https://pub.example/5.pdf"))
    store.commit()
    d = due(cfg, store)[("5", "vor")]
    assert d["url"] == f"{cfg.api.public_url}/downloads/file/5"  # n8n never sees the key
    assert store.article("5")["links"]["pdf"] == "https://pub.example/5.pdf"  # the mail keeps a link people can open


class FakeStreamHttp:
    def __init__(self, body=b"%PDF-1.7 hello", error=None):
        self.body, self.error, self.urls, self.attempts = body, error, [], []

    def stream(self, url, headers=None, params=None, attempts=None):
        self.urls.append(url)
        self.attempts.append(attempts)
        if self.error:
            raise self.error
        body = self.body

        class Resp:
            def iter_content(self, size):
                return iter([body[:5], body[5:]])

            def close(self):
                pass
        return Resp()


def serve(cfg, http):
    cfg.api.port = 0
    server = api.make_server(cfg, host="127.0.0.1", http=http)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_proxy_streams_the_pdf_and_never_leaks_the_key(cfg, store):
    cfg.sources.openalex_api_key = "SECRETKEY"
    put(store, "5", DOI_B)
    store.update_oa("5", OaLinks(oa=True, pdf_source="openalex", openalex_id="W5", url_pdf="https://pub.example/5.pdf"))
    put(store, "6", "10.1/six")
    store.update_oa("6", OaLinks(oa=True, pdf_source="pmc-s3", url_pdf="https://s3/6.pdf"))
    store.commit()

    ok = FakeStreamHttp()
    server, base = serve(cfg, ok)
    try:
        with urllib.request.urlopen(base + "/downloads/file/5") as resp:
            assert resp.headers["Content-Type"] == "application/pdf" and resp.read() == b"%PDF-1.7 hello"
        assert ok.urls == ["https://content.openalex.org/works/W5.pdf?api_key=SECRETKEY"]
        assert ok.attempts == [1]  # no retries while n8n waits: a failed try comes back at the next run
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/downloads/file/6")      # direct source: nothing to proxy
        assert err.value.code == 400
    finally:
        server.shutdown()

    for http, code in ((FakeStreamHttp(body=b"<html>blocked"), 502),
                       (FakeStreamHttp(error=requests.HTTPError("HTTP 403 (content.openalex.org) api_key=SECRETKEY")), 502)):
        server, base = serve(cfg, http)
        try:
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + "/downloads/file/5")
            assert err.value.code == code and b"SECRETKEY" not in err.value.read()
        finally:
            server.shutdown()


# --- the e-mail: preprints, versions, asking the author, library ---------------------------------------
def mail_article(**kw):
    a = {"pmid": "9", "title": "Closed trial", "authors": ["Mari J", "Kis P"], "journal_abbrev": "Eur J Pediatr",
         "journal": "Eur J Pediatr", "pub_year": "2026", "pub_types": [], "kind": "other", "is_update": False,
         "topics": ["gyermek-ams"], "section": "gyermek-ams", "abstract": [], "doi": "10.1007/s00431-026-1",
         "author_emails": [{"name": "Mari J", "email": "mari.judit@example.org"}], "pdf_version": "",
         "links": {"pubmed": "https://pubmed.ncbi.nlm.nih.gov/9/", "doi": "https://doi.org/10.1007/s00431-026-1",
                   "fulltext": "", "pdf": "", "preprint_pdf": ""}}
    a.update(kw)
    return a


def test_closed_article_offers_the_author_and_the_library(cfg):
    cfg.report.library_link = "https://lib.example/login?url=https://doi.org/{doi}"
    cfg.report.request_signature = "Dr Example\nChildren's Hospital"
    a = mail_article()
    href = author_request_link(cfg, a)
    assert href.startswith("mailto:mari.judit@example.org?subject=Full-text%20request")
    assert "Dear%20Dr%20Mari" in href and "Children%27s%20Hospital" in href
    assert library_link(cfg, a) == "https://lib.example/login?url=https://doi.org/10.1007/s00431-026-1"
    html = build(cfg, TODAY, (TODAY - timedelta(days=1), TODAY), [a], []).html
    assert "Szerző megkérése" in html and "Könyvtár" in html


def test_free_articles_do_not_get_request_links_and_versions_are_labelled(cfg):
    cfg.report.library_link = "https://lib.example/{doi}"
    free = mail_article(links={"pubmed": "p", "doi": "d", "fulltext": "", "pdf": "https://x/a.pdf", "preprint_pdf": ""},
                        pdf_version="acceptedVersion")
    preprint_only = mail_article(pmid="10", links={"pubmed": "p", "doi": "d", "fulltext": "", "pdf": "",
                                                   "preprint_pdf": "https://www.medrxiv.org/x.pdf"})
    html = build(cfg, TODAY, (TODAY - timedelta(days=1), TODAY), [free, preprint_only], []).html
    assert html.count("Szerző megkérése") == 1  # a preprint is no substitute for the article: ask anyway
    assert "PDF: elfogadott kézirat" in html
    assert "Preprint ↓ (nem lektorált)" in html and "Csak preprint (nem lektorált)" in html


# --- migrations ----------------------------------------------------------------------------------------
def test_old_ledger_becomes_versioned_without_losing_rows(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    db = sqlite3.connect(folder / "pubmed.db")
    db.executescript(
        "CREATE TABLE downloads (pmid TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
        " first_attempt_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL, next_retry_at TEXT NOT NULL DEFAULT '',"
        " last_error TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', bytes INTEGER,"
        " saved_at TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '');"
        "INSERT INTO downloads VALUES ('1','ok',1,'t','t','','','J/2026-1-x.pdf',10,'t','n8n');")
    db.commit()
    db.close()
    storage = Storage(folder)
    rows = storage.downloads()
    assert len(rows) == 1 and rows[0]["version"] == "vor" and rows[0]["path"] == "J/2026-1-x.pdf"
    storage.report_download("1", "failed", datetime.now().astimezone(), 30, error="x", version="preprint")
    assert {(r["version"], r["status"]) for r in storage.downloads()} == {("vor", "ok"), ("preprint", "failed")}
    storage.close()


# --- the n8n workflow follows the service's decisions -----------------------------------------------------
def test_workflow_takes_url_path_and_version_from_the_service():
    from pathlib import Path
    wf = json.loads((Path(__file__).parent.parent / "n8n" / "pubmed-pdf-letoltes.workflow.json").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    code = nodes["Szelekció"]["parameters"]["jsCode"]
    assert "a.download.url" in code and "a.download.path" in code and "a.download.version" in code
    assert "SECTIONS" not in code  # the scope lives in config.yaml, not in n8n
    assert nodes["PDF letöltése"]["parameters"]["url"] == "={{ $json.url }}"
    for name in ("Jelentés: kész", "Jelentés: letöltési hiba", "Jelentés: mentési hiba"):
        fields = {p["name"]: p["value"] for p in nodes[name]["parameters"]["bodyParameters"]["parameters"]}
        assert fields["version"] == "={{ $('Egyenként').item.json.version }}"


def test_workflow_downloads_one_pdf_at_a_time():
    """The HTTP node's batching only staggers the starts and then leaves every response unread until the last one
    is in; on a long list servers drop those connections ("aborted"). A loop of one item avoids that."""
    from pathlib import Path
    wf = json.loads((Path(__file__).parent.parent / "n8n" / "pubmed-pdf-letoltes.workflow.json").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    links = {src: [[t["node"] for t in branch] for branch in c["main"]] for src, c in wf["connections"].items()}
    loop = nodes["Egyenként"]
    assert loop["type"] == "n8n-nodes-base.splitInBatches" and loop["parameters"]["batchSize"] == 1
    assert links["Szelekció"] == [["Egyenként"]] and links["Egyenként"] == [[], ["PDF letöltése"]]
    for name in ("Jelentés: kész", "Jelentés: letöltési hiba", "Jelentés: mentési hiba"):
        assert links[name] == [["Egyenként"]], name             # every path hands the turn to the next item
    assert "batching" not in nodes["PDF letöltése"]["parameters"]["options"]
    assert wf["settings"]["executionOrder"] == "v1"
    assert "api_key" not in json.dumps(wf).lower()  # no secret in the workflow


def test_forced_refresh_fills_in_provenance_without_probing(cfg, store):
    cfg.sources.unpaywall_email = "x@y.z"
    put(store, "42814651", "10.2196/test", pmcid="PMC13626073")
    store.update_oa("42814651", OaLinks(oa=True, pmcid="PMC13626073", pdf_source="pmc-s3",
                                        url_pdf="https://pmc-oa-opendata.s3.amazonaws.com/PMC13626073.1/PMC13626073.1.pdf"))
    store.commit()
    epmc = fixture_json("europepmc.json")
    for r in epmc["resultList"]["result"]:
        if r.get("pmid") == "42814651":
            r["license"] = "cc by"
    answer = {"is_oa": True, "oa_status": "gold", "best_oa_location": {"url_for_pdf": "https://pub.example/a.pdf",
                                                                       "license": "cc-by", "version": "publishedVersion"}}
    http = FakeHttp(cfg, europepmc=epmc, s3_pmcids=("PMC13626073",), unpaywall={"10.2196/test": answer},
                    pdf_hosts=("pub.example",))
    runner.refresh_links(cfg, http=http, today=TODAY, force=True)
    a = store.article("42814651")
    assert a["pdf_source"] == "pmc-s3" and a["pdf_license"] == "cc-by" and a["oa_status"] == "gold"
    assert http.probes == []  # provenance only: nothing was test-downloaded


def test_preprint_without_pdf_in_europepmc_is_looked_up_in_unpaywall(cfg):
    record = json.loads(json.dumps(PREPRINT))
    record["resultList"]["result"][0]["fullTextUrlList"] = {"fullTextUrl": [
        {"documentStyle": "doi", "availabilityCode": "F", "url": "https://doi.org/10.1101/2020.06.22.20137273"}]}
    answer = {"is_oa": True, "best_oa_location": {"url_for_pdf": "https://www.medrxiv.org/content/x.full.pdf",
                                                  "version": "submittedVersion"}}
    http = FakeHttp(cfg, preprints=record, unpaywall={"10.1101/2020.06.22.20137273": answer},
                    pdf_hosts=("www.medrxiv.org",))
    found = preprint_links(http, {"32678530": "PPR179288"}, unpaywall_email="x@y.z")
    assert found["32678530"].source == "preprint" and found["32678530"].url_pdf.endswith("x.full.pdf")
    assert preprint_links(FakeHttp(cfg, preprints=record), {"32678530": "PPR179288"})["32678530"].url_pdf == ""


def test_medrxiv_preprint_without_any_listed_pdf_uses_the_medrxiv_api(cfg):
    record = json.loads(json.dumps(PREPRINT))
    record["resultList"]["result"][0]["fullTextUrlList"] = {"fullTextUrl": []}

    class Http(FakeHttp):
        def get_json(self, url, params=None, headers=None, attempts=None):
            if url.startswith("https://api.biorxiv.org/details/medrxiv/"):
                self.calls.append(("GET", url, {}))
                return {"collection": [{"version": "1"}, {"version": "2"}]}
            return super().get_json(url, params, headers)

    http = Http(cfg, preprints=record, pdf_hosts=("www.medrxiv.org",))
    found = preprint_links(http, {"32678530": "PPR179288"})
    assert found["32678530"].url_pdf == "https://www.medrxiv.org/content/10.1101/2020.06.22.20137273v2.full.pdf"
    assert found["32678530"].source == "preprint"


def test_adding_a_key_triggers_one_full_link_refresh(cfg, monkeypatch):
    from pubmedwatch import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "refresh_links", lambda c, force=False: calls.append(force))
    monkeypatch.setattr(scheduler, "refresh_bibliography_job", lambda c: 0)
    monkeypatch.setattr(scheduler, "load_config", lambda path=None: cfg)
    scheduler._startup_tasks(None)                 # first start: one forced refresh (+ the normal one)
    scheduler._startup_tasks(None)                 # nothing new: only the normal one
    cfg.sources.openalex_api_key = "NEW"
    scheduler._startup_tasks(None)                 # a key was added: forced once more
    assert calls.count(True) == 2 and calls.count(False) == 3
    assert "NEW" not in (cfg.data_dir / "link_refresh_sources.done").read_text(encoding="utf-8")  # names, not keys


def test_core_backs_off_at_the_first_rate_limit_answer(cfg):
    class Limited(FakeHttp):
        def get_json(self, url, params=None, headers=None, attempts=None):
            if "api.core.ac.uk" in url:
                self.calls.append(("GET", url, params or {}))
                assert attempts == 1  # no hammering: one try per question
                resp = requests.Response()
                resp.status_code = 429
                raise requests.HTTPError("HTTP 429 (api.core.ac.uk)", response=resp)
            return super().get_json(url, params, headers, attempts)

    http = Limited(cfg)
    assert core_links(http, {"1": "10.1/a", "2": "10.1/b", "3": "10.1/c"}, "CKEY") == {}
    assert len([c for c in http.calls if "api.core.ac.uk" in c[1]]) == 1  # stopped after the first refusal


def test_a_long_link_search_does_not_lock_out_the_api(cfg, store):
    """While a slow source is being asked, another connection (the API recording n8n's report) must be able to write."""
    import sqlite3 as sq
    cfg.sources.openalex_api_key = "OKEY"
    put(store, "1", DOI_A)
    put(store, "2", DOI_B)
    outcome = {}

    class Slow(FakeHttp):
        def get_json(self, url, params=None, headers=None, attempts=None):
            if "api.openalex.org" in url:  # meanwhile the API writes
                other = sq.connect(cfg.data_dir / "pubmed.db", timeout=0.2)
                try:
                    other.execute("INSERT INTO downloads (pmid, version, status, first_attempt_at, last_attempt_at) "
                                  "VALUES ('x', 'vor', 'failed', 't', 't')")
                    other.commit()
                    outcome["write"] = "ok"
                except sq.OperationalError as exc:
                    outcome["write"] = str(exc)
                finally:
                    other.close()
            return super().get_json(url, params, headers, attempts)

    runner.refresh_links(cfg, http=Slow(cfg, openalex=OA), today=TODAY, force=True)
    assert outcome["write"] == "ok"


def test_api_recovers_after_a_write_that_hit_a_busy_database(cfg, store, monkeypatch):
    """A report refused while another writer holds the database must not poison the API's connection."""
    import sqlite3 as sq
    monkeypatch.setattr(Storage, "BUSY_TIMEOUT", 0.3)
    put(store, "8", "10.1/eight")
    cfg.api.port = 0
    server = api.make_server(cfg, host="127.0.0.1", http=FakeStreamHttp())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def report():
        body = json.dumps({"pmid": "8", "status": "failed", "stage": "download", "error": "x"}).encode()
        req = urllib.request.Request(base + "/downloads/report", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    try:
        urllib.request.urlopen(base + "/downloads?limit=1").read()  # the API connection has read something
        blocker = sq.connect(cfg.data_dir / "pubmed.db")
        blocker.execute("BEGIN IMMEDIATE")                  # e.g. the daily run writing
        blocker.execute("UPDATE articles SET title=title || ' (frissítve)' WHERE pmid='8'")  # a real change
        assert report() == 503                               # temporarily busy: a clear answer, not a crash
        urllib.request.urlopen(base + "/downloads?limit=1").read()  # a read while the other writer is still at it
        blocker.commit()
        blocker.close()
        assert report() == 200                               # and afterwards it works again, no restart needed
    finally:
        server.shutdown()
