import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import FakeHttp

from pubmedwatch import api, runner
from pubmedwatch.config import load_config
from pubmedwatch.downloads import (issue_folder, journal_folder, organize_all, organize_file, pdf_filename,
                                   pdf_relpath, safe_name, safe_relpath, scan_pdf_dir)
from pubmedwatch.models import Article
from pubmedwatch.openaccess import OaLinks
from pubmedwatch.storage import Storage

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 4, 7, 0, tzinfo=TZ)


def stamp(dt):
    return dt.isoformat(timespec="seconds")


def art(**kw):
    base = dict(pmid="42825830", title="Early-onset neonatal sepsis: a Hungarian cohort", journal_abbrev="Pediatr Infect Dis J",
                journal="The Pediatric Infectious Disease Journal", pub_year="2026", volume="45", issue="10", section="gyermek-ams")
    base.update(kw)
    return base


# --- layout ------------------------------------------------------------------------------------
def test_journal_and_issue_folders():
    assert pdf_relpath(art(), ["journal", "issue"]) == (
        "Pediatr Infect Dis J/2026_vol-45_issue-10/2026-42825830-early-onset-neonatal-sepsis-a-hungarian-cohort.pdf")
    assert issue_folder(art(volume="", issue="")) == "2026_online-first"  # not yet assigned to an issue
    assert issue_folder(art(issue="3")) == "2026_vol-45_issue-03"  # issues sort in order
    assert issue_folder(art(issue="Suppl 1")) == "2026_vol-45_issue-Suppl-1"
    assert issue_folder(art(volume="", issue="7")) == "2026_issue-07"
    assert pdf_relpath(art(), ["section", "journal", "issue"]).startswith("gyermek-ams/Pediatr Infect Dis J/2026_vol-45")
    assert pdf_relpath(art(), ["year"]).startswith("2026/2026-")


def test_names_are_valid_on_windows_and_never_empty():
    assert journal_folder(art(journal_abbrev="J. Med: <Sci>/Res?")) == "J Med Sci Res"
    assert journal_folder(art(journal_abbrev="", journal="")) == "ismeretlen folyoirat"
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a b c d e f g h i j"
    assert safe_name("trailing dots...  ") == "trailing dots"
    assert len(safe_name("x" * 200)) == 60
    assert pdf_filename(art(title="Ékezetes cím: őűúóü!", pub_year="")) == "xxxx-42825830-ekezetes-cim-ouuou.pdf"
    with pytest.raises(ValueError):
        pdf_relpath(art(), ["journal", "bogus"])


def test_config_rejects_unknown_folder_kind(tmp_path):
    text = (load_config.__globals__["DEFAULT_CONFIG"]).read_text(encoding="utf-8")
    bad = tmp_path / "c.yaml"
    bad.write_text(text.replace("folders: [journal, issue]", "folders: [journal, volume]"), encoding="utf-8")
    with pytest.raises(ValueError, match="mappa-típus"):
        load_config(bad)


# --- disk scan ---------------------------------------------------------------------------------
def test_scan_finds_our_files_at_any_depth(tmp_path):
    for rel in ("Lancet/2026_vol-1_issue-02/2026-111-a.pdf", "iranyelvek/2026-222-b.pdf", "xxxx-333-c.pdf",
                "notes.pdf", "2026-abc-d.pdf", "Lancet/2026-444-e.txt"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF-1.4 test")
    found = {pmid: (rel, size) for pmid, _version, rel, size, _ in scan_pdf_dir(tmp_path)}
    assert set(found) == {"111", "222", "333"}
    assert found["111"] == ("Lancet/2026_vol-1_issue-02/2026-111-a.pdf", 13)
    assert scan_pdf_dir(tmp_path / "missing") == []


# --- the ledger --------------------------------------------------------------------------------
@pytest.fixture
def store(cfg):
    storage = Storage(cfg.data_dir)
    storage.run_id = storage.start_run("2026-10-01", "2026-10-04", False)
    yield storage
    storage.close()


def put(storage, pmid, *, source="pmc-s3", updated=NOW - timedelta(hours=3), **kw):
    a = Article(pmid=pmid, title=f"Title {pmid}", journal_abbrev="Pediatr Infect Dis J", pub_year="2026", **kw)
    storage.insert_article(a, "gyermek-ams", storage.run_id)
    if source:
        storage.update_oa(pmid, OaLinks(oa=True, url_pdf=f"https://x.org/{pmid}.pdf", pdf_source=source))
    storage.db.execute("UPDATE articles SET updated_at=? WHERE pmid=?", (stamp(updated), pmid))
    storage.commit()


def due(storage, now=NOW, hours=24):
    return {a["pmid"]: a["download"] for a in storage.downloads_due(now, hours, 365, lambda a, v: {"path": f"J/{a['pmid']}.pdf"})}


def test_only_fresh_scriptable_articles_are_offered(store):
    put(store, "1")                                              # new, scriptable
    put(store, "2", updated=NOW - timedelta(hours=30))           # older than the 24-hour window
    put(store, "3", source="europepmc")                          # link for people only
    put(store, "4", source="unpaywall-web")
    put(store, "5", source="unpaywall")
    put(store, "6", source="")                                   # no PDF link at all
    assert set(due(store)) == {"1", "5"}
    assert due(store)["1"] == {"version": "vor", "reason": "new", "attempts": 0, "last_error": "", "path": "J/1.pdf"}
    assert set(due(store, hours=48)) == {"1", "2", "5"}          # the window is a parameter, not a habit


def test_a_successful_download_is_never_offered_again(store):
    put(store, "1")
    store.report_download("1", "ok", NOW, 30, path="J/1.pdf", size=1000)
    assert due(store) == {} and due(store, now=NOW + timedelta(days=400), hours=24 * 500) == {}
    # a stray late failure report must not undo the success
    assert store.report_download("1", "failed", NOW + timedelta(hours=1), 30, error="late")["status"] == "ok"
    assert due(store, hours=24 * 500) == {}


def test_failed_downloads_come_back_monthly_for_a_year(store):
    put(store, "1")
    row = store.report_download("1", "failed", NOW, 30, error="HTTP 403")
    assert (row["attempts"], row["status"], row["last_error"]) == (1, "failed", "HTTP 403")
    assert row["next_retry_at"] == stamp(NOW + timedelta(days=30) - timedelta(hours=3))
    assert due(store, NOW + timedelta(days=1)) == {}             # not before the month is up
    assert due(store, NOW + timedelta(days=29, hours=20)) == {}
    # the daily run 30 days later starts a few seconds before the time this failure was reported: it still counts
    retry = due(store, NOW + timedelta(days=30) - timedelta(seconds=20))["1"]
    assert (retry["reason"], retry["attempts"], retry["last_error"]) == ("retry", 1, "HTTP 403")

    # failing again pushes the next try another month out and counts the attempt
    again = store.report_download("1", "failed", NOW + timedelta(days=31), 30, error="HTTP 500")
    assert again["attempts"] == 2 and again["first_attempt_at"] == stamp(NOW)
    assert due(store, NOW + timedelta(days=45)) == {}
    assert "1" in due(store, NOW + timedelta(days=61))

    # a year after the first attempt it gives up
    assert "1" in due(store, NOW + timedelta(days=364))
    assert due(store, NOW + timedelta(days=366)) == {}


def test_a_new_link_after_a_failure_is_tried_sooner(store):
    put(store, "1", source="unpaywall")
    store.report_download("1", "failed", NOW, 30, error="HTTP 403")
    later = NOW + timedelta(days=3)
    store.db.execute("UPDATE articles SET updated_at=? WHERE pmid='1'", (stamp(later - timedelta(hours=2)),))
    store.commit()
    assert due(store, later)["1"]["reason"] == "relinked"
    # ...but a link that stopped being scriptable is not offered
    store.db.execute("UPDATE articles SET pdf_source='unpaywall-web' WHERE pmid='1'")
    store.commit()
    assert due(store, later) == {}


def test_pdfs_already_on_disk_are_marked_done(store):
    put(store, "1")
    put(store, "2")
    found = [("1", "vor", "iranyelvek/2026-1-title.pdf", 5000, stamp(NOW - timedelta(days=1)))]
    assert store.reconcile_downloads(found, NOW) == 1
    assert store.reconcile_downloads(found, NOW) == 0            # idempotent
    assert set(due(store)) == {"2"}
    ledger = {d["pmid"]: d for d in store.downloads()}
    assert ledger["1"]["source"] == "disk" and ledger["1"]["bytes"] == 5000 and ledger["1"]["status"] == "ok"
    # a failed row is healed too when the file turns out to exist
    store.report_download("2", "failed", NOW, 30, error="x")
    assert store.reconcile_downloads([("2", "vor", "a/2026-2-t.pdf", 1, stamp(NOW))], NOW) == 1
    assert store.downloads(status="failed") == []
    assert store.counts()["downloads_ok"] == 2


# --- API ---------------------------------------------------------------------------------------
@pytest.fixture
def served(cfg, store, tmp_path):
    cfg.api.pdf_dir = tmp_path / "pdfs"
    (cfg.api.pdf_dir / "Old" / "2025_online-first").mkdir(parents=True)
    (cfg.api.pdf_dir / "Old" / "2025_online-first" / "2026-2-already-here.pdf").write_bytes(b"%PDF-1.7")
    recent = datetime.now().astimezone() - timedelta(hours=1)  # the API measures the 24-hour window from now
    put(store, "1", volume="45", issue="10", updated=recent)
    put(store, "2", updated=recent)
    return store


def call(cfg, storage, path, **params):
    status, ctype, body = api.handle(cfg, storage, path, {k: str(v) for k, v in params.items()})
    return json.loads(body)


def test_due_endpoint_gives_target_paths_and_skips_files_on_disk(cfg, served):
    items = call(cfg, served, "/downloads/due")
    assert [a["pmid"] for a in items] == ["1"]                   # 2 is already in the PDF folder
    assert items[0]["download"]["path"] == "_inbox/2026-1-title-1.pdf"            # where n8n saves it
    assert items[0]["download"]["final_path"] == "Pediatr Infect Dis J/2026_vol-45_issue-10/2026-1-title-1.pdf"
    assert items[0]["links"]["pdf"] == "https://x.org/1.pdf"
    assert items[0]["download"]["url"] == "https://x.org/1.pdf" and items[0]["download"]["version"] == "vor"


def test_report_endpoint_validates_and_records(cfg, served):
    def post(payload):
        return json.loads(api.handle_post(cfg, served, "/downloads/report", payload)[2])

    assert post({"pmid": "1", "status": "ok", "path": "J/x.pdf"})["status"] == "ok"
    assert call(cfg, served, "/downloads/due") == []
    for bad, code in (({"pmid": "x", "status": "ok"}, 400), ({"pmid": "1", "status": "maybe"}, 400),
                      ({"pmid": "999", "status": "ok"}, 404), ("not an object", 400)):
        with pytest.raises(api.ApiError) as err:
            post(bad)
        assert err.value.status == code
    with pytest.raises(api.ApiError):
        api.handle_post(cfg, served, "/articles", {})
    assert call(cfg, served, "/downloads", status="ok")[0]["pmid"] == "1"
    assert call(cfg, served, "/health")["counts"]["downloads_ok"] >= 1


def test_http_post_with_token(cfg, served):
    cfg.api.port, cfg.api.token = 0, "titok"
    server = api.make_server(cfg, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    body = json.dumps({"pmid": "1", "status": "failed", "error": "HTTP 403"}).encode()
    try:
        req = urllib.request.Request(base + "/downloads/report", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 401                              # no key, no write
        req = urllib.request.Request(base + "/downloads/report", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-API-Key": "titok"})
        row = json.load(urllib.request.urlopen(req))
        assert row["status"] == "failed" and row["attempts"] == 1
        bad = urllib.request.Request(base + "/downloads/report", data=b"{nope", method="POST",
                                     headers={"X-API-Key": "titok"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad)
        assert err.value.code == 400
    finally:
        server.shutdown()


# --- citation data -----------------------------------------------------------------------------
def test_old_database_gets_volume_and_issue_columns(tmp_path):
    path = tmp_path / "old"
    path.mkdir()
    db = sqlite3.connect(path / "pubmed.db")
    db.executescript("CREATE TABLE articles (pmid TEXT PRIMARY KEY, title TEXT NOT NULL, first_seen_at TEXT NOT NULL, "
                     "first_seen_run INTEGER NOT NULL, updated_at TEXT NOT NULL);"
                     "INSERT INTO articles VALUES ('7', 't', '2026-10-01T08:00:00+02:00', 1, '2026-10-01T08:00:00+02:00');")
    db.commit(); db.close()
    storage = Storage(path)
    columns = {r[1] for r in storage.db.execute("PRAGMA table_info(articles)")}
    assert {"volume", "issue", "bibl_checked_at"} <= columns
    assert storage.pending_bibliography("2026-10-04") == ["7"]    # never read yet: due for a refresh
    storage.close()


def test_refresh_bibliography_fills_issue_and_stops_asking(cfg, monkeypatch):
    monkeypatch.setattr(runner.mailer, "send_with_retry", lambda *a: None)
    http = FakeHttp(cfg, {"gyermek-ams": ["42825830"]})
    runner.run_once(cfg, today=date(2026, 10, 4), http=http)
    storage = Storage(cfg.data_dir)
    a = storage.article("42825830")
    assert "volume" in a and "issue" in a
    # pretend the record was stored before volume/issue were read
    storage.db.execute("UPDATE articles SET volume='', issue='', bibl_checked_at='' WHERE pmid='42825830'")
    storage.commit(); storage.close()
    assert runner.refresh_bibliography_job(cfg, http=http, today=date(2026, 10, 4)) in (0, 1)
    storage = Storage(cfg.data_dir)
    checked = storage.article("42825830")["bibl_checked_at"]
    assert checked
    # volume/issue known -> never asked again; unknown (online first) -> weekly
    storage.db.execute("UPDATE articles SET volume='45', issue='10' WHERE pmid='42825830'")
    storage.commit()
    assert storage.pending_bibliography("2026-12-01") == []
    storage.db.execute("UPDATE articles SET volume='', issue='' WHERE pmid='42825830'")
    storage.commit()
    assert storage.pending_bibliography(date.today().isoformat()) == []                     # checked today
    assert storage.pending_bibliography((date.today() + timedelta(days=8)).isoformat()) == ["42825830"]
    storage.close()


# --- filing the PDFs into journal / issue folders ----------------------------------------------
def make(root, rel, data=b"%PDF-1.7 x"):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def test_safe_relpath():
    assert safe_relpath("_inbox/2026-1-a.pdf") and safe_relpath("J/2026_vol-1/2026-1-a.pdf")
    for bad in ("", "/etc/passwd.pdf", "../x.pdf", "a/../../x.pdf", "C:/x.pdf", "\\\\nas\\x.pdf", "a/b.txt", "..\\x.pdf"):
        assert not safe_relpath(bad), bad


def test_organize_file_moves_creates_folders_and_tidies(tmp_path):
    make(tmp_path, "iranyelvek/2026-1-a.pdf")
    make(tmp_path, "iranyelvek/2026-2-other.pdf")
    final = organize_file(tmp_path, "iranyelvek/2026-1-a.pdf", "Lancet/2026_vol-1_issue-02/2026-1-a.pdf")
    assert final == "Lancet/2026_vol-1_issue-02/2026-1-a.pdf"
    assert (tmp_path / final).read_bytes().startswith(b"%PDF") and not (tmp_path / "iranyelvek/2026-1-a.pdf").exists()
    assert (tmp_path / "iranyelvek").is_dir()                      # still holds another file: not removed
    organize_file(tmp_path, "iranyelvek/2026-2-other.pdf", "Lancet/2026_vol-1_issue-02/2026-2-other.pdf")
    assert not (tmp_path / "iranyelvek").exists()                  # emptied by our move: tidied away
    assert tmp_path.is_dir()                                       # the root itself is never removed


def test_inbox_survives_being_emptied(tmp_path):
    make(tmp_path, "_inbox/2026-1-a.pdf")
    organize_file(tmp_path, "_inbox/2026-1-a.pdf", "J/2026_online-first/2026-1-a.pdf", keep_dirs=("_inbox",))
    assert (tmp_path / "_inbox").is_dir()                          # n8n needs it to exist for the next run


def test_organize_never_overwrites_or_escapes(tmp_path):
    root = tmp_path / "pdfs"
    make(root, "_inbox/2026-1-a.pdf", b"%PDF new")
    make(root, "J/2026_online-first/2026-1-a.pdf", b"%PDF existing")
    assert organize_file(root, "_inbox/2026-1-a.pdf", "J/2026_online-first/2026-1-a.pdf") == ""
    assert (root / "J/2026_online-first/2026-1-a.pdf").read_bytes() == b"%PDF existing"
    assert (root / "_inbox/2026-1-a.pdf").exists()                 # nothing was deleted either
    outside = make(tmp_path, "outside.pdf")
    assert organize_file(root, "../outside.pdf", "J/x/2026-9-x.pdf") == "" and outside.exists()
    assert organize_file(root, "_inbox/2026-1-a.pdf", "../escaped.pdf") == ""
    assert not (tmp_path / "escaped.pdf").exists()
    assert organize_file(root, "_inbox/missing.pdf", "J/x/2026-3-x.pdf") == ""   # nothing to move


def test_organize_ignores_symlinks(tmp_path):
    root = tmp_path / "pdfs"
    target = make(tmp_path, "elsewhere.pdf")
    (root / "_inbox").mkdir(parents=True)
    try:
        (root / "_inbox" / "2026-1-a.pdf").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this system")
    assert organize_file(root, "_inbox/2026-1-a.pdf", "J/x/2026-1-a.pdf") == "" and target.exists()


def test_organize_all_files_downloads_by_journal_and_issue(cfg, store, tmp_path):
    root = tmp_path / "pdfs"
    put(store, "1", volume="45", issue="10")
    put(store, "2")                                                 # online first: no volume / issue yet
    put(store, "3", volume="7", issue="1")
    for pmid in ("1", "2", "3"):
        make(root, f"_inbox/2026-{pmid}-title-{pmid}.pdf")
        store.report_download(pmid, "ok", NOW, 30, path=f"_inbox/2026-{pmid}-title-{pmid}.pdf")
    make(root, "Mine/2026-3-title-3.pdf")                           # the user moved 3 elsewhere by hand
    store.db.execute("UPDATE downloads SET path='Mine/2026-3-title-3.pdf' WHERE pmid='3'")  # ... and the ledger knows
    store.commit()
    (root / "Mine" / "2026-3-title-3.pdf").unlink()                 # ledger path no longer exists: leave it alone

    assert organize_all(store, root, ["journal", "issue"]) == 2
    assert (root / "Pediatr Infect Dis J/2026_vol-45_issue-10/2026-1-title-1.pdf").is_file()
    assert (root / "Pediatr Infect Dis J/2026_online-first/2026-2-title-2.pdf").is_file()
    assert not (root / "_inbox/2026-1-title-1.pdf").exists() and (root / "_inbox").is_dir()
    assert organize_all(store, root, ["journal", "issue"]) == 0     # nothing left to do
    ledger = {d["pmid"]: d["path"] for d in store.downloads()}
    assert ledger["1"].startswith("Pediatr Infect Dis J/2026_vol-45_issue-10/")

    # PubMed assigns the issue later: the file is re-filed, but only because it still sits where we put it
    store.db.execute("UPDATE articles SET volume='45', issue='11' WHERE pmid='2'")
    store.commit()
    assert organize_all(store, root, ["journal", "issue"]) == 1
    assert (root / "Pediatr Infect Dis J/2026_vol-45_issue-11/2026-2-title-2.pdf").is_file()
    assert not (root / "Pediatr Infect Dis J/2026_online-first").exists()    # emptied folder tidied
    assert organize_all(store, tmp_path / "no-such-folder", ["journal", "issue"]) == 0


def test_section_folders_from_the_first_layout_are_refiled(cfg, store, tmp_path):
    """The 19 PDFs saved before the journal/issue layout (in section folders) move over once."""
    root = tmp_path / "pdfs"
    put(store, "1", volume="3", issue="4")
    make(root, "iranyelvek/2026-1-title-1.pdf")
    assert store.reconcile_downloads(scan_pdf_dir(root), NOW) == 1
    assert organize_all(store, root, ["journal", "issue"]) == 1
    assert (root / "Pediatr Infect Dis J/2026_vol-3_issue-04/2026-1-title-1.pdf").is_file()
    assert not (root / "iranyelvek").exists()
    assert store.counts()["downloads_ok"] == 1 and set(due(store, hours=999)) == set()


def test_report_files_the_pdf_at_once_and_rejects_bad_paths(cfg, served, tmp_path):
    root = cfg.api.pdf_dir
    make(root, "_inbox/2026-1-title-1.pdf")
    row = json.loads(api.handle_post(cfg, served, "/downloads/report",
                                     {"pmid": "1", "status": "ok", "path": "_inbox/2026-1-title-1.pdf"})[2])
    assert row["path"] == "Pediatr Infect Dis J/2026_vol-45_issue-10/2026-1-title-1.pdf"
    assert (root / row["path"]).is_file() and not (root / "_inbox/2026-1-title-1.pdf").exists()
    for bad in ("../../etc/passwd.pdf", "/abs/x.pdf", "a/b.txt"):
        with pytest.raises(api.ApiError) as err:
            api.handle_post(cfg, served, "/downloads/report", {"pmid": "1", "status": "ok", "path": bad})
        assert err.value.status == 400


def test_a_save_problem_is_retried_tomorrow_not_next_month(cfg, served):
    row = json.loads(api.handle_post(cfg, served, "/downloads/report",
                                     {"pmid": "1", "status": "failed", "stage": "save", "error": "EACCES"})[2])
    retry = datetime.fromisoformat(row["next_retry_at"]) - datetime.fromisoformat(row["last_attempt_at"])
    assert retry == timedelta(days=1) - timedelta(hours=3)       # in time for tomorrow's run at the same hour
    row = json.loads(api.handle_post(cfg, served, "/downloads/report",
                                     {"pmid": "1", "status": "failed", "stage": "download", "error": "HTTP 403"})[2])
    gap = datetime.fromisoformat(row["next_retry_at"]) - datetime.fromisoformat(row["last_attempt_at"])
    assert gap == timedelta(days=30) - timedelta(hours=3)


# --- network failures: the next daily run tries again --------------------------------------------------------------
def test_a_download_the_network_cut_off_is_retried_at_the_next_daily_run(store):
    put(store, "1")
    afternoon = NOW.replace(hour=15, minute=42)
    row = store.report_download("1", "failed", afternoon, 30, error="aborted")
    assert row["next_retry_at"] == stamp(afternoon + timedelta(hours=12))
    tomorrow = NOW + timedelta(days=1)                           # the scheduled 07:00 run
    assert due(store, tomorrow)["1"]["reason"] == "retry"
    # three cut-off attempts in a row: from then on it waits for the monthly retry like any other failure
    store.report_download("1", "failed", tomorrow, 30, error="timeout of 120000ms exceeded")
    third = store.report_download("1", "failed", tomorrow + timedelta(days=1), 30, error="socket hang up")
    assert third["next_retry_at"] == stamp(tomorrow + timedelta(days=1, hours=12))
    fourth = store.report_download("1", "failed", tomorrow + timedelta(days=2), 30, error="aborted")
    assert fourth["attempts"] == 4
    assert fourth["next_retry_at"] == stamp(tomorrow + timedelta(days=32) - timedelta(hours=3))


def test_a_refusal_is_not_mistaken_for_a_network_failure(store):
    put(store, "1")
    for error in ('403 - "<!DOCTYPE html><html lang=\\"en-US', "nem PDF (text/html)",
                  '502 - {"error": "a forrás nem adta ki a PDF-et: HTTP 403 (api.wiley.com)"}'):
        row = store.report_download("1", "failed", NOW, 30, error=error)
        assert row["next_retry_at"] == stamp(NOW + timedelta(days=30) - timedelta(hours=3)), error
    busy = '502 - {"error": "a forrás nem adta ki a PDF-et: HTTP 429 (content.openalex.org)"}'
    store.db.execute("DELETE FROM downloads")
    assert store.report_download("1", "failed", NOW, 30, error=busy)["next_retry_at"] == stamp(NOW + timedelta(hours=12))


def test_after_a_restart_the_cut_off_downloads_are_offered_at_once(store):
    for pmid in ("1", "2", "3"):
        put(store, pmid)
    for pmid, error in (("1", "aborted"), ("2", "HTTP 403"), ("3", "timeout of 120000ms exceeded")):
        store._ledger_write({"pmid": pmid, "version": "vor", "status": "failed", "attempts": 1,
                             "first_attempt_at": stamp(NOW), "last_attempt_at": stamp(NOW),
                             "next_retry_at": stamp(NOW + timedelta(days=30)), "last_error": error, "path": "",
                             "bytes": None, "saved_at": "", "source": "n8n"})
    store.commit()
    restart = NOW + timedelta(hours=9)
    assert store.retry_transient_failures(restart) == 2
    assert store.retry_transient_failures(restart) == 0          # idempotent
    assert set(due(store, restart)) == {"1", "3"}                # the 403 still waits for its month


def test_retries_are_capped_per_run_but_new_articles_never(store):
    for n in range(1, 6):
        put(store, str(n), updated=NOW - timedelta(days=3))
        store.report_download(str(n), "failed", NOW - timedelta(days=3, minutes=n), 30, error="aborted")
    for n in range(6, 9):
        put(store, str(n))                                       # new within the 24-hour window
    later = NOW + timedelta(minutes=1)
    items = store.downloads_due(later, 24, 365, lambda a, v: {"path": f"J/{a['pmid']}.pdf"}, max_retries=2)
    reasons = [(a["pmid"], a["download"]["reason"]) for a in items]
    assert [p for p, r in reasons if r == "new"] == ["6", "7", "8"]
    assert [p for p, r in reasons if r == "retry"] == ["5", "4"]  # the longest-waiting first
    assert len(store.downloads_due(later, 24, 365, lambda a, v: {"path": "x"})) == 8  # no cap: everything


# --- one-off repair: PMC links that pointed at a supplement --------------------------------------------------------
def test_supplements_taken_for_the_article_are_set_aside_and_the_article_comes_again(cfg, store, tmp_path):
    cfg.api.pdf_dir = tmp_path / "pdfs"
    bucket = "https://pmc-oa-opendata.s3.amazonaws.com"
    put(store, "1", source="", pmcid="PMC101")
    put(store, "2", source="", pmcid="PMC202")
    put(store, "3", source="", pmcid="PMC303")
    for pmid, pmcid, name in (("1", "PMC101", "Data_Sheet_1.PDF"), ("2", "PMC202", "DataSheet1.pdf"),
                              ("3", "PMC303", "PMC303.1.pdf")):
        store.update_oa(pmid, OaLinks(oa=True, pmcid=pmcid, url_pdf=f"{bucket}/{pmcid}.1/{name}", pdf_source="pmc-s3"))
    saved = "Pediatr Infect Dis J/2026_online-first/2026-1-title-1.pdf"
    (cfg.api.pdf_dir / saved).parent.mkdir(parents=True)
    (cfg.api.pdf_dir / saved).write_bytes(b"%PDF-1.7 a data sheet")
    store.report_download("1", "ok", NOW, 30, path=saved, size=21)
    store.commit()

    http = FakeHttp(cfg, s3_pmcids=("PMC101",))                  # PMC has the article PDF of 1, none of 2
    later = NOW + timedelta(hours=2)
    assert runner.repair_pmc_supplements(cfg, http=http, now=later) == 2  # 3 was right all along
    a1, a2, a3 = (store.article(p) for p in ("1", "2", "3"))
    assert a1["links"]["pdf"] == f"{bucket}/PMC101.1/PMC101.1.pdf" and a1["pdf_source"] == "pmc-s3"
    assert a2["links"]["pdf"] == "" and a2["pdf_source"] == ""
    assert a3["links"]["pdf"] == f"{bucket}/PMC303.1/PMC303.1.pdf"
    # the data sheet is kept, but out of the way and under a name that is not counted as the article
    assert not (cfg.api.pdf_dir / saved).exists()
    assert (cfg.api.pdf_dir / "_mellekletek" / "1_Data_Sheet_1.PDF").read_bytes() == b"%PDF-1.7 a data sheet"
    assert (cfg.api.pdf_dir / "_mellekletek" / "OLVASS_EL.txt").exists()
    assert scan_pdf_dir(cfg.api.pdf_dir) == []
    store.reconcile_downloads(scan_pdf_dir(cfg.api.pdf_dir), later)
    assert due(store, later + timedelta(minutes=1))["1"]["reason"] == "retry"  # the article PDF is fetched again
    assert runner.repair_pmc_supplements(cfg, http=http, now=later) == 0       # nothing left to repair
