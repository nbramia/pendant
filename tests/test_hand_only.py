"""Only the wearer's own answers teach voiceprints: short voices keep no automatic name, and
`hand_only` takes the samples stored before the rule out (recoverably). Fake embeddings, fixture people."""
from __future__ import annotations

import json

import numpy as np

from pendant_direct import hand_only, speakers


def test_a_short_voice_keeps_no_automatic_name_except_the_wearer():
    names = ["Alex Rivera", "Noah Park", None, "Morgan Hale"]
    speech = {0: 14.0, 1: 5.0, 2: 3.0, 3: 45.0}
    assert speakers.unname_short(names, speech, 30.0, "Noah Park") == [None, "Noah Park", None, "Morgan Hale"]
    assert speakers.unname_short(names, speech, 0.0, "Noah Park") == names        # off


def _setup(tmp_path):
    from pendant_direct import process
    data = tmp_path
    con = process.db(data / "pages.db")
    rows = [(1, "user", None), (2, "meeting", None), (3, "calendar", None), (4, "user", None),
            (5, "user", "sitting")]
    for cid, by, via in rows:
        con.execute("INSERT INTO speaker_clusters (id, device, start_ms, tag, label, labeled_by, named_via, embedding, "
                    "enrolled_in) VALUES (?, 'DEV', ?, 'Noah Park', 'Noah Park', ?, ?, ?, 'Noah Park')",
                    (cid, cid * 1000, by, via, np.eye(4, dtype=np.float32)[cid % 4].tobytes()))
    con.commit()
    vdir = speakers.voiceprint_dir(data)
    for cid in (1, 2, 3, 4, 5):
        speakers.add(vdir, "Noah Park", cid, np.eye(4, dtype=np.float32)[cid % 4])
    speakers.add(vdir, "Noah Park", -7, np.ones(4, np.float32) / 2)            # a line he reassigned: his
    vp = speakers._read(vdir, "Noah Park")
    vp.legacy_sum, vp.legacy = np.ones(4, np.float32) * 2, 2                    # two samples of unknown origin
    with speakers.voiceprint_lock(vdir):
        speakers._write(vdir, "Noah Park", vp)
    # a sitting answer recorded before named_via existed: voice 4 reached from another conversation
    m = data / "reassign_undo" / "x"
    m.mkdir(parents=True)
    (m / "manifest.json").write_text(json.dumps({
        "scope": "conversation", "device": "DEV", "start_ms": 1000,
        "before": {"speaker_clusters": [{"id": 4, "device": "DEV", "start_ms": 4000, "labeled_by": None, "label": None}]},
        "after": {"speaker_clusters": [{"id": 4, "device": "DEV", "start_ms": 4000, "labeled_by": "user",
                                        "label": "Noah Park"}]}}))
    return con, vdir


def test_hand_only_takes_out_every_sample_not_from_his_answers_and_restores(tmp_path):
    con, vdir = _setup(tmp_path)
    before = speakers._read(vdir, "Noah Park")
    p = hand_only.plan(tmp_path)
    take = p["people"]["Noah Park"]["take"]
    assert take == {"meeting": [2], "calendar": [3], "sitting": [4, 5]} and p["people"]["Noah Park"]["legacy"] == 2
    backup = hand_only.apply(tmp_path, p)
    vp = speakers._read(vdir, "Noah Park")
    assert sorted(vp.ids) == [-7, 1] and vp.legacy == 0 and not vp.legacy_sum.any()
    rows = dict(con.execute("SELECT id, enrolled_in FROM speaker_clusters").fetchall())
    assert rows == {1: "Noah Park", 2: "", 3: "", 4: "", 5: ""}
    assert con.execute("SELECT named_via FROM speaker_clusters WHERE id = 4").fetchone() == ("sitting",)
    assert (backup / "voiceprints" / "Noah Park.npz").exists()
    assert hand_only.plan(tmp_path)["people"]["Noah Park"]["take"] == {}       # nothing left to take
    hand_only.restore(tmp_path, backup)
    back = speakers._read(vdir, "Noah Park")
    assert back.ids == before.ids and back.legacy == 2 and np.allclose(back.mean(), before.mean())
    assert dict(con.execute("SELECT id, enrolled_in FROM speaker_clusters").fetchall())[2] == "Noah Park"
    assert con.execute("SELECT named_via FROM speaker_clusters WHERE id = 4").fetchone() == (None,)
