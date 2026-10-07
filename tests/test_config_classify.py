import pytest

from conftest import fixture_bytes

from pubmedwatch.classify import classify, kind_of, section_of
from pubmedwatch.config import expand
from pubmedwatch.models import Article
from pubmedwatch.pubmed import parse_efetch


def test_real_config_expands_completely(cfg):
    assert [t.id for t in cfg.topics][0] == "protokoll"
    for topic in cfg.topics:
        assert "{" not in topic.query and "}" not in topic.query
        assert topic.query.count("(") == topic.query.count(")"), topic.id
        assert cfg.section(topic.section) is not None
    assert cfg.section("iranyelvek").style == "full"
    assert cfg.trials.enabled and cfg.trials.statuses


def test_expand_nested_and_unknown():
    blocks = {"a": "(x OR {b})", "b": "y[ti]"}
    assert expand("{a} AND z", blocks) == "(x OR y[ti]) AND z"
    with pytest.raises(ValueError):
        expand("{missing}", blocks)
    with pytest.raises(ValueError):
        expand("{loop}", {"loop": "{loop}"})


def art(title, pub_types=(), abstract=()):
    return Article(pmid="1", title=title, pub_types=list(pub_types), abstract=list(abstract))


@pytest.mark.parametrize("title,pub_types,kind", [
    ("Clinical Practice Guideline for antibacterial prophylaxis: 2026 Update", (), "guideline"),
    ("Management of febrile infants", ("Practice Guideline",), "guideline"),
    ("Consensus statement on the management of achondroplasia", (), "guideline"),
    ("Adherence to the 2023 IDSA guideline for UTI in children", (), "other"),
    ("An investigation into the compliance with antibiotic treatment guidelines", (), "other"),
    ("Treat-to-target recommendations in juvenile arthritis in clinical practice", (), "other"),
    ("Probiotics in infants: a systematic review and meta-analysis", (), "systematic_review"),
    ("PROA-KIDDDs study protocol: evaluation of a stewardship programme", (), "protocol"),
    ("Exercise protocols for children with cancer", (), "other"),
    ("Short vs long antibiotic course in CAP", ("Randomized Controlled Trial",), "rct"),
])
def test_kind(title, pub_types, kind):
    assert kind_of(art(title, pub_types)) == kind


def test_update_flag():
    assert classify(art("Clinical Practice Guideline ... 2026 Update")).is_update
    assert classify(art("Revised recommendations for RSV prophylaxis")).is_update
    assert not classify(art("Up-to-date review of nothing")).is_update


def test_fixture_kinds():
    kinds = {a.pmid: kind_of(a) for a in parse_efetch(fixture_bytes("efetch.xml"))}
    assert kinds["42805330"] == "other"  # compliance study, not a guideline
    assert kinds["42810927"] == "protocol"
    assert kinds["42803594"] == "systematic_review"
    assert kinds["42812206"] == "rct"


def test_section_routing(cfg):
    assert section_of(cfg, "guideline", ["innovacio"]) == "iranyelvek"
    # priority follows topic order in config.yaml
    assert section_of(cfg, "other", ["innovacio", "gyermek-ams"]) == "gyermek-ams"
    assert section_of(cfg, "protocol", ["gyermek-ams", "protokoll"]) == "vizsgalatok"
    assert section_of(cfg, "systematic_review", ["infektologia-iranyelv-review"]) == "review-infektologia"


def test_retry_cap_comes_from_the_config(cfg):
    assert cfg.downloads.max_retries_per_run == 100
