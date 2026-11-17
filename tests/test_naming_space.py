"""The naming space (naming.py, embedders.py, speakers.Namer): WeSpeaker models, their fetch and
front end, and voices named on whole stretches of speech. Fake embedders, synthetic audio; the
front-end sanity check runs the real model where it has been fetched."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import numpy as np
import pytest
from synth_audio import voice
from test_turn_rescore import KW, conversation, labels

from pendant_direct import config, embedders, naming, speakers

E = np.eye(4, dtype=np.float32)


@pytest.fixture(autouse=True)
def one_torch_thread():
    """fbank runs in torch: keep it on this thread, so no worker threads outlive the test (a later test
    forks processes)."""
    import torch
    n = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(n)


def u(*v):
    return speakers.normalize(np.array(v, dtype=np.float32))


# ---------------------------------------------------------------------------
# the model files: fetched once, checked

def test_fetch_installs_only_a_file_with_the_right_checksum(tmp_path, monkeypatch):
    body = b"not really a model"
    good = embedders.Model("m.onnx", hashlib.sha256(body).hexdigest(), len(body), 4)
    monkeypatch.setitem(embedders.MODELS, "fake", good)
    seen = []
    opener = lambda url: seen.append(url) or io.BytesIO(body)  # noqa: E731
    path = embedders.fetch(tmp_path, "fake", opener=opener)
    assert path == tmp_path / "models" / "m.onnx" and path.read_bytes() == body
    assert seen == [embedders._RELEASE + "m.onnx"]
    embedders.fetch(tmp_path, "fake", opener=lambda url: pytest.fail("already verified: no download"))
    monkeypatch.setitem(embedders.MODELS, "fake", embedders.Model("n.onnx", "0" * 64, len(body), 4))
    with pytest.raises(ValueError, match="checksum"):
        embedders.fetch(tmp_path, "fake", opener=lambda url: io.BytesIO(body))
    assert not (tmp_path / "models" / "n.onnx").exists() and not (tmp_path / "models" / "n.onnx.part").exists()


def test_a_missing_or_corrupt_model_says_how_to_fetch_it(tmp_path):
    with pytest.raises(embedders.ModelMissing, match="--fetch wespeaker-resnet293"):
        embedders.load(tmp_path, "wespeaker-resnet293")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / embedders.MODELS["wespeaker-resnet293"].file).write_bytes(b"x")
    with pytest.raises(embedders.ModelMissing):
        embedders.load(tmp_path, "wespeaker-resnet293")


def test_the_front_end_is_wespeakers_fbank():
    a = voice(120, 1.0, "aei", seconds=1.0)
    f = embedders.fbank(a)
    assert f.shape == (98, 80)                                   # 25 ms frames every 10 ms
    assert np.allclose(f.mean(0), 0, atol=1e-4)                  # each utterance's mean subtracted


class FakeSession:
    """Stands in for onnxruntime: the 'embedding' is each utterance's frame count and feature sum."""

    def __init__(self):
        self.batches = []

    def get_inputs(self):
        return [type("I", (), {"name": "feats"})()]

    def run(self, _outs, feeds):
        x = feeds["feats"]
        self.batches.append(x.shape)
        return [np.stack([[x.shape[1], float(np.abs(f).sum()), 1.0, 0.0] for f in x])]


def test_the_embedder_batches_equal_lengths_and_pads_short_audio():
    emb = embedders.WeSpeakerEmbedder(Path("unused.onnx"))
    emb._sess = FakeSession()
    a = voice(120, 1.0, "aeiou", seconds=3.0)
    out = emb.embed_audio(a, [(0.0, 1.5), (0.75, 2.25), (1.5, 3.0), (0.0, 0.2)])
    assert out.shape == (4, 4) and out[3, 0] == 48               # 0.2 s padded to 0.5 s: 48 frames
    assert sorted(emb._sess.batches) == [(1, 48, 80), (3, 148, 80)]


MODELS_DIR = Path(os.environ.get("PENDANT_MODELS", Path.home() / "pendant-direct-data" / "models"))


def two_channel_voices():
    """Three synthetic talkers, each heard once through a bright loud channel and once through a dull
    quiet one, so the per-utterance mean subtraction is what lets the model ignore the channel."""
    from scipy.signal import lfilter
    wavs, who = [], []
    for name, (f0, tract) in {"A": (110, 1.0), "B": (210, 0.85), "C": (150, 1.15)}.items():
        for j, vowels in enumerate(("aeiou", "ouiea")):
            x = voice(f0, tract, vowels, seed=j + ord(name))
            x = lfilter([1, -0.95], [1], x) if j == 0 else lfilter([1], [1, -0.9], x)
            wavs.append((x / np.abs(x).max() * (0.3 if j == 0 else 0.06)).astype(np.float32))
            who.append(name)
    return wavs, who


def separation(emb, wavs, who, front_end) -> float:
    """Mean same-speaker cosine minus mean different-speaker cosine."""
    sess = emb.session()
    x = speakers.normalize(np.stack([sess.run(None, {sess.get_inputs()[0].name: front_end(w)[None]})[0][0]
                                     for w in wavs]))
    s = x @ x.T
    pairs = [(i, j) for i in range(len(wavs)) for j in range(i + 1, len(wavs))]
    return float(np.mean([s[i, j] for i, j in pairs if who[i] == who[j]])
                 - np.mean([s[i, j] for i, j in pairs if who[i] != who[j]]))


@pytest.mark.parametrize("name", sorted(embedders.MODELS))
def test_front_end_sanity_same_speaker_scores_above_different(name):
    path = MODELS_DIR / embedders.MODELS[name].file
    if not path.exists():
        pytest.skip(f"{name} not fetched into {MODELS_DIR} (PENDANT_MODELS)")
    import torch
    import torchaudio.compliance.kaldi as kaldi
    emb = embedders.WeSpeakerEmbedder(path)
    wavs, who = two_channel_voices()
    assert separation(emb, wavs, who, embedders.fbank) > 0.05

    def no_mean_subtraction(a):     # the generic-wrapper mistake: the channel then outweighs the talker
        x = torch.from_numpy(a)[None] * (1 << 15)
        return kaldi.fbank(x, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                           sample_frequency=16000, window_type="hamming").numpy()
    assert separation(emb, wavs, who, no_mean_subtraction) < 0.0


# ---------------------------------------------------------------------------
# naming on whole stretches

def test_voice_pieces_join_a_voices_stretches_up_to_the_piece_length():
    words = [(0.0, 1.0, "a"), (1.2, 2.0, "b"), (3.0, 4.0, "x"), (5.0, 9.0, "c"), (9.1, 30.0, "d")]
    labels_ = [0, 0, 1, 0, 0]
    p = speakers.voice_pieces(words, labels_, 0, piece_s=10.0, max_pieces=6)
    assert p[0] == [(0.0, 2.0), (5.0, 13.0)]                     # 2 s + 8 s: one 10 s piece
    assert all(abs(sum(b - a for a, b in q) - 10.0) < 1e-6 for q in p[:-1])
    assert sum(b - a for q in p for a, b in q) == pytest.approx(27.0)
    assert len(speakers.voice_pieces(words, labels_, 0, piece_s=2.0, max_pieces=3)) == 3


class TwoSpace:
    """A fake model for the naming space: it hears each window's voice (like the clustering's fake)
    but answers in 3 dimensions, so mixing it with the 4-d window space would fail loudly."""

    def __init__(self, emb):
        self.emb, self.calls = emb, 0

    def __call__(self, clip, spans):
        self.calls += 1
        return self.emb(clip, spans)[:, :3]


def test_voices_are_named_in_the_naming_space(tmp_path):
    a, b = E[0], u(0, 1, 0, 0.2)
    prints = {"Noah": (np.array([1, 0, 0], np.float32), 3), "Alex": (np.array([0, 1, 0], np.float32), 3)}
    kw = {**KW, "cluster_threshold": 0.9}
    segs, emb = conversation([(a, 24), (b, 24), (a, 6)])
    for whole in (True, False):
        namer = speakers.Namer(embedder=TwoSpace(emb), whole=whole)
        d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **kw, namer=namer,
                                   rescorer=speakers.TurnScorer(prints))
        # 3-d voiceprints: naming and the rescoring's person scores ran in the naming space
        assert labels(d.turns) == ["Noah", "Alex", "Noah"] and namer.embedder.calls > 0
    with pytest.raises(ValueError):           # the 4-d window space can't be scored against them
        speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **kw)


def test_a_voice_with_too_little_speech_has_no_naming_embedding(tmp_path):
    a, b = E[0], u(0, 1, 0, 0.2)
    prints = {"Noah": (np.array([1, 0, 0], np.float32), 3), "Alex": (np.array([0, 1, 0], np.float32), 3)}
    segs, emb = conversation([(a, 3), (b, 3)])      # neither voice reaches 4 s of speech
    kw = {**KW, "cluster_threshold": 0.9}
    whole = speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **kw,
                                   namer=speakers.Namer(embedder=TwoSpace(emb), whole=True))
    assert all(t.startswith("Speaker ") for t in labels(whole.turns)) and len(set(labels(whole.turns))) == 2
    window = speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **kw,
                                    namer=speakers.Namer(embedder=TwoSpace(emb)))
    assert labels(window.turns) == ["Noah", "Alex"]


def test_the_default_space_is_the_clustering_windows_at_no_cost(tmp_path):
    assert naming.space({}) == ("ecapa", "window") and naming.namer({}, tmp_path) is None
    assert speakers.Namer().is_default
    with pytest.raises(ValueError):
        naming.space({"embedder": "nope"})
    assert "embedder" not in config.DEFAULTS["speakers"]          # the processor names in ECAPA windows


# ---------------------------------------------------------------------------
# the eval

def test_matched_threshold_accepts_the_same_share_of_other_person_trials():
    from pendant_direct import eval_speakers
    other = np.linspace(0.0, 0.99, 100)
    t = eval_speakers.matched_threshold(other, 0.05)
    assert (other >= t).mean() == pytest.approx(0.05, abs=0.011)


def test_the_eval_cache_keeps_joined_pieces_per_model(tmp_path):
    from pendant_direct import eval_speakers
    calls = []

    class Inner:
        def __call__(self, clip, spans):
            calls.append(len(spans))
            return np.ones((len(spans), 3), np.float32)
    clip = tmp_path / "c.opus"
    a = eval_speakers._cached_embedder(Inner(), clip, tmp_path, tag=".m1")
    b = eval_speakers._cached_embedder(Inner(), clip, tmp_path, tag=".m2")
    pieces = [[(0.0, 1.0), (2.0, 3.0)]]
    first = speakers.embed_pieces(a, clip, pieces)
    assert np.allclose(speakers.embed_pieces(a, clip, pieces), first) and calls == [2]
    speakers.embed_pieces(b, clip, pieces)
    assert calls == [2, 2]                                       # another model: its own entry
