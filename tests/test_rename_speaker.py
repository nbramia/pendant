"""rename_speaker: one speaker renamed in every place, spoken text untouched."""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from pendant_direct import config, enroll, meetings, process, rename_speaker, speakers, tasks, voices

NOTE = """## 09:00 — Chat

[09:00] **Alex:** Alex said hello. I told **Alex:** to wait.
[09:01] ⭐ **Alex:** a starred line
[09:02] **Noah:** Ask Alex about it
[09:03] **Speaker A:** **Alex:** inside spoken text

---
"""


def vp(seed, ids):
    rng = np.random.default_rng(seed)
    embs = rng.normal(size=(len(ids), 8)).astype(np.float32)
    return speakers.Voiceprint(np.zeros(8, np.float32), 0, list(ids), embs)


@pytest.fixture()
def world(tmp_path):
    data, notes = tmp_path / "data", tmp_path / "Raw"
    data.mkdir()
    notes.mkdir()
    (data / "config.json").write_text(json.dumps({"speakers": {
        "allowed": ["Noah", "Alex"], "wearer": "Noah", "people": {"Alex": "e-lex-w", "Noah": "e-noah"},
        "closeness": {"weight": 0.1}}, "notable": {"keywords": ["Alex"]}}))
    vdir = speakers.voiceprint_dir(data)
    vdir.mkdir()
    speakers._write(vdir, "Alex", vp(1, [1, 2]))
    speakers._write(vdir, "Noah", vp(2, [3]))
    (notes / "2026-11-08.md").write_text(NOTE)
    (notes / "2026-11-07.md").write_text("[08:00] **Noah:** nothing about the other name\n")
    lib = data / "voice_library" / "Alex"
    lib.mkdir(parents=True)
    (lib / "a.opus").write_bytes(b"a")
    (lib / "index.json").write_text(json.dumps([{"id": "a"}]))
    con = sqlite3.connect(data / "pages.db")
    con.execute(process.SPEAKER_CLUSTERS_SQL)
    voices.ensure_schema(con)
    con.executescript(enroll.SQL)
    con.executemany("INSERT INTO speaker_clusters (id, device, start_ms, tag, label) VALUES (?,?,?,?,?)",
                    [(1, "D", 1, "Alex", "Alex"), (2, "D", 1, "Speaker B", None), (3, "D", 2, "Noah", "Noah")])
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, para_index) VALUES ('D', 1, 'Speaker A', 'Alex', 2)")
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag) VALUES ('D', 1, 'Alex', 'Speaker C')")
    con.execute("INSERT INTO paragraph_samples (id, key, holder) VALUES ('p1', 1, 'Alex'), ('p2', 2, '#stranger')")
    con.execute("INSERT INTO enrollments (name, device, start_ms, end_ms, status) VALUES ('Alex', 'D', 1, 2, 'added')")
    con.commit()
    con.close()
    return data, notes


def rows(data, sql):
    con = sqlite3.connect(data / "pages.db")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_dry_run_reports_counts_and_changes_nothing(world):
    data, notes = world
    snap = {p: p.read_bytes() for p in [*data.rglob("*"), *notes.rglob("*")] if p.is_file() and p.suffix != ".db"}
    db = (data / "pages.db").read_bytes()
    out = io.StringIO()
    counts = rename_speaker.run(data, notes, "Alex", "Alex Rivera", out=out)
    assert counts["voiceprint"] == 1 and counts["samples"] == 2 and counts["merged"] is False
    assert counts["db speaker_clusters.label"] == 1 and counts["db speaker_clusters.tag"] == 1
    assert counts["db transcript_relabels.new_tag"] == 1 and counts["db transcript_relabels.old_tag"] == 1
    assert counts["db paragraph_samples.holder"] == 1 and counts["db enrollments.name"] == 1
    assert counts["notes_changed"] == 1 and counts["labels"] == 2 and counts["voice_library_clips"] == 1
    assert counts["config allowed"] == 1 and counts["config people"] == 1 and counts["config wearer"] == 0
    assert "dry run" in out.getvalue() and "--apply" in out.getvalue() and "db speaker_clusters.label: 1" in out.getvalue()
    assert {p: p.read_bytes() for p in snap} == snap and (data / "pages.db").read_bytes() == db
    assert not (data / "backups").exists()


def test_apply_renames_every_place_and_spoken_text_is_untouched(world):
    data, notes = world
    rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    vdir = speakers.voiceprint_dir(data)
    assert not (vdir / "Alex.npz").exists() and speakers.members(vdir, "Alex Rivera") == [1, 2]
    cfg = json.loads((data / "config.json").read_text())
    assert cfg["speakers"]["allowed"] == ["Noah", "Alex Rivera"]
    assert cfg["speakers"]["people"] == {"Alex Rivera": "e-lex-w", "Noah": "e-noah"}
    assert cfg["speakers"]["closeness"] == {"weight": 0.1} and cfg["notable"]["keywords"] == ["Alex"]
    assert rows(data, "SELECT id, tag, label FROM speaker_clusters ORDER BY id") == [
        (1, "Alex Rivera", "Alex Rivera"), (2, "Speaker B", None), (3, "Noah", "Noah")]
    assert rows(data, "SELECT old_tag, new_tag FROM transcript_relabels ORDER BY id") == [
        ("Speaker A", "Alex Rivera"), ("Alex Rivera", "Speaker C")]
    assert rows(data, "SELECT holder FROM paragraph_samples ORDER BY id") == [("Alex Rivera",), ("#stranger",)]
    assert rows(data, "SELECT name FROM enrollments") == [("Alex Rivera",)]
    text = (notes / "2026-11-08.md").read_text()
    assert text == NOTE.replace("[09:00] **Alex:**", "[09:00] **Alex Rivera:**").replace(
        "[09:01] ⭐ **Alex:**", "[09:01] ⭐ **Alex Rivera:**")
    assert "Alex said hello. I told **Alex:** to wait." in text      # spoken text, even a look-alike label
    assert "**Speaker A:** **Alex:** inside spoken text" in text       # not at a label position
    backups = list((data / "backups").glob("rename-speaker-*/2026-11-08.md"))
    assert len(backups) == 1 and backups[0].read_text() == NOTE
    assert not list((data / "backups").glob("rename-speaker-*/2026-11-07.md"))
    assert not (data / "voice_library" / "Alex").exists()
    assert (data / "voice_library" / "Alex Rivera" / "a.opus").read_bytes() == b"a"
    assert json.loads((data / "voice_library" / "Alex Rivera" / "index.json").read_text()) == [{"id": "a"}]
    # running it again finds nothing left
    again = rename_speaker.run(data, notes, "Alex", "Alex Rivera", out=io.StringIO())
    assert again["voiceprint"] == 0 and again["notes_changed"] == 0 and again["config allowed"] == 0


def test_renaming_into_an_existing_name_merges(world):
    data, notes = world
    vdir = speakers.voiceprint_dir(data)
    speakers._write(vdir, "Alex Rivera", vp(5, [2, 9]))       # cluster 2 is already a member
    lib = data / "voice_library" / "Alex Rivera"
    lib.mkdir(parents=True)
    (lib / "b.opus").write_bytes(b"b")
    (lib / "index.json").write_text(json.dumps([{"id": "b"}]))
    cfgp = data / "config.json"
    raw = json.loads(cfgp.read_text())
    raw["speakers"]["allowed"] = ["Noah", "Alex", "Alex Rivera"]
    cfgp.write_text(json.dumps(raw))
    c = rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert c["merged"] is True
    assert sorted(speakers.members(vdir, "Alex Rivera")) == [1, 2, 9] and not (vdir / "Alex.npz").exists()
    assert json.loads(cfgp.read_text())["speakers"]["allowed"] == ["Noah", "Alex Rivera"]
    assert sorted(f.name for f in lib.iterdir()) == ["a.opus", "b.opus", "index.json"]
    assert sorted(e["id"] for e in json.loads((lib / "index.json").read_text())) == ["a", "b"]


def test_wearer_rename_and_defaults(tmp_path):
    data, notes = tmp_path / "d", tmp_path / "n"
    data.mkdir()
    notes.mkdir()
    (data / "config.json").write_text("{}")                     # wearer and allowed are the defaults
    (notes / "2026-11-08.md").write_text("[09:00] **Noah:** hi\n")
    c = rename_speaker.run(data, notes, "Noah", "Noah Park", apply=True, out=io.StringIO())
    assert c["config wearer"] == 1 and c["config allowed"] == 1
    sp = config.load(data)["speakers"]
    assert sp["wearer"] == "Noah Park" and sp["allowed"] == ["Noah Park"]
    assert (notes / "2026-11-08.md").read_text() == "[09:00] **Noah Park:** hi\n"


def test_everything_reads_the_wearer_from_config():
    # task asks: the wearer is "Noah Park" and labels are the full name
    paras = tasks.parse_paragraphs(["[09:00] **Noah Park:** add milk to my to-do list.", "[09:01] **Alex Rivera:** no"])
    assert tasks.wearer_paragraphs(paras, "Noah Park") == ["add milk to my to-do list."]
    assert tasks.wearer_paragraphs(paras, "Noah") == []          # exact: a first name isn't the full name
    # Granola's "Me" becomes the wearer; a "Noah-Kai" title still finds the other person
    m = meetings.Meeting(path=Path("x.md"), title="Noah-Kai sync", source="granola", start_ms=0, end_ms=1,
                         people=["Noah Park", "Kai Kalani"], utterances=[(0, "Me", "hi"), (1, "Them", "yo")])
    assert meetings.meeting_names(m, "Noah Park") == {"Me": "Noah Park", "Them": "Kai"}
    m.title = "Weekly"
    assert meetings.meeting_names(m, "Noah Park") == {"Me": "Noah Park", "Them": "Kai Kalani"}


def test_refuses_when_new_is_a_different_linked_person(world):
    data, notes = world
    raw = json.loads((data / "config.json").read_text())
    raw["speakers"]["people"]["Alex Rivera"] = "e-someone-else"
    (data / "config.json").write_text(json.dumps(raw))
    before = (data / "config.json").read_text()
    with pytest.raises(rename_speaker.Refused):
        rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert (data / "config.json").read_text() == before
    assert (speakers.voiceprint_dir(data) / "Alex.npz").exists()
    assert "**Alex:**" in (notes / "2026-11-08.md").read_text()
    assert rename_speaker.main(["Alex", "Alex Rivera", "--data", str(data), "--notes", str(notes)]) == 2


def test_same_link_merges_and_bad_names_are_refused(world):
    data, notes = world
    raw = json.loads((data / "config.json").read_text())
    raw["speakers"]["people"]["Alex Rivera"] = "e-lex-w"
    (data / "config.json").write_text(json.dumps(raw))
    rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert json.loads((data / "config.json").read_text())["speakers"]["people"]["Alex Rivera"] == "e-lex-w"
    for bad in (("Alex", "Alex"), ("Alex", "T@ylor"), ("Alex", "")):
        with pytest.raises(rename_speaker.Refused):
            rename_speaker.run(data, notes, *bad, out=io.StringIO())


def test_waits_for_the_processor_lock(world):
    import fcntl
    data, notes = world
    with open(data / ".process.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(rename_speaker.Refused):
            rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO(), lock_wait_s=0.3)


# ---------------------------------------------------------------- review round 1

def state(data, notes):
    """Every file's bytes (backups aside) and the database's rows: the whole visible state."""
    files = {str(p.relative_to(data.parent)): p.read_bytes() for base in (data, notes) for p in base.rglob("*")
             if p.is_file() and "backups" not in p.parts and p.suffix != ".db" and not p.name.startswith(".")
             and not p.name.endswith("-journal")}
    con = sqlite3.connect(data / "pages.db")
    try:
        dump = list(con.iterdump())
    finally:
        con.close()
    return files, dump


@pytest.mark.parametrize("stage", ["apply_voiceprint", "apply_notes", "apply_library", "apply_config", "begin_db"])
def test_a_failure_at_any_stage_restores_everything(world, monkeypatch, stage):
    data, notes = world
    before = state(data, notes)
    real = getattr(rename_speaker, stage)

    def boom(*a, **k):
        out = real(*a, **k)           # the stage really wrote ...
        if stage == "begin_db" and out is not None:
            out.execute("ROLLBACK")
            out.close()
        raise OSError(f"injected after {stage}")   # ... and then failed
    monkeypatch.setattr(rename_speaker, stage, boom)
    with pytest.raises(OSError):
        rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert state(data, notes) == before
    assert (speakers.voiceprint_dir(data) / "Alex.npz").exists()
    assert not (speakers.voiceprint_dir(data) / "Alex Rivera.npz").exists()
    # and the rename still works afterwards
    monkeypatch.undo()
    rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert (speakers.voiceprint_dir(data) / "Alex Rivera.npz").exists()


def test_a_failed_commit_restores_everything(world, monkeypatch):
    data, notes = world
    before = state(data, notes)
    real = rename_speaker.apply_config

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("late")
    monkeypatch.setattr(rename_speaker, "apply_config", boom)
    with pytest.raises(RuntimeError):
        rename_speaker.run(data, notes, "Noah", "Noah Park", apply=True, out=io.StringIO())
    assert state(data, notes) == before
    assert rows(data, "SELECT label FROM speaker_clusters WHERE id = 3") == [("Noah",)]


def test_an_unreadable_note_fails_before_anything_is_written(world):
    data, notes = world
    (notes / "2026-11-06.md").write_bytes(b"\xff\xfe not utf-8 \x00 [09:00] **Alex:** x")
    before = state(data, notes)
    with pytest.raises(UnicodeDecodeError):
        rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert state(data, notes) == before


def test_enrolled_in_and_the_undo_records_are_renamed(world):
    data, notes = world
    con = sqlite3.connect(data / "pages.db")
    con.execute("UPDATE speaker_clusters SET enrolled_in = 'Alex' WHERE id = 1")
    con.execute("INSERT INTO bulk_relabels (batch, cluster_id, old_tag, old_label, old_enrolled_in, new_tag) "
                "VALUES ('b', 1, 'Alex', 'Alex', 'Alex', 'Speaker Z')")
    con.commit()
    con.close()
    c = rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert c["db speaker_clusters.enrolled_in"] == 1 and c["db bulk_relabels.old_enrolled_in"] == 1
    assert rows(data, "SELECT enrolled_in FROM speaker_clusters WHERE id = 1") == [("Alex Rivera",)]
    assert rows(data, "SELECT old_tag, old_label, old_enrolled_in FROM bulk_relabels") == [
        ("Alex Rivera", "Alex Rivera", "Alex Rivera")]


def test_library_merge_keeps_conflicting_clips_under_new_ids(world):
    data, notes = world
    dst = data / "voice_library" / "Alex Rivera"
    dst.mkdir()
    (dst / "a.opus").write_bytes(b"DIFFERENT")          # same id as the source's a.opus, other content
    (dst / "index.json").write_text(json.dumps([{"id": "a", "src": "cluster:9"}]))
    src = data / "voice_library" / "Alex"
    (src / "index.json").write_text(json.dumps([{"id": "a", "src": "cluster:1"}]))
    (src / "same.opus").write_bytes(b"identical")
    (dst / "same.opus").write_bytes(b"identical")
    rename_speaker.run(data, notes, "Alex", "Alex Rivera", apply=True, out=io.StringIO())
    assert not src.exists()
    idx = json.loads((dst / "index.json").read_text())
    assert (dst / "a.opus").read_bytes() == b"DIFFERENT"
    by_src = {e["src"]: e["id"] for e in idx}
    assert by_src["cluster:9"] == "a" and by_src["cluster:1"] != "a"
    assert (dst / f"{by_src['cluster:1']}.opus").read_bytes() == b"a"        # nothing was lost
    assert (dst / "same.opus").read_bytes() == b"identical"
    assert len(idx) == 2 and len({e["id"] for e in idx}) == 2


def test_case_only_rename_is_a_plain_rename_not_a_self_merge(world):
    data, notes = world
    vdir = speakers.voiceprint_dir(data)
    c = rename_speaker.run(data, notes, "Alex", "alex", apply=True, out=io.StringIO())
    assert c["merged"] is False
    assert speakers.members(vdir, "alex") == [1, 2]            # not [1, 2, 1, 2]
    assert speakers._read(vdir, "alex").count == 2
    assert "[09:00] **alex:**" in (notes / "2026-11-08.md").read_text()
    assert (data / "voice_library" / "alex" / "a.opus").exists()
    assert json.loads((data / "config.json").read_text())["speakers"]["allowed"] == ["Noah", "alex"]


def test_wearer_matching_is_exact():
    paras = tasks.parse_paragraphs(["[09:00] **Noah Smith:** add milk to my list.", "[09:01] **Noah Park:** add eggs to my list."])
    assert tasks.wearer_paragraphs(paras, "Noah Park") == ["add eggs to my list."]
    assert tasks.wearer_paragraphs(paras, "Noah") == []
    assert tasks.wearer_paragraphs(tasks.parse_paragraphs(["[09:00] **Noah:** x"]), "Noah Park") == []
