import json

import pytest

from pubmedwatch import api, scheduler, selfupdate
from pubmedwatch.config import load_config
from pubmedwatch.storage import Storage

SHA_A = "a" * 40
SHA_B = "b" * 40


class Answer:
    def __init__(self, text):
        self.text = text


class GitHub:
    def __init__(self, answer=SHA_A, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def request(self, method, url, headers=None, attempts=None, **kwargs):
        self.calls.append((url, headers))
        if self.error:
            raise self.error
        return Answer(self.answer + "\n")


@pytest.fixture(autouse=True)
def _fresh_process():
    selfupdate.running.update(sha="", started_at="")
    yield
    selfupdate.running.update(sha="", started_at="")


def test_the_newest_commit_is_asked_from_github():
    gh = GitHub()
    assert selfupdate.latest_sha(gh, "takachlaszlo/pubmed-watch", "main") == SHA_A
    url, headers = gh.calls[0]
    assert url == "https://api.github.com/repos/takachlaszlo/pubmed-watch/commits/main"
    assert headers["Accept"] == "application/vnd.github.sha"
    assert selfupdate.latest_sha(GitHub(answer="<html>rate limited</html>"), "r", "b") == ""
    assert selfupdate.latest_sha(GitHub(error=RuntimeError("offline")), "r", "b") == ""


def test_restarts_only_for_a_known_newer_version(cfg):
    selfupdate.remember_start(GitHub(SHA_A), cfg.update)
    assert selfupdate.running["sha"] == SHA_A and selfupdate.running["started_at"]
    assert selfupdate.newer_version(GitHub(SHA_A), cfg.update) == ""                    # nothing new
    assert selfupdate.newer_version(GitHub(error=RuntimeError("away")), cfg.update) == ""  # GitHub away: stay
    assert selfupdate.newer_version(GitHub(SHA_B), cfg.update) == SHA_B


def test_never_restarts_blind(cfg):
    selfupdate.remember_start(GitHub(error=RuntimeError("offline")), cfg.update)  # unknown since the start
    assert selfupdate.newer_version(GitHub(SHA_B), cfg.update) == ""
    assert selfupdate.running["sha"] == SHA_B                                    # known from now on
    assert selfupdate.newer_version(GitHub(SHA_B), cfg.update) == ""


def test_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AUTO_UPDATE", "false")
    cfg = load_config()
    assert not cfg.update.enabled
    assert (cfg.update.repo, cfg.update.branch) == ("takachlaszlo/pubmed-watch", "main")
    gh = GitHub(SHA_B)
    selfupdate.remember_start(gh, cfg.update)
    assert selfupdate.newer_version(gh, cfg.update) == "" and gh.calls == []


def test_the_process_exits_for_a_newer_version_only(monkeypatch):
    selfupdate.running["sha"] = SHA_A
    exits = []
    monkeypatch.setattr(scheduler.os, "_exit", exits.append)
    monkeypatch.setattr(scheduler.logging, "shutdown", lambda: None)
    monkeypatch.setattr(scheduler, "make_http", lambda cfg: GitHub(SHA_A))
    scheduler._update_if_newer(None)
    assert exits == []                                                           # same version: carries on
    monkeypatch.setattr(scheduler, "make_http", lambda cfg: GitHub(SHA_B))
    scheduler._update_if_newer(None)
    assert exits == [0]                                                          # Docker starts it again


def test_health_tells_the_running_version(cfg):
    selfupdate.running.update(sha=SHA_A, started_at="2026-10-07T17:00:00+02:00")
    storage = Storage(cfg.data_dir)
    try:
        body = api.handle(cfg, storage, "/health", {})[2]
    finally:
        storage.close()
    assert json.loads(body)["version"] == {"sha": SHA_A, "started_at": "2026-10-07T17:00:00+02:00"}
