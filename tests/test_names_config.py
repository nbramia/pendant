"""pendant_direct.names and pendant_direct.config."""
from __future__ import annotations

import json

from pendant_direct import config, names


def test_default_names(tmp_path):
    n = names.load(tmp_path)
    assert names.name_for("0F1E2D3C4B5A6978", n) == "old"
    assert names.name_for("a1b2c3d4e5f60718", n) == "new"
    assert names.name_for("ABCDEF0123456789", n) == "ABCDEF0123456789"


def test_names_file_overrides_and_adds(tmp_path):
    (tmp_path / "pendants.json").write_text(json.dumps({"0f1e2d3c4b5a6978": "blue", "AAAA": "spare", "BBBB": " "}))
    n = names.load(tmp_path)
    assert n["0F1E2D3C4B5A6978"] == "blue"
    assert n["A1B2C3D4E5F60718"] == "new"
    assert n["AAAA"] == "spare"
    assert "BBBB" not in n


def test_bad_names_file_keeps_defaults(tmp_path):
    (tmp_path / "pendants.json").write_text("{nope")
    assert names.load(tmp_path) == names.DEFAULTS


def test_config_defaults_and_deep_merge(tmp_path):
    c = config.load(tmp_path)
    assert c["summarizer"] == "fireworks"
    assert c["speakers"]["enabled"] is True and c["speakers"]["allowed"] == ["Noah"]
    (tmp_path / "config.json").write_text(json.dumps(
        {"summarizer": "heuristic", "speakers": {"allowed": ["Noah", "Alex"]}, "notable": {"keywords": ["x"]}}))
    c = config.load(tmp_path)
    assert c["summarizer"] == "heuristic"
    assert c["speakers"]["allowed"] == ["Noah", "Alex"]
    assert c["speakers"]["enabled"] is True          # untouched keys keep defaults
    assert c["notable"]["duration_minutes"] == 25
    assert config.DEFAULTS["speakers"]["allowed"] == ["Noah"]  # defaults not mutated
