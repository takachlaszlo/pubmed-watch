from datetime import date, timedelta

from conftest import FakeHttp, fixture_bytes, fixture_json

from pubmedwatch import runner
from pubmedwatch.config import load_config
from pubmedwatch.models import Article
from pubmedwatch.openaccess import parse_unpaywall, unpaywall_links
from pubmedwatch.pubmed import parse_efetch
from pubmedwatch.storage import Storage

DAY = date(2026, 10, 4)


def fx(name):
    return fixture_json(f"unpaywall_{name}.json")["response"]


def test_parse_real_answers():
    links, cands = parse_unpaywall(fx("pdf_ok"))
    assert links.oa and cands and "springer" in cands[0] and links.url_pdf == cands[0] and links.url_fulltext

    links, cands = parse_unpaywall(fx("landing_only"))  # open access, but only an HTML page: no PDF address
    assert links.oa and not cands and links.url_pdf == "" and links.url_fulltext.startswith("http")

    links, cands = parse_unpaywall(fx("closed"))
    assert not links.oa and not cands

    # a copy that lives only on Europe PMC / PMC is not an extra source: those pages are browser-gated
    _, cands = parse_unpaywall(fx("epmc_location"))
    assert all("ncbi.nlm.nih.gov" not in c and "europepmc.org" not in c for c in cands)


def test_classification_by_one_probe():
    answers = {"10.1/ok": fx("pdf_ok"), "10.1/gated": fx("pdf_gated_by_publisher"), "10.1/page": fx("landing_only"),
               "10.1/closed": fx("closed")}
    http = FakeHttp(load_config(), unpaywall=answers, pdf_hosts=("link.springer.com",))
    found = unpaywall_links(http, {"1": "10.1/ok", "2": "10.1/gated", "3": "10.1/page", "4": "10.1/closed"}, "x@y.z")
    assert (found["1"].pdf_source, bool(found["1"].url_pdf)) == ("unpaywall", True)
    assert found["2"].pdf_source == "unpaywall-web" and found["2"].url_pdf  # kept as a link for people
    assert found["3"].pdf_source == "" and found["3"].url_pdf == "" and found["3"].url_fulltext  # open page only
    assert "4" not in found
    assert len(http.probes) == 2  # one polite request per candidate, never repeated against a refusal


def test_known_link_is_not_probed_again():
    http = FakeHttp(load_config(), unpaywall={"10.1/gated": fx("pdf_gated_by_publisher")})
    first = unpaywall_links(http, {"2": "10.1/gated"}, "x@y.z")
    stored = first["2"].url_pdf
    http.probes.clear()
    again = unpaywall_links(http, {"2": "10.1/gated"}, "x@y.z", known_pdf={"2": stored})
    assert http.probes == [] and again["2"].url_pdf == ""  # nothing new to store


def test_runner_uses_unpaywall_and_s3_still_wins(cfg, monkeypatch):
    monkeypatch.setattr(runner.mailer, "send_with_retry", lambda *a: None)
    cfg.sources.unpaywall_email = "x@y.z"
    art = {a.pmid: a for a in parse_efetch(fixture_bytes("efetch.xml"))}["42825830"]  # not in Europe PMC's OA set
    http = FakeHttp(cfg, {"gyermek-ams": ["42825830"]}, unpaywall={art.doi: fx("pdf_ok")}, pdf_hosts=("link.springer.com",))
    runner.run_once(cfg, today=DAY, http=http)
    storage = Storage(cfg.data_dir)
    a = storage.article("42825830")
    assert a["pdf_source"] == "unpaywall" and "springer" in a["links"]["pdf"] and a["oa"]
    assert storage.articles(pdf_source="unpaywall")[0]["pmid"] == "42825830"
    storage.close()

    # a PMC copy appearing later is better: it replaces the Unpaywall link, no longer rechecked after that
    pmcid = "PMC99999999"
    storage = Storage(cfg.data_dir)
    storage.db.execute("UPDATE articles SET pmcid=? WHERE pmid='42825830'", (pmcid,))
    storage.db.execute("UPDATE articles SET pdf_source='unpaywall-web' WHERE pmid='42825830'")  # pretend it was link-only
    storage.commit(); storage.close()
    http2 = FakeHttp(cfg, s3_pmcids=(pmcid,), europepmc={"resultList": {"result": []}})
    runner.refresh_links(cfg, http=http2, today=DAY, force=True)
    storage = Storage(cfg.data_dir)
    assert storage.article("42825830")["pdf_source"] == "pmc-s3"
    storage.close()


def test_link_only_sources_are_not_selected_for_download(cfg, monkeypatch):
    """The n8n workflow downloads pdf_source in (pmc-s3, unpaywall) only; link-only sources must differ."""
    monkeypatch.setattr(runner.mailer, "send_with_retry", lambda *a: None)
    cfg.sources.unpaywall_email = "x@y.z"
    art = {a.pmid: a for a in parse_efetch(fixture_bytes("efetch.xml"))}["42825830"]
    http = FakeHttp(cfg, {"gyermek-ams": ["42825830"]}, unpaywall={art.doi: fx("pdf_gated_by_publisher")})
    runner.run_once(cfg, today=DAY, http=http)
    storage = Storage(cfg.data_dir)
    a = storage.article("42825830")
    assert a["pdf_source"] == "unpaywall-web" and a["links"]["pdf"]  # shown in the mail, not for the script
    storage.close()


def test_pending_back_off(cfg):
    """Rechecks: daily for the first 7 days, weekly up to 30 days, then monthly, for a year."""
    storage = Storage(cfg.data_dir)
    run = storage.start_run("2026-01-01", "2026-10-04", False)
    today = date(2026, 10, 4)
    cases = {  # pmid: (age in days, days since the last check or None)
        "1": (2, None), "2": (2, 1), "3": (2, 0), "4": (12, 6), "5": (12, 7), "6": (40, 29), "7": (40, 30),
        "8": (200, 31), "9": (200, 10), "10": (400, None)}
    for pmid, (age, checked) in cases.items():
        storage.insert_article(Article(pmid=pmid, title=pmid), "innovacio", run)
        first = (today - timedelta(days=age)).isoformat() + "T08:00:00+02:00"
        last = (today - timedelta(days=checked)).isoformat() + "T08:00:00+02:00" if checked is not None else ""
        storage.db.execute("UPDATE articles SET first_seen_at=?, oa_checked_at=? WHERE pmid=?", (first, last, pmid))
    storage.commit()
    since = (today - timedelta(days=365)).isoformat()
    due = {p for p, *_ in storage.pending_oa(since, today.isoformat())}
    assert due == {"1", "2", "5", "7", "8"}  # the 400-day-old one is past the year
    forced = {p for p, *_ in storage.pending_oa(since, today.isoformat(), force=True)}
    assert forced == {"1", "2", "3", "4", "5", "6", "7", "8", "9"}
    storage.close()
