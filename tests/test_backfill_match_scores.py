"""backfill_match_scores: scores clusters auto-matched before match_score was stored."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from pendant_direct import backfill_match_scores as bf
from pendant_direct import process, speakers

DEV = "0F1E2D3C4B5A6978"
T0 = 1_762_600_000_000


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Alex Rivera", "Noah Park"]}}))
    monkeypatch.setattr(process, "DB", data / "pages.db")
    vdir = speakers.voiceprint_dir(data)
    vdir.mkdir()
    speakers.enroll(vdir, "Alex Rivera", unit(1, 0, 0, 0), ["Alex Rivera", "Noah Park"])
    speakers.enroll(vdir, "Noah Park", unit(0, 1, 0, 0), ["Alex Rivera", "Noah Park"])
    con = process.db()
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day) VALUES (?,?,?,?,?)",
                (DEV, T0, T0 + 600_000, 600, "2025-11-08"))
    yield data, con
    con.close()


def add(con, label, labeled_by="match", emb=None, score=None, tag=None):
    blob = None if emb is None else np.asarray(emb, dtype=np.float32).tobytes()
    cur = con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by, embedding, match_score) "
                      "VALUES (?,?,?,?,?,?,?)", (DEV, T0, tag or label or "Speaker A", label, labeled_by, blob, score))
    con.commit()
    return cur.lastrowid


def row(con, cid):
    return con.execute("SELECT label, labeled_by, tag, match_score FROM speaker_clusters WHERE id=?", (cid,)).fetchone()


def test_scores_equal_what_the_processor_stores(env):
    data, con = env
    emb = unit(0.9, 0.3, 0, 0)
    cid = add(con, "Alex Rivera", emb=emb)
    n = bf.run(data, out=io.StringIO())
    assert n == {"scanned": 1, "scored": 1, "skipped_no_embedding": 0, "skipped_no_profile": 0}
    diar = SimpleNamespace(clusters=[SimpleNamespace(name="Alex Rivera", embedding=emb, match_score=None)])
    process.attach_match_scores(diar, speakers.load_voiceprints(speakers.voiceprint_dir(data)), None, 0.5, (DEV, T0))
    assert diar.clusters[0].match_score is not None
    assert row(con, cid)[3] == pytest.approx(diar.clusters[0].match_score)
    assert row(con, cid)[:3] == ("Alex Rivera", "match", "Alex Rivera")


def test_other_labels_and_scored_clusters_untouched(env):
    data, con = env
    ids = {k: add(con, "Alex Rivera", labeled_by=k, emb=unit(1, 0, 0, 0), tag=k)
           for k in ("user", "calendar", "meeting")}
    ids["scored"] = add(con, "Alex Rivera", emb=unit(1, 0, 0, 0), score=0.123, tag="scored")
    ids["unlabeled"] = add(con, None, labeled_by=None, emb=unit(1, 0, 0, 0), tag="u")
    before = {k: row(con, v) for k, v in ids.items()}
    n = bf.run(data, out=io.StringIO())
    assert n["scanned"] == 0 and n["scored"] == 0
    assert {k: row(con, v) for k, v in ids.items()} == before


def test_idempotent(env):
    data, con = env
    add(con, "Alex Rivera", emb=unit(1, 0.1, 0, 0))
    assert bf.run(data, out=io.StringIO())["scored"] == 1
    assert bf.run(data, out=io.StringIO())["scored"] == 0


def test_dry_run_writes_nothing(env):
    data, con = env
    cid = add(con, "Alex Rivera", emb=unit(1, 0.1, 0, 0))
    scores = []
    n = bf.run(data, dry_run=True, out=io.StringIO(), scores=scores)
    assert n["scored"] == 1 and len(scores) == 1
    assert row(con, cid)[3] is None


def test_skips_without_embedding_or_profile(env):
    data, con = env
    a = add(con, "Alex Rivera", emb=None, tag="a")
    b = add(con, "Sam Lee", emb=unit(1, 0, 0, 0), tag="b")
    n = bf.run(data, out=io.StringIO())
    assert n == {"scanned": 2, "scored": 0, "skipped_no_embedding": 1, "skipped_no_profile": 1}
    assert row(con, a)[3] is None and row(con, b)[3] is None
