"""The sitting (pendant_direct.sitting): which neighbouring pieces belong to one real
conversation (cap cuts, short gaps, a shared calendar event, a break that ends it), which
voices in them match the tapped voice and get its name (the floor, several voices of one
person, the exclusions), and the feed applying it for "This conversation" and for Who's this
and Voice Scoring answers as one undoable change. Fake 4-d embeddings."""
from __future__ import annotations

import json
import urllib.request

import numpy as np
import pytest
from test_reassign_undo import state
from test_voices import DEV, env, unit  # noqa: F401

from pendant_direct import feed, sitting, speakers

MIN = 60_000
T0 = 1_700_000_000_000
DAY = "2026-11-08"
TAPPED = [0, 1, 0, 0]
WEARER = "Morgan Hale"


def piece(start_min, minutes, voices_=(), device=DEV, event=None):
    s = T0 + int(start_min * MIN)
    return {"device": device, "start_ms": s, "end_ms": s + int(minutes * MIN),
            "calendar_event": json.dumps(event) if event else None, "voices": list(voices_)}


def voice(vid, emb, tag="Speaker A", **kw):
    return {"id": vid, "tag": tag, "label": kw.pop("label", None), "labeled_by": kw.pop("labeled_by", None),
            "label_kind": kw.pop("label_kind", None), "noise": kw.pop("noise", 0), "mixed": kw.pop("mixed", 0),
            "embedding": unit(emb), **kw}


# ---------------------------------------------------------------------------
# which pieces are one sitting (pure)

def test_cap_cut_short_gap_and_overlap_link_pieces():
    a = piece(0, 27)
    assert sitting.linked(a, piece(27 + 2 / 60, 5))                       # back to back
    assert sitting.linked(a, piece(27 + 50 / 60, 5), gap_s=10)            # the 30-minute cap cut at a pause
    assert sitting.linked(piece(0, 5), piece(5 + 2, 5))                   # a 2-minute gap, under 3
    assert sitting.linked(piece(0, 5), piece(4, 5))                       # overlapping
    assert not sitting.linked(piece(0, 5), piece(5 + 4, 5))               # 4 minutes of silence
    assert not sitting.linked(piece(0, 5), piece(5 + 2, 5, device="AAAA"))
    assert not sitting.linked({**piece(0, 5), "end_ms": None}, piece(6, 5))   # no end time: linked to nothing


def test_a_shared_calendar_event_links_pieces_across_a_long_gap():
    ev = {"id": "ev-1", "title": "Planning", "start_ms": T0, "end_ms": T0 + 120 * MIN}
    assert sitting.linked(piece(0, 20, event=ev), piece(50, 20, event=ev))
    other = {**ev, "id": "ev-2"}
    assert not sitting.linked(piece(0, 20, event=ev), piece(50, 20, event=other))
    assert not sitting.linked(piece(0, 20, event=ev), piece(50, 20))


def test_chain_stops_at_a_break():
    ps = [piece(0, 27), piece(27.05, 5), piece(32.2, 28), piece(60.5, 5), piece(80, 10), piece(90.1, 5)]
    assert sitting.chain(ps, 2) == (0, 3)
    assert sitting.chain(ps, 4) == (4, 5)
    assert sitting.settings({}) == (sitting.FLOOR, sitting.GAP_S)
    assert sitting.settings({"sitting_floor": None, "sitting_gap_s": 60}) == (None, 60.0)


# ---------------------------------------------------------------------------
# matching (pure)

def four_pieces():
    """An hour as four pieces (cap cuts and short gaps); the tapped voice is in the last."""
    return [
        piece(0, 27, [voice(11, [0, 1, 0.2, 0]), voice(12, [1, 0, 0, 0], tag="Speaker B")]),
        piece(27.05, 5, [voice(21, [0.1, 1, 0, 0], tag="Noah", label="Noah", labeled_by="match"),
                         voice(22, [0, 0, 1, 0], tag="Speaker B")]),
        piece(32.2, 28, [voice(31, [0, 1, 0.3, 0]), voice(32, [0, 1, 0.6, 0], tag="Speaker C"),
                         voice(33, [0, 0.6, 0.8, 0], tag="Speaker D")]),
        piece(60.5, 5, [voice(41, TAPPED, tag="Speaker B")]),
    ]


def tapped_of(ps, vid=41):
    p = next(p for p in ps if any(v["id"] == vid for v in p["voices"]))
    v = next(v for v in p["voices"] if v["id"] == vid)
    return {"id": vid, "device": p["device"], "start_ms": p["start_ms"], "mixed": v["mixed"],
            "embedding": v["embedding"]}


def test_the_matching_voice_is_named_in_every_piece():
    ps = four_pieces()
    p = sitting.plan(tapped_of(ps), ps, 0.7, wearer=WEARER)
    assert p["parts"] == 4 and p["before"] == 3 and p["after"] == 0
    assert p["span_s"] == round((ps[3]["end_ms"] - ps[0]["start_ms"]) / 1000)
    # piece 3 holds two voices of the same person (a split): both are named, best first
    assert sorted(sitting.ids(p)) == [11, 21, 31, 32]
    by_piece = {x["start_ms"]: [n["id"] for n in x.get("named", [])] for x in p["pieces"]}
    assert by_piece[ps[2]["start_ms"]] == [31, 32]
    # an automatic name for someone else that clears the floor is replaced
    assert 21 in sitting.ids(p)
    # below the floor: never named
    assert 33 not in sitting.ids(p) and 12 not in sitting.ids(p) and 22 not in sitting.ids(p)
    assert sitting.summary(p)["named_parts"] == 3


def test_the_floor_decides():
    ps = four_pieces()
    p = sitting.plan(tapped_of(ps), ps, 0.9, wearer=WEARER)
    # piece 3's split voice (0.86) is under 0.9 but its other voice (0.96) is not
    assert 32 not in sitting.ids(p) and 31 in sitting.ids(p)
    off = sitting.plan(tapped_of(ps), ps, None, wearer=WEARER)
    assert off["parts"] == 1 and off["named"] == []


@pytest.mark.parametrize("kw,reason,present", [
    ({"labeled_by": "user", "label": "Alex", "label_kind": "person", "tag": "Alex"}, "hand", True),
    ({"labeled_by": "user", "label_kind": "stranger", "tag": "Speaker E"}, "hand", True),
    ({"labeled_by": "dismissed", "noise": 1, "label_kind": "noise"}, "noise", False),
    ({"labeled_by": "match", "label": WEARER, "tag": WEARER}, "wearer", False),
    ({"labeled_by": "user", "label": WEARER, "tag": WEARER, "label_kind": "person"}, "wearer", False),
    ({"mixed": 1}, "mixed", True),
])
def test_hand_wearer_noise_and_mixed_voices_are_never_renamed(kw, reason, present):
    ps = four_pieces()
    ps[2]["voices"] = [voice(31, [0, 1, 0.3, 0], **kw)]
    p = sitting.plan(tapped_of(ps), ps, 0.7, wearer=WEARER)
    assert 31 not in sitting.ids(p)
    if present:   # the person is there: the sitting goes on past this piece
        kept = next(x for x in p["pieces"] if x["start_ms"] == ps[2]["start_ms"])["kept"]
        assert kept[0]["reason"] == reason and p["parts"] == 4 and {11, 21} <= set(sitting.ids(p))
    else:         # the wearer and noise don't show the person: the sitting ends there
        assert p["parts"] == 1 and p["ended"]["before"] == "no matching voice"


def test_a_shared_tag_keeps_its_name():
    ps = four_pieces()
    ps[2]["voices"] = [voice(31, [0, 1, 0.3, 0], tag="Speaker A"), voice(34, [1, 0, 0, 0], tag="Speaker A")]
    p = sitting.plan(tapped_of(ps), ps, 0.7, wearer=WEARER)
    kept = next(x for x in p["pieces"] if x["start_ms"] == ps[2]["start_ms"])["kept"]
    assert 31 not in sitting.ids(p) and kept[0]["reason"] == "shared"


def test_a_piece_without_the_voice_ends_the_sitting():
    ps = four_pieces()
    ps[1]["voices"] = [voice(21, [0, 0, 1, 0])]       # nobody like the tapped voice here
    p = sitting.plan(tapped_of(ps), ps, 0.7, wearer=WEARER)
    assert p["parts"] == 2 and 11 not in sitting.ids(p) and p["ended"]["before"] == "no matching voice"


def test_a_gap_ends_the_sitting_and_both_directions_walk():
    ps = four_pieces() + [piece(66, 20, [voice(51, [0, 1, 0, 0.2])]), piece(100, 10, [voice(61, TAPPED)])]
    p = sitting.plan(tapped_of(ps, 31), ps, 0.7, wearer=WEARER)
    assert p["before"] == 2 and p["after"] == 2 and 61 not in sitting.ids(p) and p["ended"]["after"] == "not linked"


def test_a_mixed_tapped_voice_has_no_sitting():
    ps = four_pieces()
    t = {**tapped_of(ps), "mixed": 1}
    assert sitting.plan(t, ps, 0.7)["named"] == []


# ---------------------------------------------------------------------------
# the feed: "This conversation" covers the sitting

@pytest.fixture()
def st(env):
    """Four pieces on one pendant written to the day's note; the tapped voice ("Speaker B") is in
    the last, plus a later conversation after a long break."""
    env.con.execute("DELETE FROM conversations")
    env.con.commit()
    cfg = json.loads((env.data / "config.json").read_text())
    cfg["speakers"].update({"allowed": ["Noah", "Alex", WEARER], "wearer": WEARER})
    (env.data / "config.json").write_text(json.dumps(cfg))
    parts = ["# Sunday"]

    def conv(start_min, minutes, paras, voices_):
        s = T0 + int(start_min * MIN)
        heading = f"10:{int(start_min) % 60:02d} — Part {start_min} (1m00s) · old"
        body = "\n\n".join(f"[10:{i:02d}] **{t}:** {w}" for i, (t, w) in enumerate(paras))
        parts.append(f"## {heading}\n\n{body}\n\n---\n")
        (env.notes / f"{DAY}.md").write_text("\n\n".join(parts))
        env.con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading) VALUES (?,?,?,?,?)",
                        (DEV, s, s + int(minutes * MIN), DAY, heading))
        env.con.commit()
        return s, [env.add(emb, tag=tag, start_ms=s, **kw) for tag, emb, kw in voices_]
    env.p1, (env.v1, env.w1) = conv(0, 27, [("Speaker A", "first part words"), ("Morgan Hale", "mine")],
                                    [("Speaker A", [0, 1, 0.2, 0], {}),
                                     ("Morgan Hale", [1, 0, 0, 0], {"label": WEARER, "labeled_by": "match"})])
    env.p2, (env.v2,) = conv(27.05, 5, [("Noah", "second part words")],
                             [("Noah", [0.1, 1, 0, 0], {"label": "Noah", "labeled_by": "match"})])
    env.p3, (env.v3, env.v3b) = conv(32.2, 28, [("Speaker A", "third part"), ("Speaker C", "far voice")],
                                     [("Speaker A", [0, 1, 0.3, 0], {}), ("Speaker C", [0, 0.3, 0.95, 0], {})])
    env.p4, (env.tapped,) = conv(60.5, 5, [("Speaker B", "the tapped line")], [("Speaker B", TAPPED, {})])
    env.p5, (env.v5,) = conv(90, 10, [("Speaker A", "after the break")], [("Speaker A", TAPPED, {})])
    env.daily = lambda: (env.notes / f"{DAY}.md").read_text()
    return env


def tag_of(e, cid):
    return e.con.execute("SELECT tag, label, labeled_by FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()


def test_this_conversation_names_the_voice_in_every_part_and_undoes_in_one_tap(st, monkeypatch):
    kept = []
    monkeypatch.setattr(feed, "_keep_voices", lambda con, d, ms: kept.append(ms))
    s0 = state(st)
    out = feed.reassign_recorded(DEV, st.p4, {"scope": "conversation", "tag": "Speaker B", "name": "Riley Park",
                                              "source": "user"})
    assert out["sitting"]["parts"] == 4 and out["sitting"]["before"] == 3 and out["sitting"]["after"] == 0
    assert sorted(out["sitting"]["ids"]) == sorted([st.v1, st.v2, st.v3])
    for cid in (st.tapped, st.v1, st.v2, st.v3):
        assert tag_of(st, cid) == ("Riley Park", "Riley Park", "user")
    # under the floor, the wearer, and past the break: untouched
    assert tag_of(st, st.v3b)[2] is None and tag_of(st, st.w1) == (WEARER, WEARER, "match")
    assert tag_of(st, st.v5)[2] is None
    text = st.daily()
    for line in ("**Riley Park:** first part words", "**Riley Park:** second part words",
                 "**Riley Park:** third part", "**Riley Park:** the tapped line", "**Speaker C:** far voice",
                 "**Morgan Hale:** mine", "**Speaker A:** after the break"):
        assert line in text
    # each named voice teaches the voiceprint as a hand tag does, and its whole voice is kept
    assert sorted(speakers.members(st.vdir, "Riley Park")) == sorted([st.tapped, st.v1, st.v2, st.v3])
    assert set(kept) == {st.p1, st.p2, st.p3, st.p4}
    assert st.con.execute("SELECT COUNT(*) FROM reassign_undo").fetchone()[0] == 1
    feed.undo_reassign(out["change_id"])
    assert state(st) == s0


def test_someone_else_named_automatically_and_voiceprint_samples_move(st):
    st.enroll("Noah", unit([0.1, 1, 0, 0]), cid=st.v2)
    st.con.execute("UPDATE speaker_clusters SET enrolled_in = 'Noah' WHERE id = ?", (st.v2,))
    st.enroll("Noah", unit([1, 0.2, 0, 0]), cid=900)
    st.con.commit()
    s0 = state(st)
    out = feed.reassign_recorded(DEV, st.p4, {"scope": "conversation", "tag": "Speaker B", "name": "Alex",
                                              "source": "user"})
    assert st.v2 in out["sitting"]["ids"] and speakers.members(st.vdir, "Noah") == [900]
    feed.undo_reassign(out["change_id"])
    assert state(st) == s0 and sorted(speakers.members(st.vdir, "Noah")) == sorted([900, st.v2])


def test_remove_name_and_this_line_stay_in_their_piece(st):
    out = feed.reassign_recorded(DEV, st.p4, {"scope": "conversation", "tag": "Speaker B", "remove": True})
    assert "sitting" not in out and tag_of(st, st.v1)[2] is None
    out = feed.reassign_recorded(DEV, st.p4, {"scope": "line", "paragraph": 0, "name": "Alex", "source": "user"})
    assert "sitting" not in out and tag_of(st, st.v1)[2] is None


def test_sitting_floor_off_names_only_this_piece(st):
    cfg = json.loads((st.data / "config.json").read_text())
    cfg["speakers"]["sitting_floor"] = None
    (st.data / "config.json").write_text(json.dumps(cfg))
    out = feed.reassign_recorded(DEV, st.p4, {"scope": "conversation", "tag": "Speaker B", "name": "Alex",
                                              "source": "user"})
    assert out["sitting"]["parts"] == 1 and tag_of(st, st.v1)[2] is None


def test_whos_this_answer_names_the_sitting_with_one_undo(st):
    s0 = state(st)
    out = feed.label_answer(st.tapped, "Alex", "user")
    assert out["sitting"]["named_parts"] == 3 and len(out["change_id"]) == 32
    assert tag_of(st, st.v3) == ("Alex", "Alex", "user")
    assert st.con.execute("SELECT answered FROM voice_offers WHERE cluster_id = ?",
                          (st.v3,)).fetchone() == (1,)              # Voice Scoring stops asking about it
    feed.undo_reassign(out["change_id"])
    assert state(st) == s0


def test_an_answer_with_nothing_else_to_name_records_no_change(st):
    out = feed.label_answer(st.v5, "Alex", "user")           # after the break: a sitting of one
    assert "change_id" not in out and "sitting" not in out
    assert st.con.execute("SELECT COUNT(*) FROM reassign_undo").fetchone()[0] == 0
    assert tag_of(st, st.v5) == ("Alex", "Alex", "user")


def test_sitting_payload_reports_the_coverage_and_changes_nothing(st):
    s0 = state(st)
    out = feed.sitting_payload(DEV, st.p4, "Speaker B")
    assert out["parts"] == 4 and out["named"] == 3 and out["before"] == 3
    assert out["span_s"] == round((st.p4 + 5 * MIN - st.p1) / 1000)
    assert feed.sitting_payload(DEV, st.p5, "Speaker A")["parts"] == 1
    assert feed.sitting_payload(DEV, st.p4, "Nobody")["parts"] == 1
    with pytest.raises(feed.HTTPError):
        feed.sitting_payload(DEV, 1, "Speaker B")
    assert state(st) == s0


def test_http_sitting_route(st):
    import threading
    srv = feed.make_server(0, "tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/pendant/feed/conversations/{DEV}/{st.p4}/speaker/Speaker%20B/sitting"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer tok"})
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["parts"] == 4 and out["named_parts"] == 3
    finally:
        srv.shutdown()


def test_dry_run_reads_only_and_prints_no_names(st, capsys):
    st.con.commit()
    before = state(st)
    sitting.main([str(st.tapped), "--data", str(st.data)])
    text = capsys.readouterr().out
    assert "4 parts" in text and "name   voice" in text and "(wearer)" not in text
    assert "Noah" not in text and WEARER not in text and "Riley" not in text
    assert state(st) == before
    sitting.main(["--eval", "--data", str(st.data)])
    assert "floor" in capsys.readouterr().out


def test_voices_of_the_eval_use_the_hand_labels(st):
    for cid in (st.v1, st.v3):
        st.con.execute("UPDATE speaker_clusters SET label = 'Alex', tag = 'Alex', labeled_by = 'user', "
                       "label_kind = 'person' WHERE id = ?", (cid,))
    st.con.execute("UPDATE speaker_clusters SET label = 'Noah', tag = 'Noah', labeled_by = 'user', "
                   "label_kind = 'person' WHERE id = ?", (st.v3b,))
    st.con.commit()
    ev = sitting.evaluate(st.con, {"wearer": WEARER}, floors=(0.7,))
    assert ev["tapped"] == 3
    c = ev["floors"][0.7]
    assert c["right"] == 2 and c["wrong"] == 0       # v1 <-> v3 both ways; Noah's voice is under the floor
    assert np.max(ev["pairs"]["different"]) < 0.7
