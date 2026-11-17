"""Kept whole voices (pendant_direct.voice_audio): what is kept, its hooks, retention, the trash and the eval."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from pendant_direct import eval_speakers, feed, naming, retention, speakers, trash
from pendant_direct import voice_audio as va, voice_library as vl
from pendant_direct.oggopus import OggOpusWriter
from test_process import fake_embedder, fake_whisper, proc, run  # noqa: F401
from test_trash import A, B, DEV, delete, restore, two  # noqa: F401

LINES = [[1.0, 4.0], [4.0, 6.0], [6.0, 9.0]]


def packet(i: int) -> bytes:
    return bytes([0xB8]) + i.to_bytes(2, "little") * 3


@pytest.fixture
def conv(two):
    """Conversation B as three lines of a 30 s clip: Noah, a second voice (Speaker B), Noah again."""
    for f in (two.DATA / "clips").glob("*.opus"):
        w = OggOpusWriter(f)
        for i in range(1500):
            w.add_packet(packet(i))
        w.close()
    daily = two.NOTES_DIR / "2026-01-05.md"
    daily.write_text(daily.read_text().replace(
        "[10:20] **Noah:** hello there",
        "[10:20] **Noah:** hello there\n\n[10:20] **Speaker B:** hi\n\n[10:21] **Noah:** how are you"))
    con = two.db()
    con.execute("UPDATE conversations SET paragraph_offsets=? WHERE start_ms=?", (json.dumps(LINES), B))
    con.execute("UPDATE speaker_clusters SET labeled_by=NULL, label=NULL, start_s=1, end_s=9, speech_s=8 "
                "WHERE start_ms=?", (B,))
    cb = con.execute("SELECT id FROM speaker_clusters WHERE start_ms=?", (B,)).fetchone()[0]
    clip, emb = con.execute("SELECT clip, embedding FROM speaker_clusters WHERE id=?", (cb,)).fetchone()
    con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, embedding, clip, start_s, end_s, speech_s) "
                "VALUES (?,?,?,?,?,?,?,?)", (DEV, B, "Speaker B", emb, clip, 4, 6, 2))
    con.commit()
    con.close()
    two.cb = cb
    return two


def kept(proc, start=B) -> dict:
    return va.load(va.conv_dir(proc.DATA, DEV, start))


def packets_of(proc, person, start=B):
    return vl.read_packets(va.conv_dir(proc.DATA, DEV, start) / f"{va.stem(person)}.opus")


def frames(*spans):
    return [packet(i) for a, b in spans for i in range(int(round(a / 0.02)), int(round(b / 0.02)))]


def sync(proc, start=B, **kw):
    con = proc.db()
    try:
        return va.sync_conversation(con, proc.DATA, proc.NOTES_DIR, feed.cfgmod.load(proc.DATA), DEV, start, **kw)
    finally:
        con.close()


def test_naming_a_voice_keeps_only_its_lines_and_removing_the_name_drops_them(conv):
    assert kept(conv) == {}
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    m = kept(conv)["Noah"]
    assert m["segments"] == [[1.0, 4.0], [6.0, 9.0]] and m["file_segments"] == [[0.0, 3.0], [3.0, 6.0]]
    assert m["cluster_ids"] == [conv.cb] and m["day"] == "2026-01-05" and m["speech_s"] == 6.0
    assert packets_of(conv, "Noah") == frames((1, 4), (6, 9))                   # its lines only, packet-exact
    feed.stranger_cluster(conv.cb, reassign=True)                              # "a random person"
    assert kept(conv) == {} and not va.conv_dir(conv.DATA, DEV, B).exists()


def test_moving_a_voice_to_someone_else_moves_its_kept_voice(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    feed.label_cluster(conv.cb, "Alex", "user", reassign=True)
    k = kept(conv)
    assert list(k) == ["Alex"] and k["Alex"]["tags"] == ["Alex"]
    assert packets_of(conv, "Alex") == frames((1, 4), (6, 9))


def test_a_line_reassigned_away_leaves_the_kept_voice(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    feed.reassign_speaker(DEV, B, {"name": "Alex", "source": "user", "scope": "line", "paragraph": 2})
    assert kept(conv)["Noah"]["segments"] == [[1.0, 4.0]] and packets_of(conv, "Noah") == frames((1, 4))
    feed.reassign_speaker(DEV, B, {"name": "Noah", "source": "user", "scope": "line", "paragraph": 1})
    assert kept(conv)["Noah"]["segments"] == [[1.0, 4.0], [4.0, 6.0]]   # a line tapped to him joins


def test_only_allowed_unmixed_hand_named_people_are_kept(conv):
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Mallory', tag='Noah', label_kind='person' "
                "WHERE id=?", (conv.cb,))
    con.commit()
    assert sync(conv)["kept"] == 0                                              # not in speakers.allowed
    for cols in ("label='Noah', mixed=1", "label='Noah', mixed=0, noise=1", "label='Noah', noise=0, "
                 "labeled_by='match'", "labeled_by='user', label_kind='stranger'"):
        con.execute(f"UPDATE speaker_clusters SET {cols} WHERE id=?", (conv.cb,))
        con.commit()
        assert sync(conv)["kept"] == 0 and kept(conv) == {}, cols
    con.execute("UPDATE speaker_clusters SET label_kind=NULL WHERE id=?", (conv.cb,))   # rows from before the column
    con.commit()
    con.close()
    assert sync(conv)["kept"] == 1 and list(kept(conv)) == ["Noah"]


def test_a_queued_relabel_or_unreadable_note_leaves_files_alone(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    before = packets_of(conv, "Noah")
    con = conv.db()
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, created_at, para_index) "
                "VALUES (?,?,?,?,?,?)", (DEV, B, None, "Alex", 1.0, 0))
    con.commit()
    con.close()
    r = sync(conv)
    assert r["skipped"] == {"relabel queued": 1} and packets_of(conv, "Noah") == before
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label_kind='stranger', label=NULL WHERE id=?",
                (conv.cb,))
    con.commit()
    con.close()
    assert sync(conv)["removed"] == 1 and kept(conv) == {}          # a removed name needs no note


def test_after_the_clip_is_gone_a_kept_voice_only_loses_lines(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    for f in (conv.DATA / "clips").glob("*.opus"):
        f.unlink()
    f = va.conv_dir(conv.DATA, DEV, B) / "Noah.opus"
    stamp = f.stat().st_mtime_ns
    assert sync(conv)["unchanged"] == 1 and f.stat().st_mtime_ns == stamp
    feed.reassign_speaker(DEV, B, {"name": "Noah", "source": "user", "scope": "line", "paragraph": 1})
    assert sync(conv)["unchanged"] == 1 and kept(conv)["Noah"]["segments"] == [[1.0, 4.0], [6.0, 9.0]]
    feed.reassign_speaker(DEV, B, {"name": "Alex", "source": "user", "scope": "line", "paragraph": 0})
    m = kept(conv)["Noah"]
    assert m["segments"] == [[6.0, 9.0]] and m["file_segments"] == [[0.0, 3.0]]
    assert packets_of(conv, "Noah") == frames((6, 9))               # cut from the kept file itself


def test_a_renamed_person_keeps_the_audio_after_the_clip_is_gone(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    for f in (conv.DATA / "clips").glob("*.opus"):
        f.unlink()
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET label='Nora', tag='Nora' WHERE id=?", (conv.cb,))
    con.commit()
    con.close()
    daily = conv.NOTES_DIR / "2026-01-05.md"
    daily.write_text(daily.read_text().replace("**Noah:**", "**Nora:**"))
    feed.cfgmod.allow_name(conv.DATA, "Nora")
    assert sync(conv)["recut"] == 1
    assert list(kept(conv)) == ["Nora"] and packets_of(conv, "Nora") == frames((1, 4), (6, 9))


def test_retention_keeps_a_voice_before_taking_its_clip_and_never_deletes_it(conv):
    con = conv.db()                     # named without the feed's hook: retention's own pass keeps it
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Noah', tag='Noah' WHERE id=?", (conv.cb,))
    con.commit()
    rep = retention.run(con, conv.DATA, now_ms=retention.parse_now("2027-06-01"), keep_starred=False)
    con.close()
    assert not list((conv.DATA / "clips").glob("*.opus"))
    assert packets_of(conv, "Noah") == frames((1, 4), (6, 9))
    assert packets_of(conv, "Noah", A)                                 # conversation A's hand-named voice too
    assert rep["voice_audio"]["voices"] == 2 and rep["voice_audio"]["bytes"] > 0
    assert "voice audio of hand-named voices (never purged): 2 voices" in retention.format_report(rep, dry=False)
    con = conv.db()
    retention.run(con, conv.DATA, now_ms=retention.parse_now("2028-06-01"), keep_starred=False)
    con.close()
    assert packets_of(conv, "Noah") == frames((1, 4), (6, 9))


def test_backfill_dry_run_counts_bytes_and_writes_nothing_then_is_idempotent(conv, capsys):
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Noah', tag='Noah' WHERE id=?", (conv.cb,))
    con.commit()
    con.close()
    args = ["--data", str(conv.DATA), "--backfill", "--json"]
    assert va.main([*args, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["backfill"]["kept"] == 2 and not va.root(conv.DATA).exists()
    assert va.main(args) == 0
    real = json.loads(capsys.readouterr().out)
    assert real["backfill"]["kept"] == 2 and real["kept"]["voices"] == 2
    assert dry["backfill"]["bytes"] == real["kept"]["bytes"]                   # the estimate is exact
    assert va.main(args) == 0
    again = json.loads(capsys.readouterr().out)["backfill"]
    assert again["kept"] == again["recut"] == 0 and again["unchanged"] == 2
    assert va.main(["--data", str(conv.DATA), "--backfill", "--dry-run"]) == 0
    assert "2 already kept" in capsys.readouterr().out


def test_trash_takes_the_kept_voice_with_the_conversation_and_restore_brings_it_back(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    d = va.conv_dir(conv.DATA, DEV, B)
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    delete(conv, B)
    assert not d.exists()
    assert sorted(p.name for p in (conv.DATA / "trash" / f"{DEV}-{B}" / "voice_audio").iterdir()) == sorted(before)
    con = conv.db()
    va.sync_all(con, conv.DATA, conv.NOTES_DIR, feed.cfgmod.load(conv.DATA))
    con.close()
    assert not d.exists()                                    # a deleted conversation's voice isn't kept again
    restore(conv, B)
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
    delete(conv, B)
    con = conv.db()
    trash.purge(con, conv.DATA, now=__import__("time").time() + 8 * 86400)
    con.close()
    assert not d.exists() and not (conv.DATA / "trash" / f"{DEV}-{B}").exists()


# ---------------------------------------------------------------------------
# the eval: voiceprints from kept whole voices

def test_kept_pieces_and_windows_cover_the_lines():
    p = naming.kept_pieces([[0.0, 15.0], [15.0, 40.0]], piece_s=20.0)
    assert p == [[(0.0, 15.0), (15.0, 20.0)], [(20.0, 40.0)]]
    assert len(naming.kept_pieces([[0, 100]], piece_s=10.0, max_pieces=3)) == 3
    w = naming.kept_windows([[0.0, 3.0], [3.0, 3.2], [5.0, 6.0]])
    assert w[:3] == [(0.0, 1.5), (0.75, 2.25), (1.5, 3.0)] and w[-1] == (5.0, 6.0)   # 0.2 s: no window
    assert len(naming.kept_windows([[0, 4000]], max_windows=50)) == 50


class ByLevel:
    """A fake model hearing who speaks from the audio's level: level k is voice k."""

    def __init__(self):
        self.calls = 0

    def embed_wavs(self, wavs):
        self.calls += 1
        return np.stack([np.eye(4, dtype=np.float32)[int(round(float(w.mean())))] for w in wavs])

    def embed_audio(self, audio, spans):
        return self.embed_wavs([audio[int(a * 16000):int(b * 16000)] for a, b in spans])


@pytest.fixture
def evaldata(tmp_path, monkeypatch):
    """Noah and Alex in three conversations each, hand-named. Their clip snippets sound alike (level 3);
    the kept whole voices tell them apart (level 0 Noah, 1 Alex). Two voices of Noah share conversation 0
    (one kept voice); the second one's snippet sounds like him."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah", "Alex"]}}))
    con = sqlite3.connect(data / "pages.db")
    con.execute("CREATE TABLE speaker_clusters (id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, "
                "label TEXT, embedding BLOB, clip TEXT, start_s REAL, end_s REAL, speech_s REAL, labeled_by TEXT, "
                "label_kind TEXT, mixed INT, noise INT)")
    con.execute("CREATE TABLE conversations (device TEXT, start_ms INT, day TEXT)")
    levels: dict[str, float] = {}
    cid = 0
    for k in range(3):
        con.execute("INSERT INTO conversations VALUES (?,?,?)", ("D", k, "2026-01-05"))
        for person, level in (("Noah", 0), ("Alex", 1)):
            cid += 1
            ids = [cid]
            stored = np.eye(4, dtype=np.float32)[level if k else 2]       # stored: good, except conversation 0
            con.execute("INSERT INTO speaker_clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, "D", k, person, person, stored.tobytes(), "c.opus", 0, 5, 30, "user", "person", 0, 0))
            if person == "Noah" and k == 0:
                cid += 1
                ids.append(cid)
                con.execute("INSERT INTO speaker_clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (cid, "D", k, person, person, stored.tobytes(), "c.opus", 0, 5, 30, "user", "person", 0, 0))
            d = va.conv_dir(data, "D", k)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{person}.opus").write_bytes(b"x")
            (d / f"{person}.json").write_text(json.dumps(
                {"person": person, "device": "D", "start_ms": k, "cluster_ids": ids, "other_ids": [],
                 "segments": [[0, 30]], "file_segments": [[0.0, 30.0]], "speech_s": 30, "bytes": 1}))
            levels[str(d / f"{person}.opus")] = level
    con.commit()
    con.close()
    monkeypatch.setattr(speakers, "load_audio", lambda p: np.full(16000 * 30, levels[str(p)], np.float32))
    monkeypatch.setattr(naming, "voice_audio",
                        lambda con, data, ids, lib=None: {i: np.full(16000 * 5, 0.0 if i == 2 else 3.0, np.float32)
                                                          for i in ids})
    return data


def test_whole_embeddings_file_each_kept_voice_under_its_lowest_hand_id(evaldata, tmp_path):
    emb = ByLevel()
    con = sqlite3.connect(evaldata / "pages.db")
    W, covered = eval_speakers.whole_embeddings(con, evaldata, ("ecapa", "whole"), tmp_path / "cache", emb)
    assert sorted(W) == [1, 3, 4, 5, 6, 7] and covered == set(range(1, 8))   # Noah's 1+2 are one kept voice
    assert np.allclose(W[1], np.eye(4)[0]) and np.allclose(W[3], np.eye(4)[1])
    calls = emb.calls
    again, _ = eval_speakers.whole_embeddings(con, evaldata, ("ecapa", "whole"), tmp_path / "cache", emb)
    con.close()
    assert emb.calls == calls and all(np.allclose(again[c], W[c]) for c in W)   # cached
    labeled = [(c, ("D", 0), "Noah", None) for c in (1, 2)] + [(8, ("D", 9), "Alex", None)]
    E = {1: "e1", 2: "e2", 8: "e8"}
    assert [r[0] for r in eval_speakers.print_pool(labeled, E, "whole", W, covered, set())] == [1, 8]
    assert [r[3] for r in eval_speakers.print_pool(labeled, E, "whole", W, covered, set())][1] == "e8"
    assert [r[0] for r in eval_speakers.print_pool(labeled, E, "whole-only", W, covered, set())] == [1]
    assert [r[0] for r in eval_speakers.print_pool(labeled, E, "rebuilt", W, covered, {2})] == [1, 8]


def test_spaces_name_voices_right_from_kept_whole_voices(evaldata, tmp_path):
    import io
    sp = (("ecapa", "whole"),)
    clips = eval_speakers.space_eval(evaldata, spaces=sp, out=io.StringIO(), ecapa=ByLevel())
    out = io.StringIO()
    whole = eval_speakers.space_eval(evaldata, spaces=sp, out=out, ecapa=ByLevel(),
                                     overrides={"voiceprints": "whole"})
    assert clips[1]["matched"][0] < 6                       # the snippets can't tell them apart
    # every kept voice right, none wrong (Noah's second voice in conversation 0 can't take his name twice)
    assert whole[1]["matched"] == (6, 0)
    assert "voiceprints=whole: 6 kept whole voices cover 7 hand-named voices" in out.getvalue()


# ---------------------------------------------------------------------------
# a keep that didn't succeed never loses the audio

def audio_left(proc, start):
    con = proc.db()
    try:
        clip = con.execute("SELECT clip FROM conversations WHERE start_ms=?", (start,)).fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM chunks WHERE opus IS NOT NULL AND ts_ms BETWEEN ? AND ?",
                        (start - 1000, start + 60_000)).fetchone()[0]
    finally:
        con.close()
    return (proc.DATA / "clips" / __import__("pathlib").Path(clip).name).exists(), n > 0


def purge_old(proc):
    con = proc.db()
    try:
        return retention.run(con, proc.DATA, now_ms=retention.parse_now("2027-06-01"), keep_starred=False)
    finally:
        con.close()


def test_retention_holds_a_conversation_whose_voice_could_not_be_kept_until_a_later_pass(conv, monkeypatch):
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Noah', tag='Noah' WHERE id=?", (conv.cb,))
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, created_at, para_index) "
                "VALUES (?,?,?,?,?,?)", (DEV, B, None, "Noah", 1.0, 1))
    con.commit()
    con.close()
    rep = purge_old(conv)
    assert audio_left(conv, B) == (True, True) and rep["voice_audio_held"] == [(DEV, B)]
    assert audio_left(conv, A) == (False, False) and packets_of(conv, "Noah", A)   # A was kept, then purged
    assert "conversations held until their hand-named voices are kept: 1" in retention.format_report(rep, dry=False)
    con = conv.db()
    con.execute("UPDATE transcript_relabels SET done_at=1")
    con.commit()
    con.close()
    purge_old(conv)                                                  # the next pass keeps it, then purges
    assert audio_left(conv, B) == (False, False) and packets_of(conv, "Noah") == frames((1, 4), (6, 9))


def test_a_failed_cut_or_a_failed_sync_holds_the_audio(conv, monkeypatch):
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label='Noah', tag='Noah' WHERE id=?", (conv.cb,))
    con.commit()
    con.close()

    def broken(*a, **k):
        raise OSError("disk full")
    cut, sync_all = va._cut, va.sync_all
    monkeypatch.setattr(va, "_cut", broken)
    rep = purge_old(conv)
    assert sorted(rep["voice_audio_held"]) == [(DEV, A), (DEV, B)] and audio_left(conv, B) == (True, True)
    monkeypatch.setattr(va, "sync_all", broken)
    assert purge_old(conv)["voice_audio_held"] == [(DEV, A), (DEV, B)]
    assert audio_left(conv, A) == (True, True) and audio_left(conv, B) == (True, True)
    monkeypatch.setattr(va, "_cut", cut)
    monkeypatch.setattr(va, "sync_all", sync_all)
    purge_old(conv)
    assert audio_left(conv, B) == (False, False) and packets_of(conv, "Noah") == frames((1, 4), (6, 9))


def test_a_moved_voice_keeps_its_old_file_until_the_new_one_is_written(conv):
    feed.label_cluster(conv.cb, "Noah", "user", reassign=True)
    for f in (conv.DATA / "clips").glob("*.opus"):
        f.unlink()
    con = conv.db()                     # the lines' spans changed: the old file can't give them
    con.execute("UPDATE conversations SET paragraph_offsets=? WHERE start_ms=?", ("[[2, 4], [4, 6], [6, 8]]", B))
    con.commit()
    con.close()
    feed.label_cluster(conv.cb, "Alex", "user", reassign=True)
    assert list(kept(conv)) == ["Noah"] and packets_of(conv, "Noah") == frames((1, 4), (6, 9))
    r = sync(conv)
    assert r["skipped"] == {"no audio": 1, "replacement not written": 1} and r["held"] and r["removed"] == 0
    con = conv.db()
    con.execute("UPDATE speaker_clusters SET labeled_by='user', label_kind='stranger', label=NULL WHERE id=?",
                (conv.cb,))
    con.commit()
    con.close()
    assert sync(conv)["removed"] == 1 and kept(conv) == {}          # a removed name still drops it
