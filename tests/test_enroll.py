"""Record a voice sample (pendant_direct.enroll): the waiting -> added/rejected state
machine on fake chunks with fake VAD, audio and embeddings, the consent rules, and
keeping the reading window out of conversations."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from pendant_direct import enroll, feed, speakers
from test_process import (PAGE_MS, at, fake_embedder, fake_whisper, pages, proc,  # noqa: F401
                          write_segment)

DEV = "A1B2C3D4E5F60718"
E = np.eye(4, dtype=np.float32)
START, END = at(0) + 30_000, at(0) + 90_000       # a 60 s reading inside 2 min of audio
LATER = at(0) + 3_600_000                           # an hour on: long since settled


@pytest.fixture()
def world(proc, fake_embedder, fake_whisper, monkeypatch):  # noqa: F811
    """A processor with 2 min of audio from DEV (page k at at(0) + k s) and fakes for the
    audio decoder, VAD and embedder: `w.spans` is the VAD's answer, `w.voice(mid_s)` the
    voice at that clip time (default: one voice)."""
    class W:
        spans = [(0.5, 58.5)]
        voice = staticmethod(lambda mid: E[0])
    w = W()
    w.now = END / 1000 + 30          # the server's clock when a reading is reported
    monkeypatch.setattr(enroll, "_now", lambda: w.now)
    w.proc, w.embedder, w.whisper = proc, fake_embedder, fake_whisper
    w.data = proc.DATA
    # one clip second per page: the clip holds the window's pages end to end
    monkeypatch.setattr(speakers, "load_audio",
                        lambda clip: np.zeros(int(clip.stat().st_size and _clip_len(clip) * 16000), np.float32))
    monkeypatch.setattr(speakers, "speech_timestamps", lambda audio, sr=16000: list(w.spans))
    fake_embedder.fn = lambda clip, spans: np.stack([w.voice((a + b) / 2) for a, b in spans])
    (w.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"]}}))
    write_segment(proc.SPOOL, f"{DEV}_20260105T150000Z.pmsg", pages(DEV, at(0), 120))
    w.con = proc.db()
    proc.ingest(w.con)
    return w


def _clip_len(clip):
    """Seconds of Opus packets in the clip the code rendered (one Ogg page per ~50 packets;
    count them by the pages' granule positions)."""
    data = clip.read_bytes()
    best = 0
    i = 0
    while True:
        i = data.find(b"OggS", i)
        if i < 0 or i + 14 > len(data):
            break
        best = max(best, int.from_bytes(data[i + 6:i + 14], "little", signed=True))
        i += 4
    return max(best, 0) / 48000


def make(w, name="Riley", start=START, end=END):
    return enroll.create(w.con, name, DEV, start, end)


def judge(w, now_ms=LATER, cfg=None):
    return enroll.process(w.con, w.data, cfg, now_ms=now_ms, log=lambda *_: None)


def samples(w, name):
    return speakers.load_samples(speakers.voiceprint_dir(w.data)).get(name)


def test_added_gives_each_piece_its_own_sample_and_consent(world):
    eid = make(world)
    assert enroll.status(world.con, eid) == {"id": eid, "name": "Riley", "status": "waiting"}
    assert judge(world) == [eid]
    st = enroll.status(world.con, eid)
    assert st["status"] == "added" and st["samples"] == 6
    embs, ids = samples(world, "Riley")
    assert len(embs) == 6 and len(set(ids)) == 6 and all(i < 0 for i in ids)
    assert "Riley" in json.loads((world.data / "config.json").read_text())["speakers"]["allowed"]
    assert world.con.execute("SELECT source FROM enrollments").fetchone() == ("enrolled",)


def test_pieces_are_5_to_10_seconds_of_voiced_audio(world):
    make(world)
    judge(world)
    spans = world.embedder.calls[0]
    assert 3 <= len(spans) <= enroll.MAX_PIECES
    assert all(enroll.PIECE_MIN_S <= b - a <= enroll.PIECE_MAX_S + 1e-6 for a, b in spans)


def test_pieces_split_long_runs_join_short_ones_and_drop_scraps():
    assert enroll.pieces([(0, 24)]) == [(0, 8), (8, 16), (16, 24)]
    assert enroll.pieces([(0, 3), (3.8, 7)]) == [(0, 7)]                    # one run
    assert enroll.pieces([(0, 4), (5, 9)]) == [(0, 9)]                       # joined across a short pause
    assert enroll.pieces([(0, 4), (20, 26)]) == [(20, 26)]                   # a 4 s scrap on its own is dropped
    assert enroll.pieces([(0, 6), (7, 14)]) == [(0, 6), (7, 14)]             # joining would pass 10 s
    assert enroll.pieces([(0, 9.9), (30, 31)]) == [(0, 9.9)]


def trim(w):
    """The pendant has sent nothing after the reading window yet."""
    w.con.execute("DELETE FROM chunks WHERE ts_ms > ?", (END + 1000,))


def test_waits_until_the_audio_has_settled(world):
    trim(world)
    eid = make(world)
    assert judge(world, now_ms=END + 60_000) == []          # a minute after: more may still arrive
    assert enroll.status(world.con, eid)["status"] == "waiting"
    assert judge(world, now_ms=END + enroll.SETTLE_MS - 1) == []


def test_settles_five_minutes_after_the_last_audio_and_at_once_when_audio_follows(world):
    trim(world)
    eid = make(world)
    last = world.con.execute("SELECT MAX(ts_ms) FROM chunks").fetchone()[0]
    assert judge(world, now_ms=last + enroll.SETTLE_MS - 1) == []
    assert judge(world, now_ms=last + enroll.SETTLE_MS + 1) == [eid]
    # a second reading: pages after the window (the pendant drained past it) settle it at once
    write_segment(world.proc.SPOOL, f"{DEV}_20260105T150900Z.pmsg", pages(DEV, END + 5_000, 30, first_index=900) + pages(DEV, END + 60_000, 2, first_index=950))
    world.proc.ingest(world.con)
    again = enroll.create(world.con, "Alex", DEV, END + 5_000, END + 35_000)
    assert judge(world, now_ms=END + 90_000) == [again]


def test_too_little_speech(world):
    world.spans = [(1, 12), (20, 24)]                        # 15 s voiced
    trim(world)
    eid = make(world)
    assert judge(world, now_ms=END + 10 * 60_000) == []      # a late drain might still add speech
    assert enroll.status(world.con, eid)["status"] == "waiting"
    assert judge(world) == [eid]
    assert enroll.status(world.con, eid) == {"id": eid, "name": "Riley", "status": "rejected",
                                             "reason": "too little speech"}
    assert samples(world, "Riley") is None
    assert "Riley" not in json.loads((world.data / "config.json").read_text())["speakers"]["allowed"]


def test_a_window_shorter_than_25_seconds_is_rejected_at_once(world):
    eid = make(world, end=START + 20_000)
    assert enroll.status(world.con, eid)["reason"] == "too little speech"


def test_two_voices_are_rejected(world):
    world.voice = lambda mid: E[0] if mid < 30 else E[1]    # half and half
    eid = make(world)
    judge(world)
    assert enroll.status(world.con, eid) == {"id": eid, "name": "Riley", "status": "rejected",
                                             "reason": "more than one voice"}
    assert samples(world, "Riley") is None
    assert "Riley" not in json.loads((world.data / "config.json").read_text())["speakers"]["allowed"]


def test_a_brief_interjection_is_dropped_not_fatal(world):
    world.spans = [(0.5, 9.5), (10, 19), (19.5, 28.5), (29, 38), (38.5, 47.5), (48, 57), (57.5, 62), (63, 68)]
    world.voice = lambda mid: E[1] if mid > 63 else E[0]    # a 5 s third voice at the end (about 8%)
    eid = make(world)
    judge(world)
    st = enroll.status(world.con, eid)
    assert st["status"] == "added"
    embs, _ = samples(world, "Riley")
    assert len(embs) == st["samples"] and np.allclose(embs @ E[0], 1)


def test_nothing_from_the_pendant(world):
    world.now = at(500) / 1000 + 120
    eid = enroll.create(world.con, "Riley", DEV, at(500), at(500) + 60_000)
    assert judge(world, now_ms=at(500) + 3_600_000) == []
    assert enroll.status(world.con, eid)["status"] == "waiting"
    assert judge(world, now_ms=at(500) + 60_000 + 2 * 3_600_000) == [eid]
    assert enroll.status(world.con, eid)["reason"] == "no audio from that pendant"
    world.now = END / 1000 + 30
    other = enroll.create(world.con, "Riley", "0F1E2D3C4B5A6978", START, END)   # another pendant: no audio there
    judge(world, now_ms=END + 2 * 3_600_000 + 1)
    assert enroll.status(world.con, other)["reason"] == "no audio from that pendant"


def test_the_wearers_name_refuses_a_different_voice(world):
    (world.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "wearer": "Noah"}}))
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Noah", 100 + i, E[0])
    world.voice = lambda mid: E[2]
    eid = make(world, "Noah")
    judge(world)
    assert enroll.status(world.con, eid)["reason"] == "different voice"
    assert len(samples(world, "Noah")[0]) == 6                    # nothing added


def test_the_wearers_own_voice_adds_more_samples(world):
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Noah", 100 + i, E[0])
    eid = make(world, "noah")
    judge(world)
    assert enroll.status(world.con, eid)["status"] == "added"
    assert len(samples(world, "Noah")[0]) == 6 + 6


def test_a_small_voiceprint_does_not_veto_a_new_reading(world):
    speakers.add(speakers.voiceprint_dir(world.data), "Riley", 7, E[3])
    world.voice = lambda mid: E[0]
    eid = make(world)
    judge(world)
    assert enroll.status(world.con, eid)["status"] == "added"


def test_reserved_names_and_bad_requests(world):
    assert enroll.status(world.con, make(world, "Someone"))["reason"] == "reserved name"
    assert enroll.status(world.con, make(world, "Speaker A"))["reason"] == "reserved name"
    for bad in [dict(name="  "), dict(name="x" * 50), dict(device="nothex"), dict(device=""),
                dict(start_ms=END, end_ms=START), dict(start_ms=5, end_ms=10), dict(start_ms="1"),
                dict(end_ms=START + 3_600_000)]:
        args = dict(name="Riley", device=DEV, start_ms=START, end_ms=END) | bad
        with pytest.raises(enroll.BadRequest):
            enroll.create(world.con, **args)


def test_the_same_reading_is_one_enrollment_and_a_retry_adds_nothing_twice(world):
    a, b = make(world), make(world)
    assert a == b
    judge(world)
    assert len(samples(world, "Riley")[0]) == 6
    world.con.execute("UPDATE enrollments SET status = 'waiting' WHERE id = ?", (a,))   # a crash after the add
    judge(world)
    assert len(samples(world, "Riley")[0]) == 6
    assert enroll.status(world.con, a)["samples"] == 6


def test_the_window_never_becomes_a_conversation(world):
    make(world)
    judge(world)
    world.whisper.fn = None
    world.proc.transcribe_ready(world.con)
    rows = world.con.execute("SELECT start_ms, end_ms, audio_s FROM conversations ORDER BY start_ms").fetchall()
    # pages 0-28 before the window and 92-119 after it: 64 s apart, so two conversations
    assert [round(r[2]) for r in rows] == [29, 28]
    assert rows[0][1] < START - 1000 and rows[1][0] > END + 1000
    assert world.con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone() == (0,)


def test_a_short_window_leaves_one_conversation_around_it(world):
    make(world, start=START, end=START + 30_000)
    judge(world)
    world.proc.transcribe_ready(world.con)
    (n, audio) = world.con.execute("SELECT COUNT(*), SUM(audio_s) FROM conversations").fetchone()
    assert n == 1 and round(audio) == 120 - 33          # the 30 s, 1 s padding each side and a page of rounding is cut out


def test_waiting_windows_are_excluded_too_and_late_audio_stays_out(world):
    make(world)                                           # not judged yet: still waiting
    world.proc.transcribe_ready(world.con)
    assert [round(r[0]) for r in world.con.execute("SELECT audio_s FROM conversations ORDER BY start_ms")] == [29, 28]
    # a drain adds pages inside the window after it was judged: never a conversation, never counted
    judge(world)
    write_segment(world.proc.SPOOL, f"{DEV}_20260105T150500Z.pmsg",
                  pages(DEV, START + 3_000, 3, first_index=500))
    world.proc.ingest(world.con)
    judge(world, now_ms=LATER + 1)
    world.proc.transcribe_ready(world.con)
    assert world.con.execute("SELECT COUNT(*) FROM conversations").fetchone() == (2,)
    assert world.con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone() == (0,)
    assert len(samples(world, "Riley")[0]) == 6


def test_a_failing_embedder_leaves_it_waiting(world):
    def boom(clip, spans):
        raise RuntimeError("model missing")
    world.embedder.fn = boom
    eid = make(world)
    assert judge(world) == []
    assert enroll.status(world.con, eid)["status"] == "waiting"
    world.embedder.fn = lambda clip, spans: np.tile(E[0], (len(spans), 1))
    assert judge(world) == [eid]


# ---------------------------------------------------------------------------
# endpoints

@pytest.fixture()
def server(world, monkeypatch, tmp_path):
    monkeypatch.setenv("PENDANT_DATA", str(world.data))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(tmp_path / "notes"))
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed"
    srv.shutdown()


def call(url, body=None, token="tok"):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_endpoints(world, server):
    code, out = call(server + "/enroll", {"name": "Riley", "device": DEV, "start_ms": START, "end_ms": END})
    assert code == 200 and out["status"] == "waiting" and isinstance(out["id"], int)
    assert call(server + f"/enroll/{out['id']}") == (200, {"id": out["id"], "name": "Riley", "status": "waiting"})
    judge(world)
    assert call(server + f"/enroll/{out['id']}") == (
        200, {"id": out["id"], "name": "Riley", "status": "added", "samples": 6})
    assert call(server + "/enroll", {"name": "Riley", "device": DEV, "start_ms": START, "end_ms": END})[1]["id"] == out["id"]
    assert call(server + "/enroll/999")[0] == 404


def test_endpoint_validation_and_auth(world, server):
    assert call(server + "/enroll", {"name": "", "device": DEV, "start_ms": START, "end_ms": END})[0] == 400
    assert call(server + "/enroll", {"name": "Riley", "device": DEV})[0] == 400
    assert call(server + "/enroll", {"name": "Riley", "device": DEV, "start_ms": START, "end_ms": END},
                token="bad")[0] == 401
    assert call(server + "/enroll/1", token="bad")[0] == 401
    code, out = call(server + "/enroll", {"name": "Someone", "device": DEV, "start_ms": START, "end_ms": END})
    assert code == 200 and out["status"] == "rejected" and out["reason"] == "reserved name"


# ---------------------------------------------------------------------------
# a window holds audio back only while waiting, and only an added one consumes it

def conversation_audio(w):
    return sum(r[0] for r in w.con.execute("SELECT audio_s FROM conversations"))


def test_a_rejected_window_releases_its_audio_to_the_vault(world):
    world.voice = lambda mid: E[0] if mid < 30 else E[1]
    eid = make(world)
    world.proc.transcribe_ready(world.con)                    # waiting: held back
    assert [round(r[0]) for r in world.con.execute("SELECT audio_s FROM conversations ORDER BY start_ms")] == [29, 28]
    judge(world)
    assert enroll.status(world.con, eid)["status"] == "rejected"
    assert world.con.execute("SELECT COUNT(*) FROM chunks WHERE done = 1 AND ts_ms BETWEEN ? AND ?",
                             (START, END)).fetchone() == (0,), "a rejection consumes nothing"
    world.proc.transcribe_ready(world.con)
    assert round(conversation_audio(world)) == 29 + 28 + 63    # the window's 63 s is now a conversation


def test_every_rejection_reason_releases_the_audio(world):
    world.spans = [(1, 12)]
    eid = make(world)
    judge(world)
    assert enroll.status(world.con, eid)["reason"] == "too little speech"
    world.proc.transcribe_ready(world.con)
    assert round(conversation_audio(world)) == 120


def test_a_check_that_keeps_failing_expires_and_releases_the_audio(world):
    def boom(clip, spans):
        raise RuntimeError("model missing")
    world.embedder.fn = boom
    eid = make(world)
    for _ in range(enroll.MAX_ATTEMPTS - 1):
        assert judge(world) == []
    assert enroll.status(world.con, eid)["status"] == "waiting"
    assert judge(world) == [eid]
    assert enroll.status(world.con, eid)["reason"] == "could not be checked"
    world.proc.transcribe_ready(world.con)
    assert round(conversation_audio(world)) == 120


def test_a_window_still_waiting_two_hours_on_expires(world, monkeypatch):
    monkeypatch.setattr(enroll, "evaluate", lambda *a, **k: "waiting")
    eid = make(world)
    assert judge(world, now_ms=END + enroll.NO_AUDIO_MS - 1) == []
    assert judge(world, now_ms=END + enroll.NO_AUDIO_MS) == [eid]
    assert enroll.status(world.con, eid)["status"] == "rejected"
    world.proc.transcribe_ready(world.con)
    assert round(conversation_audio(world)) == 120


def test_a_waiting_window_holds_audio_back_without_consuming_it(world):
    trim(world)
    make(world)
    assert judge(world, now_ms=END + 60_000) == []
    assert world.con.execute("SELECT COUNT(*) FROM chunks WHERE done = 1").fetchone() == (0,)
    world.proc.transcribe_ready(world.con)
    assert round(conversation_audio(world)) == 29            # only the audio before the window


def test_an_added_window_marks_its_audio_done(world):
    make(world)
    judge(world)
    done = world.con.execute("SELECT COUNT(*) FROM chunks WHERE done = 1").fetchone()[0]
    assert done == world.con.execute("SELECT COUNT(*) FROM chunks WHERE ts_ms BETWEEN ? AND ?",
                                     (START - 1000, END + 1000)).fetchone()[0]


# ---------------------------------------------------------------------------
# the clock

def test_a_window_in_the_future_or_long_past_is_refused(world):
    world.now = END / 1000
    enroll.create(world.con, "Riley", DEV, START, END)                          # ends exactly now: fine
    with pytest.raises(enroll.BadRequest):
        enroll.create(world.con, "Alex", DEV, END + 1000, END + 62_000)       # ends over a minute ahead
    world.now = START / 1000 + 2 * 3600 + 1
    with pytest.raises(enroll.BadRequest):
        enroll.create(world.con, "Alex", DEV, START - 1, END - 1)             # started over 2 h ago
    world.now = START / 1000 + 2 * 3600
    assert enroll.create(world.con, "Alex", "0F1E2D3C4B5A6978", START + 1, END - 1) > 0   # exactly 2 h: fine


def test_a_future_window_does_not_suppress_conversations_yet_to_happen(world):
    world.now = END / 1000
    with pytest.raises(enroll.BadRequest):
        enroll.create(world.con, "Riley", DEV, END + 3_600_000, END + 3_660_000)
    assert world.con.execute("SELECT COUNT(*) FROM enrollments").fetchone() == (0,)


# ---------------------------------------------------------------------------
# overlaps

def test_an_overlapping_request_is_a_conflict_whatever_the_name(world):
    a = make(world)
    for name in ("Riley", "Alex"):
        with pytest.raises(enroll.Conflict):
            enroll.create(world.con, name, DEV, START + 10_000, END + 10_000)
    assert make(world) == a                                    # the same reading again is a retry
    assert enroll.create(world.con, "Alex", "0F1E2D3C4B5A6978", START, END) != a    # another pendant
    assert enroll.create(world.con, "Alex", DEV, END, END + 30_000) != a            # touching, not overlapping
    judge(world)
    with pytest.raises(enroll.Conflict):                       # an added one still blocks
        enroll.create(world.con, "Alex", DEV, START, END - 5_000)


def test_a_rejected_enrollment_does_not_block_a_new_one(world):
    world.spans = [(1, 12)]
    make(world)
    judge(world)
    world.spans = [(0.5, 58.5)]
    assert enroll.status(world.con, make(world, "Alex"))["status"] == "waiting"


def test_overlap_is_a_409_over_http(world, server):
    body = {"name": "Riley", "device": DEV, "start_ms": START, "end_ms": END}
    assert call(server + "/enroll", body)[0] == 200
    assert call(server + "/enroll", body | {"name": "Alex"})[0] == 409
    assert call(server + "/enroll", body | {"end_ms": END + 120_000})[0] == 400          # in the future


# ---------------------------------------------------------------------------
# idempotent sample writing

def test_a_retry_after_a_partial_write_leaves_exactly_the_latest_cuts(world):
    eid = make(world)
    judge(world)
    assert len(samples(world, "Riley")[0]) == 6
    # a crash after the voiceprint write: the row is still waiting, and late audio changes the cuts
    world.con.execute("UPDATE enrollments SET status = 'waiting', samples = 0 WHERE id = ?", (eid,))
    world.spans = [(0.5, 35)]                                  # four pieces now
    judge(world)
    st = enroll.status(world.con, eid)
    assert st["status"] == "added" and st["samples"] == 4
    embs, ids = samples(world, "Riley")
    assert len(embs) == 4 and len(set(ids)) == 4


def test_a_retry_keeps_other_peoples_and_earlier_samples(world):
    vdir = speakers.voiceprint_dir(world.data)
    speakers.add(vdir, "Riley", 77, E[0])
    eid = make(world)
    judge(world)
    world.con.execute("UPDATE enrollments SET status = 'waiting' WHERE id = ?", (eid,))
    judge(world)
    embs, ids = samples(world, "Riley")
    assert len(embs) == 7 and 77 in ids


# ---------------------------------------------------------------------------
# round 2: atomic create, cleanup on reject/expire, schema guard

def test_concurrent_requests_cannot_both_pass_the_overlap_check(world):
    results = []

    def request(name):
        con = world.proc.db()
        try:
            results.append(enroll.create(con, name, DEV, START, END))
        except enroll.Conflict:
            results.append("conflict")
        finally:
            con.close()
    threads = [threading.Thread(target=request, args=(n,)) for n in
               ["Anna", "Bea", "Cleo", "Dina", "Edna", "Fay", "Gwen", "Hana"]]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r != "conflict" for r in results) == 1 and results.count("conflict") == 7
    assert world.con.execute("SELECT COUNT(*) FROM enrollments").fetchone() == (1,)


def test_the_write_lock_is_taken_before_the_overlap_check(world):
    import sqlite3
    other = sqlite3.connect(world.proc.DB, timeout=0.1)
    mine = sqlite3.connect(world.proc.DB, timeout=0.1)
    other.execute("BEGIN IMMEDIATE")                  # someone is mid-request
    try:
        with pytest.raises(sqlite3.OperationalError):
            enroll.create(mine, "Riley", DEV, START, END)
    finally:
        other.rollback()
        other.close()
    assert enroll.create(world.con, "Riley", DEV, START, END) > 0


def _crash_after_write(w, eid):
    """The voiceprint write landed but the row never left `waiting`."""
    w.con.execute("UPDATE enrollments SET status = 'waiting', reason = NULL, samples = 0 WHERE id = ?", (eid,))
    w.con.commit()


def test_a_retry_that_rejects_removes_the_samples_a_crash_left(world):
    speakers.add(speakers.voiceprint_dir(world.data), "Riley", 77, E[0])     # an earlier sample stays
    eid = make(world)
    judge(world)
    assert len(samples(world, "Riley")[0]) == 7
    _crash_after_write(world, eid)
    world.spans = [(1, 12)]
    judge(world)
    assert enroll.status(world.con, eid)["reason"] == "too little speech"
    embs, ids = samples(world, "Riley")
    assert ids == [77]


def test_a_retry_that_expires_removes_the_samples_a_crash_left(world):
    eid = make(world)
    judge(world)
    assert len(samples(world, "Riley")[0]) == 6
    _crash_after_write(world, eid)
    world.embedder.fn = lambda clip, spans: (_ for _ in ()).throw(RuntimeError("model missing"))
    judge(world, now_ms=END + enroll.NO_AUDIO_MS)
    assert enroll.status(world.con, eid)["reason"] == "could not be checked"
    assert samples(world, "Riley") is None


def test_a_wearer_mismatch_on_retry_removes_them_too(world):
    (world.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "wearer": "Noah"}}))
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Noah", 100 + i, E[0])
    eid = make(world, "Noah")
    judge(world)
    assert len(samples(world, "Noah")[0]) == 12
    _crash_after_write(world, eid)
    world.voice = lambda mid: E[2]
    judge(world)
    assert enroll.status(world.con, eid)["reason"] == "different voice"
    assert len(samples(world, "Noah")[0]) == 6


def test_the_attempts_column_is_added_to_an_older_table(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE enrollments (id INTEGER PRIMARY KEY, name TEXT, device TEXT, start_ms INT, "
                "end_ms INT, lo_ms INT, hi_ms INT, status TEXT, reason TEXT, samples INT, source TEXT, "
                "created_at REAL, finished_at REAL, UNIQUE (device, start_ms, end_ms, name))")
    con.execute("INSERT INTO enrollments (name, status) VALUES ('Old', 'waiting')")
    enroll.ensure_schema(con)
    enroll.ensure_schema(con)                          # twice: a no-op
    assert con.execute("SELECT attempts FROM enrollments").fetchone() == (0,)


# ---------------------------------------------------------------------------
# the phone-mic copy of a reading

@pytest.fixture()
def phone(world, monkeypatch):
    """A 60 s phone recording: `world.spans` and `world.voice` drive the fakes as for the pendant."""
    from pendant_direct import voice_library
    monkeypatch.setattr(speakers, "load_audio", lambda clip: np.zeros(int(world.phone_s * 16000), np.float32))
    world.phone_s = 60
    world.library = []
    monkeypatch.setattr(voice_library, "add", lambda *a, **k: world.library.append(k) or "added")
    return world


def send_phone(w, eid):
    return enroll.add_phone(w.con, w.data, None, eid, b"RIFFfake")


def test_phone_copy_adds_samples_tagged_phone_before_the_pendant_syncs(phone):
    eid = make(phone)
    out = send_phone(phone, eid)
    assert out == {"id": eid, "name": "Riley", "status": "waiting", "phone": {"status": "added", "samples": 6}}
    embs, ids = samples(phone, "Riley")
    assert len(embs) == 6 and ids == [speakers.sample_key(f"enroll:{eid}:phone:{n}") for n in range(6)]
    assert "Riley" in json.loads((phone.data / "config.json").read_text())["speakers"]["allowed"]
    assert phone.library == []                    # the library holds Opus as recorded: no clips for a phone WAV


def test_the_pendant_copy_is_still_added_when_it_syncs_and_tagged_pendant(phone):
    eid = make(phone)
    send_phone(phone, eid)
    assert judge(phone) == [eid]
    st = enroll.status(phone.con, eid)
    assert st["status"] == "added" and st["samples"] == 6 and st["phone"] == {"status": "added", "samples": 6}
    assert len(samples(phone, "Riley")[0]) == 12
    assert {k["origin"] for k in phone.library} == {"pendant"}
    assert {k["src"] for k in phone.library} == {f"enroll:{eid}:{n}" for n in range(6)}


def test_a_rejected_pendant_copy_leaves_the_phone_samples(phone):
    eid = make(phone)
    send_phone(phone, eid)
    phone.spans = [(1, 4)]                       # the pendant heard almost nothing of it
    assert judge(phone) == [eid]
    assert enroll.status(phone.con, eid)["status"] == "rejected"
    assert len(samples(phone, "Riley")[0]) == 6


def test_resending_the_phone_copy_adds_nothing_twice(phone):
    eid = make(phone)
    send_phone(phone, eid)
    send_phone(phone, eid)
    assert len(samples(phone, "Riley")[0]) == 6


def test_phone_copy_too_little_speech(phone):
    phone.spans = [(1, 12), (20, 24)]
    eid = make(phone)
    out = send_phone(phone, eid)
    assert out["phone"] == {"status": "rejected", "reason": "too little speech"}
    assert samples(phone, "Riley") is None
    assert "Riley" not in json.loads((phone.data / "config.json").read_text())["speakers"]["allowed"]


def test_phone_copy_two_voices_rejected_and_a_retry_clears_earlier_samples(phone):
    eid = make(phone)
    send_phone(phone, eid)
    assert len(samples(phone, "Riley")[0]) == 6
    phone.voice = lambda mid: E[0] if mid < 30 else E[1]
    assert send_phone(phone, eid)["phone"] == {"status": "rejected", "reason": "more than one voice"}
    assert samples(phone, "Riley") is None or len(samples(phone, "Riley")[0]) == 0


def test_phone_copy_keeps_the_wearer_voiceprint_rule(phone):
    (phone.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "wearer": "Noah"}}))
    vdir = speakers.voiceprint_dir(phone.data)
    for i in range(6):
        speakers.add(vdir, "Noah", 100 + i, E[0])
    phone.voice = lambda mid: E[2]
    eid = make(phone, "Noah")
    assert send_phone(phone, eid)["phone"] == {"status": "rejected", "reason": "different voice"}
    assert len(samples(phone, "Noah")[0]) == 6


def test_phone_copy_of_a_reserved_name_is_refused(phone):
    eid = make(phone, "Someone")
    assert send_phone(phone, eid)["phone"] == {"status": "rejected", "reason": "reserved name"}
    assert samples(phone, "Someone") is None


def test_phone_copy_over_60_seconds_unknown_id_and_undecodable_audio(phone, monkeypatch):
    eid = make(phone)
    phone.phone_s = 75
    with pytest.raises(enroll.BadRequest):
        send_phone(phone, eid)
    assert enroll.add_phone(phone.con, phone.data, None, 9999, b"x") is None

    def broken(clip):
        raise RuntimeError("bad data")
    monkeypatch.setattr(speakers, "load_audio", broken)
    with pytest.raises(enroll.BadRequest):
        send_phone(phone, eid)
    assert samples(phone, "Riley") is None


def test_the_phone_columns_are_added_to_an_older_table(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE enrollments (id INTEGER PRIMARY KEY, name TEXT, device TEXT, start_ms INT, "
                "end_ms INT, lo_ms INT, hi_ms INT, status TEXT, reason TEXT, samples INT, source TEXT, "
                "attempts INT DEFAULT 0, created_at REAL, finished_at REAL)")
    enroll.ensure_schema(con)
    assert {"phone_status", "phone_reason", "phone_samples"} <= {r[1] for r in con.execute("PRAGMA table_info(enrollments)")}


def test_phone_endpoint(phone, server):
    eid = make(phone)
    url = server + f"/enroll/{eid}/phone"

    def post(u, data, token="tok"):
        req = urllib.request.Request(u, data=data, method="POST", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    code, out = post(url, b"RIFFfake")
    assert code == 200 and out["phone"] == {"status": "added", "samples": 6}
    assert call(server + f"/enroll/{eid}")[1]["phone"]["status"] == "added"
    assert post(url, b"RIFFfake", token="bad")[0] == 401
    assert post(server + "/enroll/999/phone", b"x")[0] == 404
    assert post(url, b"")[0] == 400
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(server.split(":")[2].split("/")[0]))
    conn.putrequest("POST", f"/pendant/feed/enroll/{eid}/phone")
    conn.putheader("Authorization", "Bearer tok")
    conn.putheader("Content-Length", str(feed.MAX_PHONE_BODY + 1))
    conn.endheaders()
    assert conn.getresponse().status == 413
    phone.phone_s = 90
    assert post(url, b"x")[0] == 400


# ---------------------------------------------------------------------------
# a reading is the reference: never refused for sounding different, and what disagrees is set aside

def _voice(c):
    v = np.zeros(4, np.float32)
    v[2], v[c] = 0.6, v[c] + 0.8
    return v / np.linalg.norm(v)


def test_a_different_voice_is_added_and_the_saved_samples_that_disagree_are_set_aside(world):
    from pendant_direct import sample_hygiene
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Riley", 100 + i, E[0])                 # a voiceprint the reading disagrees with
    speakers.add(vdir, "Riley", 200, _voice(2))                    # one saved sample that agrees (0.6+)
    speakers.add(vdir, "Riley", speakers.sample_key("enroll:77:0"), E[1])   # an earlier reading: kept
    world.con.executemany("INSERT INTO speaker_clusters (id, device, start_ms, tag, enrolled_in) VALUES (?, 'X', 1, 'A', 'Riley')",
                          [(100 + i,) for i in range(6)])
    world.con.commit()
    world.voice = lambda mid: E[2]
    eid = make(world)
    judge(world)
    st = enroll.status(world.con, eid)
    assert st["status"] == "added" and st["samples"] == 6 and st["set_aside"] == 6
    ids = samples(world, "Riley")[1]
    assert not set(range(100, 106)) & set(ids) and 200 in ids and speakers.sample_key("enroll:77:0") in ids
    assert len(ids) == 1 + 1 + 6
    assert world.con.execute("SELECT COUNT(*) FROM speaker_clusters WHERE enrolled_in = 'Riley'").fetchone() == (0,)
    backup, = (world.data / "backups").glob(f"set-aside-{eid}-*.json")
    assert sample_hygiene.restore(world.data, backup) == 6         # recoverable, in hygiene's format
    assert set(range(100, 106)) <= set(samples(world, "Riley")[1])
    assert world.con.execute("SELECT COUNT(*) FROM speaker_clusters WHERE enrolled_in = 'Riley'").fetchone() == (6,)


def test_a_matching_reading_sets_nothing_aside(world):
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Riley", 100 + i, E[0])
    eid = make(world)
    judge(world)
    st = enroll.status(world.con, eid)
    assert st["status"] == "added" and "set_aside" not in st
    assert len(samples(world, "Riley")[0]) == 12
    assert not (world.data / "backups").exists()


def test_the_wearers_samples_are_never_set_aside(world):
    (world.data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah"], "wearer": "Noah"}}))
    vdir = speakers.voiceprint_dir(world.data)
    for i in range(6):
        speakers.add(vdir, "Noah", 100 + i, E[0])
    speakers.add(vdir, "Noah", 300, E[3])                           # an odd sample the reading disagrees with
    eid = make(world, "Noah")
    judge(world)
    assert enroll.status(world.con, eid)["status"] == "added"
    assert 300 in samples(world, "Noah")[1]


def test_the_phone_copy_sets_aside_too(phone):
    vdir = speakers.voiceprint_dir(phone.data)
    for i in range(6):
        speakers.add(vdir, "Riley", 100 + i, E[0])
    phone.voice = lambda mid: E[2]
    eid = make(phone)
    out = send_phone(phone, eid)
    assert out["phone"] == {"status": "added", "samples": 6} and out["set_aside"] == 6
    assert len(samples(phone, "Riley")[0]) == 6
