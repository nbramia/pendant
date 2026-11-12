"""DeepSeek summary cache, per-call token usage log and the feed's status totals."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from pendant_direct import feed, summarize as sm

CFG = {"summarizer": "fireworks", "jev_action_items": False}
REPLY = ('{"title": "Dinner Plans", "overview": "o", "action_items": ["Call Sam"], "category": "family",'
         ' "kind": "conversation", "importance": 1}')

# sha256(SYSTEM + SCHEMA). If this fails you changed the prompt or schema: bump
# summarize.PROMPT_VERSION (old cache entries must stop matching), then update this hash.
PINNED = ("PROMPT_VERSION", "1", "09a09f389c5eba55")   # template with {W}; renders as before for an unchanged wearer


def con_():
    con = sqlite3.connect(":memory:")
    con.executescript(sm.SQL)
    return con


class Post:
    def __init__(self, reply=REPLY, usage=None, finish="stop"):
        self.calls, self.reply, self.finish = 0, reply, finish
        self.usage = usage if usage is not None else {
            "prompt_tokens": 498, "completion_tokens": 359,
            "completion_tokens_details": {"reasoning_tokens": 288},
            "prompt_tokens_details": {"cached_tokens": 100}}

    def __call__(self, url, headers, body):
        self.calls += 1
        return {"choices": [{"finish_reason": self.finish, "message": {"content": self.reply}}],
                "usage": self.usage}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")


def test_second_identical_text_is_a_cache_hit_without_a_request():
    con, post = con_(), Post()
    a = sm.summarize("hello there", CFG, post=post, con=con)
    b = sm.summarize("hello there", CFG, post=post, con=con)
    assert post.calls == 1 and a == b
    assert (b.title, b.action_items, b.kind, b.importance) == ("Dinner Plans", ["Call Sam"], "conversation", 1)
    assert con.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1   # a hit costs nothing


def test_key_is_sha256_of_model_prompt_version_and_text():
    expect = hashlib.sha256("\0".join(("m", sm.PROMPT_VERSION, "t")).encode()).hexdigest()
    assert sm.cache_key("m", "t") == expect


def test_changed_text_model_or_prompt_version_misses(monkeypatch):
    con, post = con_(), Post()
    sm.summarize("one", CFG, post=post, con=con)
    sm.summarize("two", CFG, post=post, con=con)
    sm.summarize("one", {**CFG, "fireworks_model": "other"}, post=post, con=con)
    assert post.calls == 3
    monkeypatch.setattr(sm, "PROMPT_VERSION", "999")
    sm.summarize("one", CFG, post=post, con=con)
    assert post.calls == 4


def test_jev_still_runs_on_a_cache_hit(monkeypatch):
    from pendant_direct import jev
    seen = []
    monkeypatch.setattr(jev, "keep_explicit", lambda text, items, log=print, post=None, **kw: seen.append(items) or [])
    con, post = con_(), Post()
    cfg = {"summarizer": "fireworks", "jev_action_items": True}
    sm.summarize("x", cfg, post=post, con=con)
    s = sm.summarize("x", cfg, post=post, con=con)
    assert post.calls == 1 and seen == [["Call Sam"], ["Call Sam"]] and s.action_items == []
    # the cache holds DeepSeek's raw items, not Jev's verdict
    assert json.loads(con.execute("SELECT summary FROM summaries").fetchone()[0])["action_items"] == ["Call Sam"]


def test_heuristic_fallbacks_are_not_cached_and_retry():
    con = con_()
    bad = Post(reply="not json")
    s = sm.summarize("some words here", CFG, post=bad, con=con, log=lambda m: None)
    assert s == sm.heuristic("some words here")
    assert con.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
    untitled = Post(reply=REPLY.replace("Dinner Plans", ""))
    sm.summarize("some words here", CFG, post=untitled, con=con, log=lambda m: None)
    assert con.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
    good = Post()
    assert sm.summarize("some words here", CFG, post=good, con=con).title == "Dinner Plans"
    assert good.calls == 1


def test_explicit_heuristic_touches_neither_cache_nor_usage():
    con, post = con_(), Post()
    sm.summarize("hi", {"summarizer": "heuristic"}, post=post, con=con)
    assert post.calls == 0
    assert con.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0


def test_usage_row_per_call_with_token_split():
    con = con_()
    sm.summarize("hello", CFG, post=Post(), con=con, purpose="backfill_kind")
    ts, purpose, model, p, c, r, k, ok = con.execute("SELECT * FROM llm_usage").fetchone()
    assert abs(ts - time.time()) < 5
    assert (purpose, model, p, c, r, k, ok) == ("backfill_kind", sm.FIREWORKS_MODEL, 498, 359, 288, 100, 1)


def test_failed_calls_are_logged_with_ok_zero():
    con = con_()
    sm.summarize("a", CFG, post=Post(finish="length"), con=con, log=lambda m: None)   # cut off: tokens known
    def boom(*a):
        raise OSError("down")
    sm.summarize("b", CFG, post=boom, con=con, log=lambda m: None)
    rows = con.execute("SELECT prompt_tokens, ok FROM llm_usage ORDER BY rowid").fetchall()
    assert rows == [(498, 0), (0, 0)]


def test_missing_usage_block_logs_zeros():
    con = con_()
    sm.summarize("a", CFG, post=Post(usage={}), con=con)
    assert con.execute("SELECT prompt_tokens, completion_tokens, ok FROM llm_usage").fetchone() == (0, 0, 1)


def test_broken_database_never_blocks_a_summary():
    con = sqlite3.connect(":memory:")   # no tables
    msgs = []
    s = sm.summarize("hello", CFG, post=Post(), con=con, log=msgs.append)
    assert s.title == "Dinner Plans" and msgs


def test_no_connection_means_no_caching():
    post = Post()
    sm.summarize("a", CFG, post=post)
    sm.summarize("a", CFG, post=post)
    assert post.calls == 2


def test_prompt_version_is_pinned_to_the_prompt():
    digest = hashlib.sha256((sm.SYSTEM + json.dumps(sm.SCHEMA, sort_keys=True)).encode()).hexdigest()[:16]
    assert (sm.PROMPT_VERSION, digest) == PINNED[1:], (
        "SYSTEM or SCHEMA changed: bump summarize.PROMPT_VERSION, then update PINNED in this test")


def test_process_db_has_the_tables_and_keeps_done_marks(tmp_path, monkeypatch):
    from pendant_direct import process
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    con = process.db()
    con.execute("INSERT INTO chunks (device, page, ord, opus_sha, ts_ms, opus, done) VALUES ('D',1,0,'s',5,x'00',1)")
    con.commit()
    con.close()
    con = process.db()   # reopen: migrations rerun
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"summaries", "llm_usage"} <= names
    assert con.execute("SELECT done FROM chunks").fetchone() == (1,)


def test_status_totals(tmp_path, monkeypatch):
    from pendant_direct import process
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    con = process.db()
    now = time.time()
    rows = [(now, "summary", "m", 1000, 500, 300, 0, 1),
            (now - 3600 * 24 * 3, "summary", "m", 2000, 1000, 600, 0, 1),
            (now - 3600 * 24 * 30, "summary", "m", 9999, 9999, 9999, 0, 1)]
    con.executemany("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    st = feed.status_payload()
    # "today" is since local midnight; the 3-day-old row is only in the 7-day window
    assert st["llm_today"]["calls"] == 1 and st["llm_today"]["prompt_tokens"] == 1000
    assert st["llm_7d"] == {"calls": 2, "prompt_tokens": 3000, "completion_tokens": 1500,
                            "reasoning_tokens": 900, "cached_tokens": 0,
                            "usd": round((3000 * 0.30 + 1500 * 1.20) / 1e6, 4)}


def test_status_rates_are_configurable(tmp_path, monkeypatch):
    from pendant_direct import process
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    (tmp_path / "config.json").write_text('{"llm": {"usd_per_m_prompt": 1, "usd_per_m_completion": 2}}')
    con = process.db()
    con.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?)", (time.time(), "summary", "m", 1_000_000, 1_000_000, 0, 0, 1))
    con.commit()
    con.close()
    assert feed.status_payload()["llm_7d"]["usd"] == 3.0


def test_status_with_an_old_database_without_usage_table(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    con = sqlite3.connect(tmp_path / "pages.db")
    con.execute("CREATE TABLE chunks (device TEXT, ts_ms INT, done INT)")
    con.commit()
    con.close()
    assert feed.status_payload()["llm_7d"]["calls"] == 0
