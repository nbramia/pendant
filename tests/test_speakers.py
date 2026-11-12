"""pendant_direct.speakers: grouping, clustering, matching, voiceprints, diarize
with a fake embedder. One opt-in slow test loads the real ECAPA model."""
from __future__ import annotations

import os

import numpy as np
import pytest

from pendant_direct import speakers as sp
from synth_audio import write_tone

A = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
B = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
C = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)


def noisy(v, seed):
    r = np.random.default_rng(seed)
    return (v + r.normal(0, 0.05, v.shape)).astype(np.float32)


def test_group_segments_merges_short_and_drops_tiny():
    segs = [(0.0, 0.4, "a"), (0.5, 1.2, "b"), (1.4, 4.0, "c"), (10.0, 10.5, "d"), (20.0, 23.0, "e")]
    g = sp.group_segments(segs)
    assert g == [(0.0, 4.0, [0, 1, 2]), (20.0, 23.0, [4])]   # "d" alone is < 1 s


def test_group_segments_does_not_merge_across_gaps():
    g = sp.group_segments([(0.0, 0.8, "a"), (3.0, 3.6, "b")])
    assert g == []


def test_agglomerative_separates_voices():
    embs = np.stack([noisy(A, i) for i in range(4)] + [noisy(B, 10 + i) for i in range(3)])
    labels = sp.agglomerative(embs, 0.5)
    assert len(set(labels[:4])) == 1 and len(set(labels[4:])) == 1 and labels[0] != labels[4]
    assert len(set(sp.agglomerative(embs, -1.0))) == 1   # threshold low enough merges all
    assert list(sp.agglomerative(np.zeros((0, 4)), 0.5)) == []


def test_cluster_folds_tiny_clusters_and_assigns_overflow():
    embs = np.stack([noisy(A, 1), noisy(A, 2), noisy(B, 3), noisy(C, 4)])
    durs = np.array([5.0, 5.0, 4.0, 1.0])   # C has only 1 s of speech
    labels = sp.cluster(embs, durs, 0.5, min_cluster_s=3.0)
    assert labels[0] == labels[1] and labels[2] != labels[0]
    assert len(set(labels)) == 2
    labels = sp.cluster(embs, durs, 0.5, min_cluster_s=0, max_n=2)
    assert len(set(labels)) == 1    # only the two A spans were clustered; others join it


def test_match_uses_each_name_once():
    vps = {"Noah": (A, 3), "Alex": (B, 1)}
    cents = np.stack([noisy(B, 1), A, 0.8 * A + 0.3 * C, C])
    assert sp.match(cents, vps, 0.5) == ["Alex", "Noah", None, None]
    assert sp.match(cents, {}, 0.5) == [None] * 4


def test_enroll_consent_gate_and_running_mean(tmp_path):
    with pytest.raises(sp.ConsentError):
        sp.enroll(tmp_path, "Alex", A, ["Noah"])
    assert not list(tmp_path.iterdir())
    assert sp.enroll(tmp_path, "noah", A * 2, ["Noah"]) == 1
    assert sp.enroll(tmp_path, "Noah", B, ["Noah"]) == 2
    vps = sp.load_voiceprints(tmp_path)
    assert list(vps) == ["Noah"] and vps["Noah"][1] == 2
    np.testing.assert_allclose(vps["Noah"][0], (A + B) / 2, atol=1e-6)


def test_allowed_name_rejects_path_tricks():
    assert sp.allowed_name("../Noah", ["Noah"]) is None
    assert sp.allowed_name("Noah", ["../x"]) is None
    assert sp.allowed_name(" NOAH ", ["Noah"]) == "Noah"


SEGS = [(0.0, 3.0, "hi it's me"), (3.2, 3.5, "yeah"), (5.0, 8.0, "hello Noah"),
        (9.0, 12.0, "how was work"), (12.0, 16.0, "fine thanks")]


def by_text(voices):
    """Fake embedder: the voice of the first segment that starts inside each span."""
    def fn(clip, spans):
        out = []
        for s, e in spans:
            i = next(k for k, seg in enumerate(SEGS) if s <= seg[0] < e)
            out.append(noisy(voices[i], i))
        return np.stack(out)
    return fn


def test_diarize_labels_and_clusters(tmp_path):
    voices = [A, A, B, B, A]
    d = sp.diarize(tmp_path / "c.opus", SEGS, by_text(voices), {"Noah": (A, 5)},
                   cluster_threshold=0.5, match_threshold=0.5)
    assert d.show
    assert d.labels == ["Noah", "Noah", "Speaker A", "Speaker A", "Noah"]
    names = {c.tag: c for c in d.clusters}
    assert names["Noah"].name == "Noah" and names["Speaker A"].name is None
    assert names["Speaker A"].speech_s == pytest.approx(6.0)
    assert names["Speaker A"].excerpt in ("hello Noah", "how was work")
    assert 0 <= names["Speaker A"].start_s < names["Speaker A"].end_s <= 16


def test_diarize_shows_turns_between_two_voices_without_voiceprints(tmp_path):
    d = sp.diarize(tmp_path / "c.opus", SEGS, by_text([A, A, B, B, A]), {},
                   cluster_threshold=0.5, match_threshold=0.5)
    assert d.show
    assert sorted(c.tag for c in d.clusters) == ["Speaker A", "Speaker B"]


def test_diarize_single_unknown_voice_is_not_labelled(tmp_path):
    d = sp.diarize(tmp_path / "c.opus", SEGS, by_text([A, A, A, A, A]), {},
                   cluster_threshold=0.5, match_threshold=0.5)
    assert not d.show


def test_diarize_rejects_bad_embedder_output(tmp_path):
    with pytest.raises(ValueError):
        sp.diarize(tmp_path / "c.opus", SEGS, lambda c, s: np.zeros((1, 4)), {},
                   cluster_threshold=0.5, match_threshold=0.5)


def test_load_audio_decodes_ogg_opus(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "tone.ogg"
    write_tone(path, seconds=1.0)
    audio = sp.load_audio(path)
    assert audio.dtype == np.float32
    assert abs(len(audio) - 16000) < 1600
    assert np.abs(audio).max() > 0.05


@pytest.mark.skipif(not os.environ.get("PENDANT_SLOW"), reason="loads the real ECAPA model; set PENDANT_SLOW=1")
def test_real_ecapa_model_on_cpu(tmp_path):
    clip = tmp_path / "tone.ogg"
    write_tone(clip)
    emb = sp.EcapaEmbedder(os.environ.get("PENDANT_ECAPA_DIR", tmp_path / "ecapa"))
    out = emb(clip, [(0.0, 1.5), (1.5, 3.0)])
    assert out.shape == (2, 192)
    assert np.isfinite(out).all()


def test_cluster_keeps_main_voices_and_folds_short_outliers():
    # two people talking for minutes, plus a few short noisy spans that look like neither
    embs = [noisy(A, i) for i in range(10)] + [noisy(B, 100 + i) for i in range(8)] + [noisy(C, 200 + i) for i in range(3)]
    durs = np.array([30.0] * 10 + [25.0] * 8 + [3.0] * 3)   # C: 9 s total, under the 20 s floor
    labels = sp.cluster(np.stack(embs), durs, 0.25)
    assert len(set(labels)) == 2
    assert len(set(labels[:10])) == 1 and len(set(labels[10:18])) == 1 and labels[0] != labels[10]


# ---------------------------------------------------------------------------
# word-level turns

from pendant_direct.transcribe import Seg  # noqa: E402


def timeline_words(t0, t1, step=0.4, prefix="w"):
    out, t, n = [], t0, 0
    while t + step <= t1 + 1e-9:
        out.append((round(t, 3), round(t + step - 0.05, 3), f"{prefix}{n}"))
        t += step
        n += 1
    return out


def by_time(switches):
    """Fake embedder: the voice at a window's midpoint. switches = [(until_s, vec)]."""
    def fn(clip, spans):
        out = []
        for s, e in spans:
            m = (s + e) / 2
            out.append(next(v for until, v in switches if m < until))
        return np.stack(out)
    return fn


def one_segment(words):
    return [Seg(words[0][0], words[-1][1], " ".join(w[2] for w in words), words)]


def test_two_voices_inside_one_segment_split_at_the_word(tmp_path):
    words = timeline_words(0.0, 9.0)
    segs = one_segment(words)
    d = sp.diarize_words(tmp_path / "c.opus", segs, by_time([(5.0, A), (99, B)]), {},
                         cluster_threshold=0.5, match_threshold=0.5)
    assert d.show and len(d.turns) == 2
    first, second = d.turns
    assert first.label != second.label
    # the boundary lands within one word (0.4 s) of the true switch at 5 s
    assert abs(first.end - 5.0) <= 0.4 and abs(second.start - 5.0) <= 0.4
    assert first.text.split()[0] == "w0" and second.text.split()[-1] == words[-1][2]
    assert [t.label for t in d.turns] == ["Speaker A", "Speaker B"]
    assert len(d.clusters) == 2 and {c.tag for c in d.clusters} == {"Speaker A", "Speaker B"}
    assert d.labels == ["Speaker A"]    # the segment as a whole: A speaks 5 s of 9


def test_single_voice_keeps_whisper_text_and_is_not_shown(tmp_path):
    words = timeline_words(0.0, 6.0)
    segs = [Seg(0.0, 3.0, "first, text.", [w for w in words if w[1] <= 3.0]),
            Seg(3.0, 6.0, "second text.", [w for w in words if w[1] > 3.0])]
    d = sp.diarize_words(tmp_path / "c.opus", segs, by_time([(99, A)]), {},
                         cluster_threshold=0.5, match_threshold=0.5)
    assert not d.show
    assert [t.text for t in d.turns] == ["first, text.", "second text."]
    assert len(d.clusters) == 1 and d.labels == ["Speaker A"] * 2


def test_enrolled_voiceprint_names_window_cluster(tmp_path):
    segs = one_segment(timeline_words(0.0, 9.0))
    d = sp.diarize_words(tmp_path / "c.opus", segs, by_time([(5.0, A), (99, B)]), {"Noah": (A, 3)},
                         cluster_threshold=0.5, match_threshold=0.5)
    assert [t.label for t in d.turns] == ["Noah", "Speaker A"]
    assert d.show and [c.name for c in d.clusters] == ["Noah", None]
    noah = d.clusters[0]
    assert noah.speech_s == pytest.approx(4.2, abs=0.5) and noah.excerpt.startswith("w0")
    assert 0.0 <= noah.start_s < noah.end_s <= 5.5


def test_short_interjection_is_absorbed(tmp_path):
    words = timeline_words(0.0, 12.0)
    segs = one_segment(words)
    # B talks for only 0.6 s in the middle of A
    d = sp.diarize_words(tmp_path / "c.opus", segs, by_time([(5.7, A), (6.3, B), (99, A)]), {},
                         cluster_threshold=0.5, match_threshold=0.5)
    assert len(d.turns) == 1 and not d.show


def test_smooth_runs():
    words = [(i * 0.4, i * 0.4 + 0.35, "w") for i in range(20)]
    # A A A A A B B A A ... : the 0.8 s B run between A and A goes
    labels = [0] * 5 + [1] * 2 + [0] * 13
    assert sp.smooth_runs(words, labels) == [0] * 20
    # a 3 s run stays
    labels = [0] * 5 + [1] * 8 + [0] * 7
    assert sp.smooth_runs(words, labels) == labels
    # short run between two different voices joins the longer neighbour
    labels = [0] * 4 + [2] * 2 + [1] * 14
    assert sp.smooth_runs(words, labels) == [0] * 4 + [1] * 16
    # short edge run joins its neighbour
    assert sp.smooth_runs(words, [1] * 2 + [0] * 18) == [0] * 20
    assert sp.smooth_runs(words[:2], [0, 1]) == [0, 0] or sp.smooth_runs(words[:2], [0, 1]) == [1, 1]


def test_windows_skip_silence_and_cap(tmp_path):
    words = timeline_words(0.0, 4.0) + timeline_words(60.0, 64.0)
    wins, voiced = sp.voice_windows(words)
    assert wins and all(not (6.0 < s < 58.0) for s, _e in wins)   # nothing embedded in the gap
    assert len(wins) == len(voiced)
    many = timeline_words(0.0, 1500.0)
    with pytest.raises(ValueError):       # too long: never widen the hop, fall back instead
        sp.voice_windows(many, max_windows=100)
    wins, _ = sp.voice_windows(many)      # 1500 s of speech fits the default cap
    starts = [w[0] for w in wins]
    assert max(b - a for a, b in zip(starts, starts[1:])) <= sp.HOP_S + 1e-6


def test_segment_without_words_rejects_word_path(tmp_path):
    words = timeline_words(0.0, 8.0)
    segs = [Seg(0.0, 3.0, "has words", [w for w in words if w[1] <= 3.0]), Seg(3.5, 8.0, "no words")]
    with pytest.raises(ValueError):
        sp.diarize_words(tmp_path / "c.opus", segs, by_time([(99, A)]), {},
                         cluster_threshold=0.5, match_threshold=0.5)


def test_smoothing_counts_speech_not_silence():
    # B says two quick words 0.35 s each, with a 1.2 s pause between them: span > 1 s, speech 0.7 s
    words = [(i * 0.4, i * 0.4 + 0.35, "w") for i in range(5)]
    words += [(2.0, 2.35, "b"), (3.55, 3.9, "b")]
    words += [(4.0 + i * 0.4, 4.35 + i * 0.4, "w") for i in range(6)]
    labels = [0] * 5 + [1, 1] + [0] * 6
    assert sp.smooth_runs(words, labels) == [0] * 13


def test_diarize_words_needs_words_and_valid_embeddings(tmp_path):
    with pytest.raises(ValueError):
        sp.diarize_words(tmp_path / "c.opus", [(0.0, 1.0, "no words")], by_time([(99, A)]), {},
                         cluster_threshold=0.5, match_threshold=0.5)
    with pytest.raises(ValueError):
        sp.diarize_words(tmp_path / "c.opus", one_segment(timeline_words(0.0, 4.0)),
                         lambda c, s: np.zeros((1, 4)), {}, cluster_threshold=0.5, match_threshold=0.5)


def _unit_vec(*c):
    v = np.array(c, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_less_eager_naming_defaults():
    """Naming tuned on Noah's labels (#84): weak matches stay unnamed, margin knob exists but is off."""
    from pendant_direct.config import DEFAULTS
    d = DEFAULTS["speakers"]
    assert d["match_threshold"] == 0.55
    assert d["turn_person_floor"] == 0.4 and sp.TURN_PERSON_FLOOR == 0.4
    assert d["match_margin"] == 0.0


def test_weak_match_stays_unnamed_at_default_threshold():
    from pendant_direct.config import DEFAULTS
    thr = DEFAULTS["speakers"]["match_threshold"]
    vps = {"Noah": (_unit_vec(1, 0, 0), 5)}
    weak = _unit_vec(0.52, np.sqrt(1 - 0.52 ** 2), 0)      # cosine 0.52: used to be named
    strong = _unit_vec(0.7, np.sqrt(1 - 0.7 ** 2), 0)
    assert sp.match(np.stack([weak]), vps, 0.5) == ["Noah"]
    assert sp.match(np.stack([weak]), vps, thr) == [None]
    assert sp.match(np.stack([strong]), vps, thr) == ["Noah"]


def test_match_margin_leaves_ambiguous_voice_unnamed():
    vps = {"Noah": (_unit_vec(1, 0, 0), 5), "Alex": (_unit_vec(0.8, 0.6, 0), 2)}
    both = _unit_vec(1, 0.3, 0)                              # close to both people
    clear = _unit_vec(0, 0, 1) * 0.1 + _unit_vec(1, -0.2, 0)
    clear = clear / np.linalg.norm(clear)
    assert sp.match(np.stack([both]), vps, 0.55) == ["Noah"]
    assert sp.match(np.stack([both]), vps, 0.55, margin=0.1) == [None]
    assert sp.match(np.stack([clear]), vps, 0.55, margin=0.1) == ["Noah"]


def test_cluster_folds_fragments_when_no_voice_reaches_the_floor():
    # noisy audio: two people, each scattered over many clusters of a few seconds (none at 20 s)
    rng = np.random.default_rng(7)
    embs = [A + 1.2 * rng.standard_normal(A.shape) for _ in range(12)] + \
           [B + 1.2 * rng.standard_normal(B.shape) for _ in range(12)]
    durs = np.full(24, 1.5)                    # 36 s of talk, 18 s each
    labels = sp.cluster(np.stack(embs), durs, 0.9)   # a threshold that merges almost nothing
    assert len(set(labels)) <= 6               # each voice has at least 15% of the talk
    speech = np.bincount(labels, weights=durs)
    assert speech.min() >= sp.NO_BIG_SHARE * durs.sum()


def test_cluster_without_a_big_voice_keeps_two_short_voices_apart():
    # a short exchange: two voices of 8 s each, both under the 20 s floor, stay two voices
    embs = np.stack([noisy(A, i) for i in range(4)] + [noisy(B, 10 + i) for i in range(4)])
    labels = sp.cluster(embs, np.full(8, 2.0), 0.5)
    assert len(set(labels[:4])) == 1 and len(set(labels[4:])) == 1 and labels[0] != labels[4]


def _w(*items):
    out, t = [], 0.0
    for it in items:
        gap = 0.6 if it == "|" else None
        if gap:
            t += gap
            continue
        out.append((t, t + 0.3, it))
        t += 0.35
    return out


def test_snap_boundaries_moves_a_change_to_the_sentence_end():
    words = _w("That's", "what", "I", "said.", "I", "don't", "believe", "you.")
    labels = [0, 0, 0, 0, 0, 1, 1, 1]          # the change fell one word late
    out, info = sp.snap_boundaries(words, labels, 2)
    assert out == [0, 0, 0, 0, 1, 1, 1, 1] and info["moved"] == 1
    early = [0, 0, 0, 1, 1, 1, 1, 1]           # ...or one word early
    assert sp.snap_boundaries(words, early, 2)[0] == [0, 0, 0, 0, 1, 1, 1, 1]


def test_snap_boundaries_leaves_natural_and_unclear_changes_alone():
    words = _w("we", "went", "home.", "Then", "we", "ate")
    assert sp.snap_boundaries(words, [0, 0, 0, 1, 1, 1], 2)[0] == [0, 0, 0, 1, 1, 1]
    # a capitalized sentence start where it fell is natural; a comma nearby doesn't pull it
    words = _w("what", "comes", "next", "That's", "it,", "they're", "gone")
    assert sp.snap_boundaries(words, [0, 0, 0, 1, 1, 1, 1], 2)[0] == [0, 0, 0, 1, 1, 1, 1]
    # no punctuation or pause nearby: nothing moves; too far away: nothing moves
    words = _w("a", "b", "c", "d", "e", "f")
    assert sp.snap_boundaries(words, [0, 0, 0, 1, 1, 1], 2)[0] == [0, 0, 0, 1, 1, 1]
    words = _w("a.", "b", "c", "d", "e", "f")
    assert sp.snap_boundaries(words, [0, 0, 0, 0, 1, 1], 2)[0] == [0, 0, 0, 0, 1, 1]
    assert sp.snap_boundaries(words, [0, 0, 0, 1, 1, 1], 0)[0] == [0, 0, 0, 1, 1, 1]


def test_snap_boundaries_keeps_every_run_and_moves_to_a_pause():
    words = _w("x", "y", "|", "z", "w")
    assert sp.snap_boundaries(words, [0, 1, 1, 1], 2)[0] == [0, 0, 1, 1]   # into the pause
    words = _w("a.", "b", "c")
    out = sp.snap_boundaries(words, [0, 0, 1], 2)[0]
    assert out == [0, 1, 1]
    # a one-word run between two changes is never emptied
    words = _w("a.", "b", "c.", "d")
    out = sp.snap_boundaries(words, [0, 1, 2, 2], 2)[0]
    assert len(set(out)) == 3
