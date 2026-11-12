"""The permanent voice library (pendant_direct.voice_library) and its hooks."""
from __future__ import annotations

import pytest

from pendant_direct import feed, retention, trash, voice_library as vl
from pendant_direct.oggopus import OggOpusWriter
from test_process import fake_embedder, fake_whisper, proc, run  # noqa: F401
from test_trash import A, B, DEV, delete, restore, two  # noqa: F401

CFG = {"speakers": {"allowed": ["Noah", "Alex"], "people": {"Dana": "ent-1"}}}


@pytest.fixture
def clip(tmp_path):
    """A 30 s Ogg Opus clip of fake 20 ms packets, laid out in data/clips."""
    d = tmp_path / "data" / "clips"
    d.mkdir(parents=True)
    f = d / "c.opus"
    w = OggOpusWriter(f)
    for i in range(1500):
        w.add_packet(bytes([0xB8]) + i.to_bytes(2, "little") * 3)
    w.close()
    return f


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


def put(data, clip, name, kind, n, *, day="2026-01-05", conv=None, start=1.0, end=9.0, **kw):
    return vl.add(data, CFG, name, kind=kind, src=f"cluster:{n}", clip=clip, start_s=start, end_s=end,
                  device="D", start_ms=conv if conv is not None else n, day=day, **kw)


def srcs(data, name="Noah"):
    return sorted(e["src"] for e in vl.load(data, name))


def test_cut_makes_a_clip_of_the_span(data, clip):
    assert put(data, clip, "Noah", "hand", 1, start=2.0, end=8.0, voiced=0.9) == "added"
    e = vl.load(data, "Noah")[0]
    out = data / "voice_library" / "Noah" / f"{e['id']}.opus"
    assert abs(e["dur_s"] - 6.0) < 0.05 and len(vl.read_packets(out)) == 300
    assert vl.read_packets(out)[0] == vl.read_packets(clip)[100]            # packet-exact cut
    # a long span is trimmed to its middle 15 s
    put(data, clip, "Noah", "hand", 2, start=0.0, end=30.0)
    e2 = [x for x in vl.load(data, "Noah") if x["src"] == "cluster:2"][0]
    assert abs(e2["dur_s"] - 15.0) < 0.05 and e2["start_s"] == 7.5


def test_hand_beats_enrollment_beats_auto(data, clip):
    for i in range(vl.CAP):
        assert put(data, clip, "Noah", "auto", i, score=0.1 + i / 100) == "added"
    assert put(data, clip, "Noah", "auto", 100, score=9.0) == "skipped: library full"   # same rank: no swap
    assert put(data, clip, "Noah", "enroll", 101) == "replaced"
    kinds = [e["kind"] for e in vl.load(data, "Noah")]
    assert kinds.count("enroll") == 1 and kinds.count("auto") == vl.CAP - 1
    assert "cluster:0" not in srcs(data)          # the lowest-ranked (and weakest) auto went
    assert put(data, clip, "Noah", "hand", 102) == "replaced"
    assert {"enroll", "hand"} <= {e["kind"] for e in vl.load(data, "Noah")}
    assert put(data, clip, "Noah", "hand", 103) == "replaced"       # still replaces an auto, not the enrollment
    assert [e["kind"] for e in vl.load(data, "Noah")].count("enroll") == 1
    assert len(list((data / "voice_library" / "Noah").glob("*.opus"))) == vl.CAP
    # a library of hand tags only gives way to nothing
    for i in range(200, 200 + vl.CAP):
        put(data, clip, "Noah", "hand", i)
    assert put(data, clip, "Noah", "hand", 999) == "skipped: library full"
    assert put(data, clip, "Noah", "enroll", 998) == "skipped: library full"
    assert all(e["kind"] == "hand" for e in vl.load(data, "Noah"))


def test_batch_selection_prefers_diversity_then_quality(data, clip):
    c = [dict(rank=1, day="d1", device="D", start_ms=1, voiced=0.9, speech_s=9, src="a"),
         dict(rank=1, day="d1", device="D", start_ms=1, voiced=0.8, speech_s=9, src="b"),
         dict(rank=1, day="d1", device="D", start_ms=2, voiced=0.7, speech_s=9, src="c"),
         dict(rank=1, day="d2", device="D", start_ms=3, voiced=0.5, speech_s=9, src="d"),
         dict(rank=3, day="d9", device="D", start_ms=9, voiced=1.0, speech_s=99, score=5, src="auto")]
    assert [x["src"] for x in vl.select(c, 3)] == ["a", "d", "c"]            # new day, then new conversation
    assert [x["src"] for x in vl.select(c, 5)][-1] == "auto"                  # auto only after hand
    same = [dict(rank=1, day="d", device="D", start_ms=1, voiced=0.5, speech_s=5, src="short"),
            dict(rank=1, day="d", device="D", start_ms=1, voiced=0.5, speech_s=12, src="long")]
    assert vl.select(same, 1)[0]["src"] == "long"
    autos = [dict(rank=3, day="d1", device="D", start_ms=1, voiced=0.9, speech_s=9, score=0.1, src="lo"),
             dict(rank=3, day="d1", device="D", start_ms=1, voiced=0.5, speech_s=9, score=0.9, src="hi")]
    assert vl.select(autos, 1)[0]["src"] == "hi"                               # auto: the highest margin


def test_consent_and_quality_gates(data, clip):
    assert put(data, clip, "Mallory", "hand", 1).startswith("skipped: not an allowed")
    assert put(data, clip, "Someone", "hand", 2).startswith("skipped: not an allowed")
    assert put(data, clip, "Noah", "hand", 3, mixed=True) == "skipped: mixed voice"
    assert put(data, clip, "Noah", "hand", 4, voiced=0.1) == "skipped: not speech"
    assert put(data, clip, "Noah", "hand", 5, start=1, end=4) == "skipped: under 4 s"
    assert vl.add(data, CFG, "Noah", kind="hand", src="x", clip=data / "gone.opus", start_s=0, end_s=9) \
        == "skipped: no audio"
    assert put(data, clip, "Dana", "hand", 6).startswith("skipped: not an allowed")     # a LifeOS link isn't consent
    assert vl.people(data) == []
    assert vl.eligible({"speakers": {"allowed": ["Alex"], "people": {}}}, "alex") == "Alex"
    assert vl.eligible(CFG, "../etc") is None


def test_drop_removes_the_clips_a_source_contributed(data, clip):
    put(data, clip, "Noah", "hand", 1)
    put(data, clip, "Alex", "hand", 2)
    assert vl.drop(data, "cluster:1") == 1
    assert vl.people(data) == ["Alex"] and not (data / "voice_library" / "Noah").exists()
    put(data, clip, "Alex", "hand", 3)
    assert vl.drop(data, "cluster:", prefix=True) == 2 and vl.people(data) == []


def test_naming_a_voice_again_replaces_its_clip(data, clip):
    put(data, clip, "Noah", "auto", 1, score=0.3)
    put(data, clip, "Alex", "hand", 1)           # same voice, now Alex's
    assert vl.people(data) == ["Alex"] and vl.load(data, "Alex")[0]["kind"] == "hand"


# ---------------------------------------------------------------------------
# hooks

def grow(two):
    """Rewrite each conversation's clip as 30 s of fake packets (the fixtures' clips are 3 s)."""
    for f in (two.DATA / "clips").glob("*.opus"):
        w = OggOpusWriter(f)
        for i in range(1500):
            w.add_packet(bytes([0xB8]) + i.to_bytes(2, "little") * 3)
        w.close()
    con = two.db()
    con.execute("UPDATE speaker_clusters SET start_s=1, end_s=9, voiced=0.9, speech_s=8")
    con.commit()
    con.close()


def test_answering_a_voice_adds_a_hand_clip_and_moving_it_away_removes_it(two):
    grow(two)
    con = two.db()
    cid = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone()[0]
    con.close()
    feed.label_cluster(cid, "Noah", "user", reassign=True)
    e = vl.load(two.DATA, "Noah")
    assert [x["src"] for x in e] == [f"cluster:{cid}"] and e[0]["kind"] == "hand" and e[0]["day"] == "2026-01-05"
    feed.label_cluster(cid, "Alex", "user", reassign=True)                  # moved to someone else
    assert vl.people(two.DATA) == ["Alex"]
    feed.stranger_cluster(cid, reassign=True)                                 # "a random person"
    assert vl.people(two.DATA) == []
    feed.label_cluster(cid, "Noah", "user", reassign=True)
    assert vl.people(two.DATA) == ["Noah"]


def test_a_tapped_line_adds_a_clip_and_reassigning_it_moves_or_removes_it(two):
    grow(two)
    con = two.db()
    con.execute("UPDATE conversations SET paragraph_offsets='[[0.5, 6.0]]' WHERE start_ms=?", (B,))
    con.commit()
    con.close()
    feed.reassign_speaker(DEV, B, {"name": "Noah", "source": "user", "scope": "line", "paragraph": 0})
    assert [e["src"] for e in vl.load(two.DATA, "Noah")] == [f"para:{DEV}:{B}:0"]
    feed.reassign_speaker(DEV, B, {"name": "Someone", "scope": "line", "paragraph": 0})
    assert vl.people(two.DATA) == []


def test_retention_never_touches_the_library_and_reports_it(two):
    grow(two)
    con = two.db()
    cid = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (B,)).fetchone()[0]
    con.close()
    feed.label_cluster(cid, "Noah", "user", reassign=True)
    before = {p.name: p.read_bytes() for p in (two.DATA / "voice_library" / "Noah").iterdir()}
    assert len(before) == 2
    con = two.db()
    now = retention.parse_now("2027-06-01")
    rep = retention.run(con, two.DATA, now_ms=now, keep_starred=False)
    con.close()
    assert rep["total"]["clips"] == 2 and not list((two.DATA / "clips").glob("*.opus"))
    assert {p.name: p.read_bytes() for p in (two.DATA / "voice_library" / "Noah").iterdir()} == before
    assert rep["voice_library"]["Noah"]["clips"] == 1 and rep["voice_library"]["Noah"]["bytes"] > 0
    assert "voice library (never purged): Noah 1 clips" in retention.format_report(rep, dry=False)


def test_trash_round_trip_moves_the_recordings_library_clips(two):
    grow(two)
    con = two.db()
    ca = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone()[0]
    cb = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (B,)).fetchone()[0]
    con.close()
    feed.label_cluster(ca, "Noah", "user", reassign=True)
    feed.label_cluster(cb, "Noah", "user", reassign=True)
    lib = two.DATA / "voice_library" / "Noah"
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    index = vl.load(two.DATA, "Noah")
    delete(two, A)
    assert [e["src"] for e in vl.load(two.DATA, "Noah")] == [f"cluster:{cb}"]      # A's clip left the library
    assert len(list(lib.glob("*.opus"))) == 1
    assert list((two.DATA / "trash" / f"{DEV}-{A}" / "library" / "Noah").glob("*.opus"))
    restore(two, A)
    assert {p.name: p.read_bytes() for p in lib.glob("*.opus")} == {k: v for k, v in before.items() if k.endswith(".opus")}
    assert sorted(vl.load(two.DATA, "Noah"), key=lambda e: e["id"]) == sorted(index, key=lambda e: e["id"])


def test_backfill_builds_from_todays_clips(two, capsys):
    grow(two)
    con = two.db()
    cid = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone()[0]
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Noah', tag='Noah' WHERE id=?", (cid,))
    con.execute("UPDATE conversations SET paragraph_offsets='[[0.5, 6.0]]' WHERE start_ms=?", (A,))
    con.commit()
    res = vl.backfill(con, two.DATA, {"speakers": {"allowed": ["Noah"]}}, dry_run=True)
    assert res["Noah"]["hand"] == 3 and vl.people(two.DATA) == []                  # dry run adds nothing
    res = vl.backfill(con, two.DATA, {"speakers": {"allowed": ["Noah"]}})
    con.close()
    assert res["Noah"]["clips"] >= 1 and vl.summary(two.DATA)["Noah"]["clips"] == res["Noah"]["clips"]
    assert vl.main(["--data", str(two.DATA)]) == 0


def test_auto_matches_join_at_rank_three_by_margin(two):
    import numpy as np
    grow(two)
    con = two.db()
    cid = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (A,)).fetchone()[0]
    con.execute("UPDATE speaker_clusters SET labeled_by='match', label='Noah', tag='Noah', embedding=? WHERE id=?",
                (np.array([1, 0, 0, 0], np.float32).tobytes(), cid))
    con.commit()
    cfg = {"speakers": {"allowed": ["Noah"], "match_threshold": 0.5}}
    assert vl.add_auto(con, two.DATA, cfg, DEV, A) == 1
    e = vl.load(two.DATA, "Noah")
    con.close()
    assert [x["kind"] for x in e] == ["auto"] and e[0]["rank"] == 3 and e[0]["score"] is not None
