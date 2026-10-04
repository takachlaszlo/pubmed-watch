import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime

import pytest

from conftest import FakeHttp

from pubmedwatch import api, report, runner
from pubmedwatch.models import Article
from pubmedwatch.openaccess import OaLinks
from pubmedwatch.scheduler import next_target
from pubmedwatch.storage import Storage


@pytest.fixture
def filled(cfg, monkeypatch):
    monkeypatch.setattr(runner.mailer, "send_with_retry", lambda *a: None)
    hits = {"gyermek-ams": ["42825830"], "gyermek-iranyelv-review": ["42816283", "42803594"],
            "protokoll": ["42814651", "42810927"], "innovacio": ["42812206"]}
    runner.run_once(cfg, today=date(2026, 10, 4), http=FakeHttp(cfg, hits))
    storage = Storage(cfg.data_dir)
    yield storage
    storage.close()


def test_update_oa_only_moves_on_change(cfg):
    storage = Storage(cfg.data_dir)
    run_id = storage.start_run("2026-10-01", "2026-10-04", False)
    storage.insert_article(Article(pmid="1", title="T"), "innovacio", run_id)
    before = storage.article("1")["updated_at"]
    assert storage.update_oa("1", OaLinks()) is False
    assert storage.update_oa("1", OaLinks(oa=True, url_pdf="https://x/a.pdf", pdf_source="unpaywall")) is True
    a = storage.article("1")
    assert a["links"]["pdf"] == "https://x/a.pdf" and a["pdf_source"] == "unpaywall" and a["updated_at"] >= before
    assert storage.add_topics("1", ["innovacio", "innovacio"], run_id) == ["innovacio"]
    assert storage.add_topics("1", ["innovacio"], run_id) == []
    storage.close()


def test_report_daily_layout(cfg, filled):
    articles = filled.articles(limit=1000)
    trials = filled.trials(limit=1000)
    digest = report.build(cfg, date(2026, 10, 5), (date(2026, 10, 3), date(2026, 10, 5)), articles, trials)
    html = digest.html
    assert digest.subject.startswith("PubMed-figyelő 2026.10.05.")
    assert "2026. október 5., hétfő" in html
    # section order as configured, trials section holds CT.gov entries and protocols
    positions = [html.index(f'id="{s.id}"') for s in cfg.sections]
    assert positions == sorted(positions)
    assert "clinicaltrials.gov/study/NCT07854847" in html
    assert "PDF ↓" in html and "?pdf=render" in html
    assert "Ma nincs új tétel." in html  # e.g. the infection review section is empty here
    assert "PubMed: https://pubmed.ncbi.nlm.nih.gov/" in digest.text


def test_report_escapes_html(cfg):
    a = {"pmid": "9", "title": "<script>x</script> & co", "authors": [], "journal_abbrev": "J", "journal": "J",
         "pub_year": "2026", "pub_types": [], "kind": "other", "is_update": False, "topics": ["innovacio"],
         "section": "innovacio", "abstract": [],
         "links": {"pubmed": "https://pubmed.ncbi.nlm.nih.gov/9/", "doi": "", "fulltext": "", "pdf": ""}}
    html = report.build(cfg, date(2026, 10, 5), (date(2026, 10, 3), date(2026, 10, 5)), [a], []).html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_conclusion_extraction():
    structured = [{"label": "BACKGROUND", "text": "Bg."}, {"label": "CONCLUSIONS", "text": "Do less."}]
    assert report.conclusion(structured) == "Do less."
    plain = [{"label": "", "text": "One. Two. Three."}]
    assert report.conclusion(plain) == "Two. Three."
    assert report.conclusion([{"label": "", "text": "word " * 200}]).endswith("…")


def test_api_routes(cfg, filled):
    def get(path, **params):
        status, ctype, body = api.handle(cfg, filled, path, {k: str(v) for k, v in params.items()})
        return status, json.loads(body) if ctype.startswith("application/json") else body

    assert get("/health")[1]["counts"]["articles"] == 6
    assert {a["pmid"] for a in get("/articles", section="vizsgalatok")[1]} == {"42814651", "42810927"}
    with_pdf = get("/articles", has_pdf="true")[1]
    assert "42814651" in {a["pmid"] for a in with_pdf} and all(a["links"]["pdf"] for a in with_pdf)
    assert all(a["links"]["pdf"] == "" for a in get("/articles", has_pdf="false")[1])
    assert get("/articles", topic="gyermek-ams")[1][0]["pmid"] == "42825830"
    assert get("/articles", limit=2)[1].__len__() == 2
    assert get("/articles/42825830")[1]["journal_abbrev"] == "Eur J Pediatr"
    assert get("/trials")[1][0]["url"].startswith("https://clinicaltrials.gov/study/")
    assert get("/runs/latest")[1]["status"] == "ok"
    assert get("/topics")[1]["topics"][0]["id"] == "protokoll"
    for path, params in (("/articles/1", {}), ("/trials/NCT0", {}), ("/x", {})):
        with pytest.raises(api.ApiError) as err:
            get(path, **params)
        assert err.value.status == 404
    with pytest.raises(api.ApiError) as err:
        get("/articles", limit="sok")
    assert err.value.status == 400


def test_api_server_token(cfg, filled):
    cfg.api.port, cfg.api.token = 0, "titok"
    server = api.make_server(cfg, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/health")
        assert err.value.code == 401
        req = urllib.request.Request(base + "/articles?limit=1", headers={"X-API-Key": "titok"})
        data = json.load(urllib.request.urlopen(req))
        assert len(data) == 1 and "links" in data[0]
    finally:
        server.shutdown()


def test_next_target():
    assert next_target(datetime(2026, 10, 4, 5, 0), "06:30") == datetime(2026, 10, 4, 6, 30)
    assert next_target(datetime(2026, 10, 4, 7, 0), "06:30") == datetime(2026, 10, 5, 6, 30)
