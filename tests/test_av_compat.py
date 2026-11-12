"""faster-whisper must be able to decode audio with the installed PyAV.

PyAV 18+ dropped av.open(metadata_errors=...), which faster-whisper 1.2 passes;
a dependency bump once broke every transcription silently until audio arrived.
"""
from pendant_direct.oggopus import OggOpusWriter


def test_faster_whisper_can_decode_our_ogg(tmp_path):
    from faster_whisper.audio import decode_audio
    clip = tmp_path / "c.opus"
    w = OggOpusWriter(clip)
    w.add_packet(b"\xf8\xff\xfe")  # one 20 ms Opus silence frame (CELT, 16 kHz)
    w.close()
    audio = decode_audio(str(clip), sampling_rate=16000)
    assert audio.ndim == 1


def test_speakers_load_audio_reads_our_ogg(tmp_path):
    from pendant_direct.speakers import load_audio
    clip = tmp_path / "c.opus"
    w = OggOpusWriter(clip)
    for _ in range(10):
        w.add_packet(b"\xf8\xff\xfe")
    w.close()
    audio = load_audio(clip)
    assert audio.dtype.name == "float32" and audio.size > 0


def test_segments_after_audio_end_are_dropped():
    from pendant_direct.transcribe import within_audio
    segs = [(0.0, 3.0, "hello"), (10.0, 12.5, "world"), (11.95, 23.95, "Thank you.")]
    assert within_audio(segs, 11.38) == [(0.0, 3.0, "hello"), (10.0, 11.38, "world")]
    assert within_audio(segs, None) == segs


def test_believable_filters_hallucinations():
    from pendant_direct.transcribe import believable
    assert not believable("So, I'm going to take a look at the top of my head.", -1.33)
    assert not believable("Thank you.", -1.01)
    assert not believable("You", -0.8)
    assert believable("You", -0.3)
    assert believable("Step out and chat with Emily for a few minutes.", -0.5)
