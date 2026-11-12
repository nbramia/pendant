"""pendant_direct.tasks and POST /pendant/feed/tasks: tasks only when Noah asks."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from pendant_direct import feed, jev, process, tasks

HEADING = "16:15 — Fix The Garage Door (26m03s) · old"
DAY = "2026-11-09"


class FakeLifeOS:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, path, body, timeout=10.0):
        if self.fail:
            raise self.fail("down")
        self.calls.append((path, body))
        return {"id": f"t{len(self.calls)}", "success": True}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    con = process.db()
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day, pendant, title, overview, "
                "action_items, category, people, notable, raw_heading, clip) "
                "VALUES ('DEV', 5, 9, 60, ?, 'old', 'Fix', '', '[\"Buy hinge\"]', '', '[]', 0, ?, '/x')",
                (DAY, HEADING))
    con.commit()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / f"{DAY}.md").write_text(f"# t\n\n## {HEADING}\n\n[16:15] **Noah:** I need to fix it. "
                                     "Add buy coffee filters to my to-do list.\n\n"
                                     "[16:16] **Sam:** Sure.\n\n---\n")
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setenv("PENDANT_DATA", str(data))
    return con, notes


def test_request_creates_once_with_link_and_source(env):
    con, _ = env
    api = FakeLifeOS()
    a = tasks.request_task(con, "DEV", 5, "Buy hinge", "app", api)
    b = tasks.request_task(con, "DEV", 5, "Buy hinge", "app", api)
    assert a == b == {"status": "created", "task_id": "t1"}
    assert len(api.calls) == 1
    path, body = api.calls[0]
    assert path == "/api/tasks" and body["description"] == "Buy hinge"
    assert f"[[{DAY}#{HEADING}]]" in body["notes"] and body["fields"] == {"source": "pendant"}
    assert tasks.tasked_items(con, "DEV", 5) == ["Buy hinge"]


def test_unknown_conversation(env):
    with pytest.raises(KeyError):
        tasks.request_task(env[0], "DEV", 6, "x", "app", FakeLifeOS())


def test_lifeos_down_keeps_pending_and_retries(env):
    con, _ = env
    out = tasks.request_task(con, "DEV", 5, "Buy hinge", "app", FakeLifeOS(tasks.LifeOSDown))
    assert out == {"status": "pending", "task_id": None}
    assert tasks.tasked_items(con, "DEV", 5) == ["Buy hinge"]
    api = FakeLifeOS()
    assert tasks.retry_pending(con, api) == 1 and len(api.calls) == 1
    assert tasks.retry_pending(con, api) == 0
    assert con.execute("SELECT status FROM task_requests").fetchone()[0] == "created"


def test_rejection_is_failed_and_not_retried(env):
    con, _ = env
    assert tasks.request_task(con, "DEV", 5, "x", "app", FakeLifeOS(tasks.LifeOSRejected))["status"] == "failed"
    api = FakeLifeOS()
    assert tasks.retry_pending(con, api) == 0 and not api.calls
    assert tasks.tasked_items(con, "DEV", 5) == []


@pytest.mark.parametrize("text,item", [
    ("Add buy coffee filters to my to-do list.", "buy coffee filters"),
    ("Please remind me to call the plumber tomorrow.", "call the plumber tomorrow"),
    ("Put renew passport on my list.", "renew passport"),
    ("We should email Sam. Add that to my task list.", "We should email Sam"),
    ("Make that a task.", None),
    ("Please make renew passport a task.", "renew passport"),
    ("Call the dentist. Make that a task.", "Call the dentist"),
    ("Put it on my list. ", None),
    ("Pick up the cake. Put it on my list.", "Pick up the cake"),
    ("Fix the gate. Add that to my to-do list.", "Fix the gate"),
])
def test_candidate_extraction(text, item):
    got = tasks.candidates([text])
    assert [g[0] for g in got] == ([item] if item else [])


def test_plain_mentions_are_not_candidates():
    for t in ("I have a lot on my to-do list.", "My list is huge today.", "The task is hard.",
              "Sam will remind everyone."):
        assert tasks.candidates([t]) == []


def test_only_wearer_paragraphs_when_labels_exist():
    paras = tasks.parse_paragraphs(["[16:15] **Noah:** Hello there.", "[16:16] **Sam:** Add milk to my list.",
                                    "Also recorded in [[x]]"])
    assert tasks.wearer_paragraphs(paras, "Noah") == ["Hello there."]
    plain = tasks.parse_paragraphs(["[16:15] Add milk to my list."])
    assert tasks.wearer_paragraphs(plain, "Noah") == []


def test_spoken_pass_threshold_line_and_idempotence(env):
    con, notes = env
    api = FakeLifeOS()
    seen = []

    def ask(span, item):
        seen.append(item)
        return 0.9
    assert tasks.spoken_pass(con, notes, "Noah", ask=ask, post=api, log=lambda m: None) == {DAY}
    assert seen == ["buy coffee filters"] and len(api.calls) == 1
    text = (notes / f"{DAY}.md").read_text()
    assert text.count("Added to tasks: buy coffee filters") == 1
    # scanned: a second run does nothing, and the line helper is idempotent
    tasks.spoken_pass(con, notes, "Noah", ask=ask, post=api, log=lambda m: None)
    tasks.add_task_line(notes / f"{DAY}.md", HEADING, "buy coffee filters")
    assert len(api.calls) == 1 and (notes / f"{DAY}.md").read_text().count("Added to tasks:") == 1


def test_spoken_pass_below_threshold_creates_nothing(env):
    con, notes = env
    api = FakeLifeOS()
    assert tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 0.79, post=api, log=lambda m: None) == set()
    assert not api.calls and "Added to tasks" not in (notes / f"{DAY}.md").read_text()


def test_jev_failure_is_retried_next_run(env):
    con, notes = env
    api = FakeLifeOS()

    def boom(s, i):
        raise OSError("jev down")
    tasks.spoken_pass(con, notes, "Noah", ask=boom, post=api, log=lambda m: None)
    assert not api.calls
    tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 1.0, post=api, log=lambda m: None)
    assert len(api.calls) == 1


def test_spoken_request_survives_lifeos_outage(env):
    con, notes = env
    tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 1.0, post=FakeLifeOS(tasks.LifeOSDown),
                      log=lambda m: None)
    assert con.execute("SELECT status FROM task_requests").fetchone()[0] == "pending"
    api = FakeLifeOS()
    tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 1.0, post=api, log=lambda m: None)
    assert len(api.calls) == 1


def test_no_jev_key_means_no_spoken_tasks(env):
    con, notes = env  # conftest removes the key
    assert tasks.spoken_pass(con, notes, "Noah", post=FakeLifeOS(), log=lambda m: None) == set()
    assert con.execute("SELECT COUNT(*) FROM task_scans").fetchone()[0] == 0


def test_asked_to_add_body_and_answer():
    sent = {}

    def post(url, headers, body):
        sent.update(body=body)
        return {"answers": {"ask": {"type": "noul", "noul": 0.91}}}
    assert jev.asked_to_add("Add milk to my list.", "milk", post=post, key="k") == 0.91
    assert sent["body"]["state"] == {"transcript": "Add milk to my list.", "item": "milk"}
    assert sent["body"]["questions"]["ask"]["type"] == "noul"
    with pytest.raises(RuntimeError):
        jev.asked_to_add("t", "i", post=post, key=None)


# --- endpoint ----------------------------------------------------------------

@pytest.fixture()
def srv(env):
    s = feed.make_server(0, "tok")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def post(url, body, token="tok"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_endpoint_creates_idempotently_and_day_feed_lists_tasked(srv, monkeypatch):
    api = FakeLifeOS()
    monkeypatch.setattr(tasks, "lifeos_post", api)
    body = {"device_id": "DEV", "start_ms": 5, "item": "Buy hinge"}
    assert post(srv + "/pendant/feed/tasks", body) == (200, {"task_id": "t1", "item": "Buy hinge"})
    assert post(srv + "/pendant/feed/tasks", body)[1]["task_id"] == "t1"
    assert len(api.calls) == 1
    day = feed.day_payload(feed.dt.date(2026, 11, 9))
    c = day["conversations"][0]
    assert c["start_ms"] == 5 and c["device_id"] == "DEV" and c["tasked"] == ["Buy hinge"]


def test_endpoint_errors(srv, monkeypatch):
    assert post(srv + "/pendant/feed/tasks", {"item": "x"})[0] == 400
    assert post(srv + "/pendant/feed/tasks", {"device_id": "DEV", "start_ms": 9, "item": "x"})[0] == 404
    assert post(srv + "/pendant/feed/tasks", {"device_id": "DEV", "start_ms": 5, "item": "x"}, token="bad")[0] == 401
    monkeypatch.setattr(tasks, "lifeos_post", FakeLifeOS(tasks.LifeOSDown))
    code, out = post(srv + "/pendant/feed/tasks", {"device_id": "DEV", "start_ms": 5, "item": "Buy hinge"})
    assert code == 502 and "retried" in out["error"]
    day = feed.day_payload(feed.dt.date(2026, 11, 9))
    assert day["conversations"][0]["tasked"] == ["Buy hinge"]  # kept


def test_unlabeled_conversation_makes_no_spoken_task(env):
    con, notes = env
    (notes / f"{DAY}.md").write_text(f"# t\n\n## {HEADING}\n\n[16:15] Add milk to my to-do list.\n\n---\n")
    api = FakeLifeOS()
    tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 1.0, post=api, log=lambda m: None)
    assert not api.calls


def test_other_speaker_cannot_make_a_task(env):
    con, notes = env
    (notes / f"{DAY}.md").write_text(f"# t\n\n## {HEADING}\n\n[16:15] **Sam:** Add milk to my to-do list.\n\n---\n")
    api = FakeLifeOS()
    tasks.spoken_pass(con, notes, "Noah", ask=lambda s, i: 1.0, post=api, log=lambda m: None)
    assert not api.calls


def test_outage_leaves_unscanned_and_looks_back_a_week(env, monkeypatch):
    con, notes = env
    import datetime as dt
    old = (dt.datetime.now(tasks.TZ).date() - dt.timedelta(days=6)).isoformat()
    con.execute("UPDATE conversations SET day=?", (old,))
    (notes / f"{old}.md").write_text((notes / f"{DAY}.md").read_text())
    con.commit()
    # no key (conftest) -> nothing marked; failing Jev -> nothing marked
    tasks.spoken_pass(con, notes, "Noah", post=FakeLifeOS(), log=lambda m: None)
    monkeypatch.setattr(jev, "api_key", lambda: "k")
    monkeypatch.setattr(jev, "asked_to_add", lambda s, i: (_ for _ in ()).throw(OSError("down")))
    tasks.spoken_pass(con, notes, "Noah", post=FakeLifeOS(), log=lambda m: None)
    assert con.execute("SELECT COUNT(*) FROM task_scans").fetchone()[0] == 0
    api = FakeLifeOS()
    monkeypatch.setattr(jev, "asked_to_add", lambda s, i: 0.95)
    tasks.spoken_pass(con, notes, "Noah", post=api, log=lambda m: None)
    assert len(api.calls) == 1
