"""pendant_direct.summarize: DeepSeek (Fireworks, mocked) by default, heuristic fallback."""
from __future__ import annotations

import json

import pytest

from pendant_direct import summarize as sm

TEXT = "[10:00] **Noah:** okay so we need to fix the [garage] door today. It's broken again! " \
       "Then call the plumber.\n\n[10:02] **Speaker A:** sure thing"


def test_heuristic_title_and_overview():
    s = sm.heuristic(TEXT)
    assert s.title == "Okay So We Need To Fix The Garage"
    assert s.overview == "okay so we need to fix the [garage] door today. It's broken again!"
    assert s.action_items == [] and s.category == ""


def test_heuristic_short_and_empty():
    assert sm.heuristic("[09:00] hi.").title == "Hi"
    assert sm.heuristic("").title == "Untitled conversation"


def test_clean_title_strips_heading_unsafe_chars():
    assert sm.clean_title("  #Plan | the [[thing]] ^now  ") == "Plan the thing now"
    long = "word " * 40
    assert len(sm.clean_title(long)) <= sm.MAX_TITLE


def test_default_without_a_deepseek_key_uses_the_heuristic():
    # conftest removes the Fireworks key, so the DeepSeek default falls back locally
    assert sm.summarize(TEXT, {}, log=lambda m: None) == sm.heuristic(TEXT)


def test_explicit_heuristic_never_calls_deepseek(monkeypatch):
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")

    def post(*a):
        raise AssertionError("must not be called")
    assert sm.summarize(TEXT, {"summarizer": "heuristic"}, post=post) == sm.heuristic(TEXT)


def test_fireworks_backend_parses_json_and_strips_think(monkeypatch):
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")
    sent = {}

    def post(url, headers, body):
        sent.update(url=url, auth=headers["Authorization"], model=body["model"])
        return {"choices": [{"finish_reason": "stop", "message": {"content":
                '<think>hmm</think>```json\n{"title": "Returning The Library Books", "overview": "They discussed it.",'
                ' "action_items": ["Renew the library books"], "category": "Personal"}\n```'}}]}
    s = sm.summarize("text", {"summarizer": "fireworks", "jev_action_items": False}, post=post)
    assert s.title == "Returning The Library Books"
    assert s.action_items == ["Renew the library books"]
    assert s.category == "personal"
    assert sent["auth"] == "Bearer k" and sent["model"].endswith("deepseek-v4p1-flash-1017")


def test_fireworks_failure_falls_back_to_heuristic(monkeypatch):
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")

    def boom(url, headers, body):
        raise OSError("down")
    logs = []
    s = sm.summarize("Hello there this is a test of the system.", {"summarizer": "fireworks"},
                     log=logs.append, post=boom)
    assert s.title and "failed" in logs[0]


def test_fireworks_without_key_falls_back(monkeypatch):
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "fireworks_key", lambda: None)
    s = sm.summarize("Hello there.", {"summarizer": "fireworks"}, log=lambda m: None)
    assert s.title


def test_action_items_capped_at_three():
    from pendant_direct import summarize as sm
    s = sm.parse_summary({"title": "T", "overview": "", "category": "",
                          "action_items": ["a", "b", "c", "d", "e"]})
    assert s.action_items == ["a", "b", "c"]


def test_fireworks_items_are_checked_by_jev(monkeypatch):
    from pendant_direct import jev
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")
    monkeypatch.setattr(jev, "api_key", lambda: "jk")

    def post(url, headers, body):
        return {"choices": [{"finish_reason": "stop", "message": {"content":
                '{"title": "Errands", "overview": "", "action_items": ["Call plumber", "Fix fridge"], "category": ""}'}}]}

    def jev_post(url, headers, body):
        return {"answers": {"item0": {"noul": 0.97}, "item1": {"noul": 0.05},
                            "later0": {"noul": 0.9}, "later1": {"noul": 0.9}}}
    s = sm.summarize("text", {"summarizer": "fireworks"}, post=post, jev_post=jev_post, log=lambda m: None)
    assert s.action_items == ["Call plumber"]


def test_default_and_legacy_claude_config_use_deepseek(monkeypatch):
    from pendant_direct import summarize as sm
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")
    calls = []

    def post(url, headers, body):
        calls.append(url)
        return {"choices": [{"finish_reason": "stop", "message": {"content":
                '{"title": "Errands", "overview": "", "action_items": [], "category": ""}'}}]}
    for cfg in ({}, {"summarizer": "claude"}, {"summarizer": "fireworks"}):
        assert sm.summarize("text", {**cfg, "jev_action_items": False}, post=post).title == "Errands"
    assert calls == [sm.FIREWORKS_URL] * 3


def test_no_anthropic_code_path():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "pendant_direct"
    for f in src.rglob("*.py"):
        assert "anthropic" not in f.read_text().lower(), f
