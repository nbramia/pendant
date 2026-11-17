"""Speaker embedding models besides ECAPA: WeSpeaker ResNet ONNX exports, run on CPU.

`speakers.embedder` picks the model that names voices (`ecapa`, the default, or one below). Each
model file is fetched once into `<data>/models/` and checked against its SHA-256:

    python -m pendant_direct.embedders --fetch wespeaker-resnet293 [--data DIR]

The models are the WeSpeaker VoxCeleb ResNet "-LM" exports published with sherpa-onnx (CC BY 4.0,
see THIRD_PARTY_NOTICES.md). Their ONNX graphs take Kaldi fbank features, computed the way
WeSpeaker computes them: 80 mel bins, 25 ms frames every 10 ms, a Hamming window, no dither, on
samples scaled to the int16 range, then each utterance's mean subtracted. Fed anything else they
still return vectors, but the vectors no longer tell speakers apart.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"


@dataclass(frozen=True)
class Model:
    file: str
    sha256: str
    size: int
    dim: int

    @property
    def url(self) -> str:
        return _RELEASE + self.file


MODELS = {
    "wespeaker-resnet293": Model("wespeaker_en_voxceleb_resnet293_LM.onnx",
                                 "f65dbc820e534eef64ae12d1e289e20244d60e60f7f00d7b092092b1c458be2e", 114336527, 256),
    "wespeaker-resnet34": Model("wespeaker_en_voxceleb_resnet34_LM.onnx",
                                "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012", 26530550, 256),
}
NAMES = ("ecapa", *MODELS)


class ModelMissing(RuntimeError):
    """The model file isn't in place (or fails its checksum): run the fetch command."""


def model_path(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / "models" / MODELS[name].file


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: Path, name: str) -> bool:
    m = MODELS[name]
    return path.exists() and path.stat().st_size == m.size and _sha256(path) == m.sha256


def fetch(data_dir: Path, name: str, opener=urllib.request.urlopen) -> Path:
    """Download `name` into <data>/models/ unless a verified copy is there; the file lands only after
    its SHA-256 matches. Returns its path."""
    m = MODELS[name]
    path = model_path(data_dir, name)
    if verify(path, name):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    h = hashlib.sha256()
    with opener(m.url) as r, open(tmp, "wb") as f:
        for block in iter(lambda: r.read(1 << 20), b""):
            h.update(block)
            f.write(block)
    if h.hexdigest() != m.sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"{m.file}: checksum mismatch; nothing installed")
    os.replace(tmp, path)
    return path


def fbank(audio: np.ndarray):
    """WeSpeaker's front end for 16 kHz float audio in [-1, 1]: (frames, 80) log-mel features."""
    import torch
    import torchaudio.compliance.kaldi as kaldi
    x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None] * (1 << 15)
    f = kaldi.fbank(x, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                    sample_frequency=SAMPLE_RATE, window_type="hamming")
    return (f - f.mean(0)).numpy()


MIN_SAMPLES = SAMPLE_RATE // 2     # shorter audio is padded with silence to this length


class WeSpeakerEmbedder:
    """A WeSpeaker ResNet ONNX model behind the embedder interface speakers.py uses: called with a
    clip and [(start_s, end_s)] spans, or `embed_audio(audio, spans)`, or `embed_wavs([audio])`."""

    def __init__(self, path: Path, threads: int | None = None, name: str = "wespeaker"):
        self.path = Path(path)
        self.name = name
        self.threads = threads or min(8, max(1, (os.cpu_count() or 4) // 2))
        self._sess = None

    def session(self):
        if self._sess is None:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.threads
            so.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(str(self.path), so, providers=["CPUExecutionProvider"])
        return self._sess

    def embed_wavs(self, wavs: list) -> np.ndarray:
        sess = self.session()
        name = sess.get_inputs()[0].name
        wavs = [np.asarray(w, dtype=np.float32) for w in wavs]
        wavs = [w if len(w) >= MIN_SAMPLES else np.pad(w, (0, MIN_SAMPLES - len(w))) for w in wavs]
        out: list = [None] * len(wavs)
        by_len: dict[int, list[int]] = {}
        for i, w in enumerate(wavs):
            by_len.setdefault(len(w), []).append(i)
        for idx in by_len.values():     # same-length audio goes through as one batch
            for j in range(0, len(idx), 32):
                part = idx[j:j + 32]
                feats = np.stack([fbank(wavs[i]) for i in part])
                for i, v in zip(part, sess.run(None, {name: feats})[0]):
                    out[i] = v
        return np.stack(out).astype(np.float32)

    def embed_audio(self, audio: np.ndarray, spans) -> np.ndarray:
        return self.embed_wavs([audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)] for s, e in spans])

    def __call__(self, clip: Path, spans) -> np.ndarray:
        from .speakers import load_audio
        return self.embed_audio(load_audio(clip), spans)


def load(data_dir: Path, name: str) -> WeSpeakerEmbedder:
    """The embedder for `name`, from its verified model file (ModelMissing when absent or corrupt)."""
    if name not in MODELS:
        raise ValueError(f"unknown speakers.embedder {name!r}; one of {', '.join(NAMES)}")
    path = model_path(data_dir, name)
    if not verify(path, name):
        raise ModelMissing(f"{path} missing or corrupt: python -m pendant_direct.embedders --fetch {name}")
    return WeSpeakerEmbedder(path, name=name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    ap.add_argument("--fetch", choices=sorted(MODELS), required=True, help="download and verify this model")
    args = ap.parse_args(argv)
    path = fetch(args.data.expanduser(), args.fetch)
    print(f"{args.fetch}: {path} (sha256 verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
