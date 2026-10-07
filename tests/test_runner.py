from datetime import date

import pytest

from conftest import FakeHttp

from pubmedwatch import runner
from pubmedwatch.storage import Storage

DAY1, DAY2, DAY3 = date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(runner.mailer, "send_with_retry", lambda mail, subject, html, text: sent.append((subject, html, text)))
    return sent


def test_baseline_then_daily(cfg, mails):
    hits = {"gyermek-ams": ["42825830", "42803594"], "gyermek-iranyelv-review": ["42803594", "42816283"],
            "protokoll": ["42810927"]}
    first = runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, hits))
    assert first.baseline and first.window == (date(2026, 9, 27), DAY1)
    assert (first.new_articles, first.new_trials) == (4, 3)
    assert len(mails) == 1 and "elindult" in mails[0][0]
    assert "pubmed.ncbi.nlm.nih.gov/42825830" in mails[0][1]  # the first mail carries real content, not just counts

    storage = Storage(cfg.data_dir)
    a = storage.article("42803594")
    assert a["topics"] == ["gyermek-ams", "gyermek-iranyelv-review"]
    assert a["section"] == "gyermek-ams"  # AMS outranks the review topic
    assert storage.article("42816283")["section"] == "iranyelvek"
    assert storage.article("42810927")["section"] == "vizsgalatok"
    assert storage.article("42825830")["links"]["pubmed"] == "https://pubmed.ncbi.nlm.nih.gov/42825830/"
    storage.close()

    # next day: one known article shows up in another topic, one new article appears
    hits2 = {"gyermek-ams": ["42825830"], "innovacio": ["42825830", "42812206"]}
    second = runner.run_once(cfg, today=DAY2, http=FakeHttp(cfg, hits2, trials={"studies": []}))
    assert not second.baseline and second.window == (date(2026, 10, 2), DAY2)
    assert (second.new_articles, second.new_trials) == (1, 0)
    assert len(mails) == 2
    subject, html, text = mails[1]
    assert "1 új tétel" in subject
    assert "pubmed.ncbi.nlm.nih.gov/42812206" in html and "42825830" not in html  # only new items are mailed
    storage = Storage(cfg.data_dir)
    assert storage.article("42825830")["topics"] == ["gyermek-ams", "innovacio"]
    storage.close()

    # nothing new: no mail (send_empty is false)
    third = runner.run_once(cfg, today=DAY3, http=FakeHttp(cfg, hits2, trials={"studies": []}))
    assert third.new_articles == 0 and len(mails) == 2


def test_open_access_links_reach_report_and_db(cfg, mails):
    runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, {"protokoll": ["42814651"]}))
    storage = Storage(cfg.data_dir)
    a = storage.article("42814651")
    assert a["oa"] and a["links"]["pdf"].endswith("?pdf=render") and a["pdf_source"] == "europepmc"
    assert storage.articles(has_pdf=True)[0]["pmid"] == "42814651"
    storage.close()
    html = (cfg.data_dir / "last_report.html").read_text(encoding="utf-8")
    assert "első futás" in html


def test_failed_run_does_not_advance_window(cfg, mails):
    runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, {}))
    with pytest.raises(RuntimeError):
        runner.run_once(cfg, today=DAY2, http=FakeHttp(cfg, {}, fail_search=True))
    storage = Storage(cfg.data_dir)
    assert storage.runs()[0]["status"] == "error"
    since, until, baseline = runner.search_window(storage, cfg, DAY3)
    assert (since, until, baseline) == (date(2026, 10, 2), DAY3, False)  # still counted from the last good run
    storage.close()


def test_webhook_payload(cfg, mails, monkeypatch):
    cfg.api.webhook_url = "http://n8n.local/webhook/pubmed"
    http = FakeHttp(cfg, {"gyermek-ams": ["42825830"]}, trials={"studies": []})
    runner.run_once(cfg, today=DAY1, http=http)
    url, payload = http.posted[0]
    assert url == cfg.api.webhook_url
    assert payload["event"] == "pubmed-watch.run" and payload["counts"]["articles"] == 1
    article = payload["articles"][0]
    assert article["pmid"] == "42825830" and set(article["links"]) == {"pubmed", "doi", "fulltext", "pdf", "preprint_pdf"}


def test_mail_failure_keeps_data(cfg, monkeypatch):
    def boom(*args):
        raise OSError("mail.home.arpa nem válaszol")
    monkeypatch.setattr(runner.mailer, "send_with_retry", boom)
    result = runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, {"gyermek-ams": ["42825830"]}))
    assert not result.mail_sent
    storage = Storage(cfg.data_dir)
    run = storage.runs()[0]
    assert run["status"] == "ok" and "levélküldés sikertelen" in run["message"]
    storage.close()


def test_baseline_mail_shows_recent_days_in_full(cfg, mails):
    cfg.baseline_digest_days = 0  # only the run day itself: the fixtures entered PubMed on 09-28 .. 10-02
    runner.run_once(cfg, today=date(2026, 10, 2), http=FakeHttp(cfg, {"gyermek-ams": ["42825830", "42803594"]}))
    subject, html, text = mails[0]
    assert "42825830" in html and "42803594" not in html  # entrez 10-02 shown, 09-28 only in the database
    assert "2 tétel került az adatbázisba" in html or "tétel került az adatbázisba" in html
    storage = Storage(cfg.data_dir)
    assert storage.counts()["articles"] == 2
    storage.close()


def test_pmc_s3_pdf_beats_europepmc_and_marks_downloadable(cfg, mails):
    hits = {"protokoll": ["42814651"], "gyermek-ams": ["42803594"]}
    http = FakeHttp(cfg, hits, s3_pmcids=("PMC13626073",))
    runner.run_once(cfg, today=DAY1, http=http)
    storage = Storage(cfg.data_dir)
    a = storage.article("42814651")
    assert a["pdf_source"] == "pmc-s3"
    assert a["links"]["pdf"] == "https://pmc-oa-opendata.s3.amazonaws.com/PMC13626073.1/PMC13626073.1.pdf"
    assert storage.articles(pdf_source="pmc-s3")[0]["pmid"] == "42814651"
    storage.close()


def test_refresh_links_upgrades_europepmc_link_later(cfg, mails):
    runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, {"protokoll": ["42814651"]}))  # not in S3 yet
    storage = Storage(cfg.data_dir)
    assert storage.article("42814651")["pdf_source"] == "europepmc"
    storage.close()
    assert runner.refresh_links(cfg, http=FakeHttp(cfg, s3_pmcids=("PMC13626073",)), today=DAY1, force=True) == 1
    storage = Storage(cfg.data_dir)
    assert storage.article("42814651")["pdf_source"] == "pmc-s3"
    storage.close()
    # once on the best source the daily link search does not look at it again
    http = FakeHttp(cfg, s3_pmcids=("PMC13626073",))
    assert runner.refresh_links(cfg, http=http, today=DAY1) == 0 and not http.calls  # the daily run leaves it alone


def test_send_digest_from_database(cfg, mails):
    runner.run_once(cfg, today=DAY1, http=FakeHttp(cfg, {"gyermek-ams": ["42825830", "42803594"]}))
    mails.clear()
    digest = runner.send_digest(cfg, days=3, today=date(2026, 10, 3))
    assert "összesítő az utolsó 3 napról" in digest.subject and "42825830" in digest.html
    assert len(mails) == 1
