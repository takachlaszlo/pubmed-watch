from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from pubmedwatch.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
ALL_PMIDS = ["42816283", "42805330", "42803594", "42814651", "42810927", "42825830", "42812206", "42805644"]


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch, tmp_path):
    for name in ("NCBI_API_KEY", "UNPAYWALL_EMAIL", "N8N_WEBHOOK_URL", "API_TOKEN", "PUBMEDWATCH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBMEDWATCH_DATA", str(tmp_path / "data"))


@pytest.fixture
def cfg():
    return load_config()


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name).decode("utf-8"))


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", status: int = 200):
        self._payload = payload
        self.content = content or (json.dumps(payload).encode() if payload is not None else b"")
        self.status_code = status

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Stands in for HttpClient. `topic_hits` maps a topic id to the PMIDs its search returns."""

    def __init__(self, cfg, topic_hits: dict[str, list[str]] | None = None, trials: dict | None = None,
                 europepmc: dict | None = None, fail_search: bool = False):
        self.query_to_topic = {t.query: t.id for t in cfg.topics}
        self.topic_hits = topic_hits or {}
        self.trials = trials if trials is not None else fixture_json("ctgov.json")
        self.europepmc = europepmc if europepmc is not None else fixture_json("europepmc.json")
        self.fail_search = fail_search
        self.calls: list[tuple[str, str, dict]] = []
        self.posted: list[tuple[str, dict]] = []

    def request(self, method, url, data=None, params=None, **kwargs):
        self.calls.append((method, url, data or params or {}))
        path = urlsplit(url).path
        if path.endswith("esearch.fcgi"):
            if self.fail_search:
                raise RuntimeError("PubMed nem elérhető")
            ids = self.topic_hits.get(self.query_to_topic.get(data["term"]), [])
            return FakeResponse({"esearchresult": {"count": str(len(ids)), "idlist": ids}})
        if path.endswith("efetch.fcgi"):
            wanted = set(data["id"].split(","))
            root = ET.fromstring(fixture_bytes("efetch.xml"))
            for node in list(root):
                if node.findtext("MedlineCitation/PMID") not in wanted:
                    root.remove(node)
            return FakeResponse(content=ET.tostring(root))
        raise AssertionError(f"váratlan kérés: {method} {url}")

    def get_json(self, url, params=None):
        self.calls.append(("GET", url, params or {}))
        if "europepmc" in url:
            return self.europepmc
        if "clinicaltrials.gov" in url:
            return self.trials
        if "unpaywall" in url:
            return {"is_oa": False}
        raise AssertionError(f"váratlan kérés: GET {url}")

    def post_json(self, url, payload):
        self.posted.append((url, payload))
        return FakeResponse({})
