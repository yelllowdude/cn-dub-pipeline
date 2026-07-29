"""
extract_audio_16k must refuse a short extract.

The failure this guards against, observed on a real batch: on a Drive for
Desktop mount the master streams on demand, so ffmpeg reads only the cached
bytes, exits 0, and writes a WAV that just ends early. Four of five videos
extracted short -- one 181s of a 674s master -- and nothing downstream noticed:
Whisper transcribed what it was given, anchors covered only that span, and the
dub would have run out a quarter of the way in.
"""

import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import align  # noqa: E402


class Cfg:
    ffmpeg_path = "/fake/ffmpeg"


def _patch(src_s, got_s):
    """Make extraction a no-op and duration probing return these values."""
    align.subprocess.run = lambda *a, **k: None  # noqa: ARG005
    align.get_config = lambda: Cfg()
    import cn_pipeline.render as render
    calls = {"n": 0}

    def probe(_ffmpeg, path):
        calls["n"] += 1
        return (src_s if calls["n"] == 1 else got_s) * 1000
    render.probe_duration_ms = probe
    return calls


def test_full_length_extract_is_accepted():
    _patch(673.9, 673.9)
    with tempfile.TemporaryDirectory() as td:
        align.extract_audio_16k(Path(td) / "master.mov", Path(td) / "audio_16k.wav")


def test_sub_second_shortfall_is_tolerated():
    """Container rounding routinely differs by a few ms; that isn't truncation."""
    _patch(197.292, 197.291)
    with tempfile.TemporaryDirectory() as td:
        align.extract_audio_16k(Path(td) / "master.mov", Path(td) / "audio_16k.wav")


def test_the_real_truncation_is_rejected():
    """181s of a 674s master -- the case that silently shipped."""
    _patch(673.917, 181.341)
    with tempfile.TemporaryDirectory() as td:
        try:
            align.extract_audio_16k(Path(td) / "master.mov", Path(td) / "audio_16k.wav")
            raise AssertionError("expected RuntimeError on a 492s shortfall")
        except RuntimeError as e:
            msg = str(e)
            assert "181.3s" in msg and "673.9s" in msg
            assert "492.6s short" in msg
            # the error has to carry the fix, since the cause looks like nothing
            assert "cat " in msg and "--force" in msg


def test_a_modest_shortfall_is_still_rejected():
    """A 2s gap is not rounding, and a dub that ends 2s early is still wrong."""
    _patch(377.042, 375.0)
    with tempfile.TemporaryDirectory() as td:
        try:
            align.extract_audio_16k(Path(td) / "master.mov", Path(td) / "audio_16k.wav")
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass


def test_a_longer_extract_is_not_an_error():
    """Some containers report a shorter duration than their audio stream; only
    a SHORT extract loses content."""
    _patch(400.0, 400.5)
    with tempfile.TemporaryDirectory() as td:
        align.extract_audio_16k(Path(td) / "master.mov", Path(td) / "audio_16k.wav")


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
