"""pendant_direct.highlights: notable rules, event notes, daily Highlights."""
from __future__ import annotations

import datetime as dt
import json

from pendant_direct import highlights as hl

TZ = hl.TZ
T0 = int(dt.datetime(2026, 1, 11, 16, 15, tzinfo=TZ).timestamp() * 1000)
RULES = {"duration_minutes": 25, "action_items_min": 2, "keywords": ["Budget"]}


def notable(**kw):
    args = dict(key="D-1", audio_s=60, action_items=[], title="t", overview="o", rules=RULES, overrides={})
    args.update(kw)
    return hl.is_notable(**args)


def test_notable_rules():
    assert not notable()
    assert notable(audio_s=25 * 60)
    assert not notable(audio_s=25 * 60 - 1)
    assert notable(action_items=["a", "b"])
    assert not notable(action_items=["a"])
    assert notable(overview="the budget talk")
    assert not notable(overrides={"D-1": False}, audio_s=3600)
    assert notable(overrides={"D-1": True})


def test_load_overrides(tmp_path):
    assert hl.load_overrides(tmp_path / "missing.json") == {}
    (tmp_path / "o.json").write_text(json.dumps({"D-1": 1, "D-2": False}))
    assert hl.load_overrides(tmp_path / "o.json") == {"D-1": True, "D-2": False}
    (tmp_path / "bad.json").write_text("[1,2]")
    assert hl.load_overrides(tmp_path / "bad.json") == {}


def test_slugify_and_event_filename():
    assert hl.slugify("Tense Exchange: About Apologies!") == "tense-exchange-about-apologies"
    assert hl.slugify("") == "untitled"
    assert hl.event_filename("F00D", T0, "Café Plans") == f"2026-01-11T161500 - cafe-plans - F00D-{T0}.md"


def conv(start_ms, title, *, notable=0, items=(), overview="", event_file=None, people=()):
    t = dt.datetime.fromtimestamp(start_ms / 1000, TZ)
    return {"device": "F00D", "start_ms": start_ms, "end_ms": start_ms + 60_000, "audio_s": 1800.0,
            "day": f"{t:%Y-%m-%d}", "pendant": "old", "title": title, "overview": overview,
            "action_items": json.dumps(list(items)), "category": "family", "people": json.dumps(list(people)),
            "notable": notable, "raw_heading": f"{t:%H:%M} — {title} (30m00s) · old",
            "event_file": event_file, "clip": "/x.opus"}


def test_render_event():
    c = conv(T0, "Tense Exchange", notable=1, items=["Apologize"], overview="They argued.", people=["Noah"])
    text = hl.render_event(c)
    assert text.startswith("---\ncategory: 'family'\ndate: '2026-01-11'\nduration_minutes: 30\n")
    assert "pendant: 'old'\npendant_id: 'F00D'\npeople:\n- 'Noah'\n" in text
    assert "raw_link: '[[2026-01-11#16:15 — Tense Exchange (30m00s) · old]]'" in text
    assert "source: 'pendant'" in text
    assert "\n# Tense Exchange\n\n## Summary\n\nThey argued.\n\n## Action Items\n\n- [ ] Apologize\n" in text
    assert text.endswith("## Link to Raw\n\n[[2026-01-11#16:15 — Tense Exchange (30m00s) · old]]\n")
    bare = hl.render_event(conv(T0, "Quiet", overview=""))
    assert "*No summary available.*" in bare and "*No action items.*" in bare


def test_render_highlights_format():
    ev = "2026-01-11T161500 - tense-exchange - F00D-1.md"
    convs = [conv(T0, "Tense Exchange", notable=1, overview="They argued.", event_file=ev,
                  items=["Apologize", "Call mom"]),
             conv(T0 - 3_600_000, "Morning Coffee")]
    text = hl.render_highlights("2026-01-11", convs)
    assert text.startswith("---\ndate: '2026-01-11'\npeople: []\nsource: 'pendant'\n"
                           "timezone: 'America/New_York'\ntype: 'highlights'\n---\n")
    assert "# Highlights — 2026-01-11\n\n## Notable Events\n\n" \
           "- 16:15 — [[2026-01-11T161500 - tense-exchange - F00D-1]]\n  - They argued.\n" in text
    assert "## Action Items\n\n- [ ] Apologize (16:15, [[2026-01-11#16:15 — Tense Exchange (30m00s) · old]])\n" \
           "- [ ] Call mom (16:15," in text
    allc = text.split("## All Conversations\n\n")[1]
    assert allc.splitlines() == [
        "- 15:15 — Morning Coffee → [[2026-01-11#15:15 — Morning Coffee (30m00s) · old]]",
        "- 16:15 — Tense Exchange → [[2026-01-11#16:15 — Tense Exchange (30m00s) · old]]"
        " | [[2026-01-11T161500 - tense-exchange - F00D-1]]"]
    # order of input doesn't matter: output is deterministic
    assert hl.render_highlights("2026-01-11", list(reversed(convs))) == text


def test_render_highlights_empty_sections():
    text = hl.render_highlights("2026-01-11", [conv(T0, "Hi")])
    assert "## Notable Events\n\n*No notable events.*\n" in text
    assert "## Action Items\n\n*No action items.*\n" in text


def test_write_if_changed(tmp_path):
    p = tmp_path / "a" / "b.md"
    assert hl.write_if_changed(p, "x")
    assert not hl.write_if_changed(p, "x")
    assert hl.write_if_changed(p, "y") and p.read_text() == "y"


def test_render_highlights_starred_moments():
    c = conv(T0, "Tense Exchange")
    link = "[[2026-01-11#16:15 — Tense Exchange (30m00s) · old]]"
    stars = [{"ts_ms": T0 + 125_000, "raw_heading": c["raw_heading"], "text": "**Noah:** That's the key point."},
             {"ts_ms": T0 + 5_000, "raw_heading": c["raw_heading"], "text": ""}]
    text = hl.render_highlights("2026-01-11", [c], stars)
    assert (f"## Notable Events\n\n*No notable events.*\n\n## Starred Moments\n\n"
            f"- 16:15 — {link}\n- 16:17 — {link} — **Noah:** That's the key point.\n\n## Action Items") in text
    assert "## Starred Moments\n\n*No starred moments.*\n" in hl.render_highlights("2026-01-11", [c])
