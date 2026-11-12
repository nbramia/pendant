"""day_signals: DeepSeek daily signal (faked), wheel validation, input bound, recompute, feed route."""
from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request

import pytest

from pendant_direct import day_signals as ds, feed, process, summarize

TZ = ds.TZ
NOW = dt.datetime(2026, 11, 10, 18, 0, tzinfo=TZ)
DAY = "2026-11-10"


def ms(h, m, day=(2026, 11, 10)):
    return int(dt.datetime(*day, h, m, tzinfo=TZ).timestamp() * 1000)


class Post:
    def __init__(self, reply):
        self.reply, self.bodies = reply, []

    def __call__(self, url, headers, body):
        self.bodies.append(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.reply)}}], "usage": {}}


NULL = {"has_emotional_moment": False, "path": [], "valence": "mixed", "reason": "", "confidence": 0,
        "at": "", "resonant": "", "words": []}


def moment(path, valence, reason="Tense call about the move", conf=0.9, at="15:04", **kw):
    return {"has_emotional_moment": True, "path": path, "valence": valence, "reason": reason,
            "confidence": conf, "at": at, "resonant": "A hard but honest afternoon.", "words": ["Steady", "Busy"], **kw}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(summarize, "fireworks_key", lambda: "k")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    monkeypatch.setenv("PENDANT_DATA", str(data))
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    return process.db(), notes


def add_conv(con, notes, hh, mm, text, title="Chat", importance=0, kind="conversation", day=DAY, notable=0):
    start = ms(hh, mm, tuple(int(x) for x in day.split("-")))
    heading = f"{hh:02d}:{mm:02d} — {title} (1m00s) · pen"
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day, pendant, title, raw_heading, "
                "kind, importance, notable) VALUES ('D', ?, ?, 60, ?, 'pen', ?, ?, ?, ?, ?)",
                (start, start + 60000, day, title, heading, kind, importance, notable))
    con.commit()
    f = notes / f"{day}.md"
    old = f.read_text() if f.exists() else ""
    f.write_text(old + f"\n## {heading}\n\n[{hh:02d}:{mm:02d}] {text}\n\n---\n")
    return start


CFG = {"summarizer": "fireworks"}


def stored(con, day=DAY):
    return ds.read(con, day)


def test_null_day(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "Let's get lunch and fix the gate.")
    post = Post(NULL)
    assert ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW) == 1
    s = stored(con)
    assert s["emotional_moment"] is None and s["resonant"] is None and s["words"] == []
    assert len(post.bodies) == 1, "yesterday had no conversations: no model call"
    assert stored(con, "2026-11-09") is None, "no recordings: no row"
    assert "Return a small private signal" in post.bodies[0]["messages"][0]["content"]
    assert "unmistakably" in post.bodies[0]["messages"][0]["content"].lower()


def test_negative_moment(env):
    con, notes = env
    add_conv(con, notes, 15, 4, "I can't do this, I'm overwhelmed.")
    ds.compute(con, notes, CFG, log=lambda m: None, post=Post(moment(["Fearful", "Anxious", "Overwhelmed"], "negative")), now=NOW)
    m = stored(con)["emotional_moment"]
    assert m == {"path": ["Fearful", "Anxious", "Overwhelmed"], "valence": "negative",
                 "reason": "Tense call about the move", "confidence": 0.9, "at": "15:04"}
    assert stored(con)["words"] == ["Steady", "Busy"] and stored(con)["resonant"]


def test_positive_moment(env):
    con, notes = env
    add_conv(con, notes, 12, 0, "We got the house!")
    ds.compute(con, notes, CFG, log=lambda m: None,
               post=Post(moment(["Happy", "Proud"], "positive", "Proud of the finished build", 0.85, "12:00")), now=NOW)
    m = stored(con)["emotional_moment"]
    assert m["valence"] == "positive" and m["path"] == ["Happy", "Proud"] and m["confidence"] == 0.85


@pytest.mark.parametrize("path", [["Fearful", "Joyful"], ["Nope"], ["Happy", "Proud", "Overwhelmed"], [],
                                  ["Fearful", "Anxious", "Overwhelmed", "Extra"], "Happy"])
def test_invalid_path_is_dropped(env, path):
    con, notes = env
    add_conv(con, notes, 12, 0, "text")
    ds.compute(con, notes, CFG, log=lambda m: None, post=Post(moment(path, "positive")), now=NOW)
    s = stored(con)
    assert s["emotional_moment"] is None
    assert s["words"] == ["Steady", "Busy"], "the rest of the signal survives"


def test_field_limits(env):
    con, notes = env
    add_conv(con, notes, 12, 0, "text")
    reply = moment(["Sad"], "mixed", "x" * 300, 7, "soon", resonant="y" * 500,
                   words=["Calm", "two words", "Averyveryverylongwordindeed", "Calm", "Tired", "Glad"])
    ds.compute(con, notes, CFG, log=lambda m: None, post=Post(reply), now=NOW)
    s = stored(con)
    assert len(s["emotional_moment"]["reason"]) == 120 and s["emotional_moment"]["confidence"] == 1.0
    assert s["emotional_moment"]["at"] is None and len(s["resonant"]) == 200
    assert s["words"] == ["Calm", "Tired", "Glad"]


def test_wheel_matches_home_vocabulary():
    w = ds.wheel()
    assert len(w) == 7 and sum(len(c[1]) for c in w) == 41 and sum(len(s[1]) for c in w for s in c[1]) == 82
    assert ds.valid_path(["Surprised", "Amazed", "In awe"]) and not ds.valid_path(["Surprised", "Amazed", "Eager"])


def test_input_is_bounded_and_prefers_important(env, monkeypatch):
    con, notes = env
    monkeypatch.setattr(ds, "MAX_INPUT_CHARS", 3000)
    monkeypatch.setattr(ds, "MAX_CONV_CHARS", 1500)
    add_conv(con, notes, 8, 0, "routine " * 400, title="Routine A")
    add_conv(con, notes, 9, 0, "routine " * 400, title="Routine B")
    add_conv(con, notes, 10, 0, "starred " * 400, title="Starred", notable=1)
    add_conv(con, notes, 11, 0, "important " * 400, title="Important", importance=2)
    add_conv(con, notes, 12, 0, "media " * 50, title="Show", kind="media")
    post = Post(NULL)
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    text = post.bodies[0]["messages"][1]["content"]
    assert len(text) <= 3000 + 200
    assert "Important" in text and "Starred" in text
    assert "Routine A" not in text and "Routine B" not in text, "lower-ranked conversations are left out"
    assert "Show" not in text, "only kind=conversation"
    assert text.index("Starred") < text.index("Important"), "kept ones read in time order"


def test_recompute_only_when_conversation_set_changes(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "first")
    post = Post(NULL)
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    assert len(post.bodies) == 1, "unchanged set: no second call"
    add_conv(con, notes, 10, 0, "second")
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    assert len(post.bodies) == 1, "a changed set waits out the refresh interval"
    con.execute("UPDATE day_signals SET computed_at = computed_at - ?", (ds.MIN_REFRESH_SECONDS + 1,))
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    assert len(post.bodies) == 2
    assert con.execute("SELECT COUNT(*) FROM day_signals WHERE day=?", (DAY,)).fetchone()[0] == 1


def test_only_today_and_yesterday(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "old", day="2026-11-05")
    post = Post(NULL)
    ds.compute(con, notes, CFG, log=lambda m: None, post=post, now=NOW)
    assert post.bodies == [] and stored(con, "2026-11-05") is None


def test_failure_logs_category_only_and_writes_nothing(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "SECRET TRANSCRIPT TEXT")
    logs = []

    def boom(url, headers, body):
        raise RuntimeError("SECRET TRANSCRIPT TEXT")
    assert ds.compute(con, notes, CFG, log=logs.append, post=boom, now=NOW) == 0
    assert stored(con) is None
    assert logs and all("SECRET" not in m for m in logs) and "RuntimeError" in logs[0]


def test_heuristic_summarizer_skips(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "x")
    assert ds.compute(con, notes, {"summarizer": "heuristic"}, post=Post(NULL), now=NOW) == 0


def test_usage_logged(env):
    con, notes = env
    add_conv(con, notes, 9, 0, "x")
    ds.compute(con, notes, CFG, log=lambda m: None, post=Post(NULL), now=NOW)
    assert con.execute("SELECT purpose, ok FROM llm_usage").fetchall() == [("day_signals", 1)]


def test_feed_route(env):
    con, notes = env
    add_conv(con, notes, 15, 4, "text")
    ds.compute(con, notes, CFG, log=lambda m: None, post=Post(moment(["Sad", "Lonely"], "negative")), now=NOW)
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed/day-signals"

    def get(q, token="tok"):
        req = urllib.request.Request(base + q, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    try:
        code, out = get(f"?day={DAY}")
        assert code == 200 and out["day"] == DAY and out["emotional_moment"]["path"] == ["Sad", "Lonely"]
        assert set(out) == {"day", "emotional_moment", "resonant", "words", "computed_at"}
        for q, status in (("?day=2026-12-01", 404), ("?day=nope", 400), ("", 400)):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(q)
            assert e.value.code == status
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"?day={DAY}", token="wrong")
        assert e.value.code == 401
    finally:
        srv.shutdown()
