"""
Tests for the cross-machine reproducibility guards:
  - encoder selection (renders must work off macOS)
  - repo-versioned output settings + drift detection
  - atomic .env writes
"""

import json
import os
import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import config as cfgmod  # noqa: E402
from cn_pipeline import render  # noqa: E402


# --- encoder selection -------------------------------------------------------

def _fake_encoders(listing: str):
    """Patch render's subprocess.run so encoder probing sees `listing`."""
    class R:
        stdout = listing
    render._encoder_cache.clear()
    render.subprocess.run = lambda *a, **k: R()  # noqa: ARG005


def test_prefers_videotoolbox_when_present():
    _fake_encoders("V..... h264_videotoolbox\nV..... libx264\n")
    args = render.video_encoder_args("/fake/ffmpeg")
    assert args[:2] == ["-c:v", "h264_videotoolbox"], args


def test_falls_back_to_libx264_off_macos():
    """The gap that made the pipeline macOS-only: no videotoolbox on Linux."""
    _fake_encoders("V..... libx264\nV..... libvpx\n")
    args = render.video_encoder_args("/fake/ffmpeg")
    assert args[:2] == ["-c:v", "libx264"], args
    assert "-crf" in args, args


def test_raises_when_no_h264_encoder_at_all():
    _fake_encoders("V..... libvpx\n")
    try:
        render.video_encoder_args("/fake/ffmpeg")
    except RuntimeError as e:
        assert "libx264" in str(e)
        assert "doctor" in str(e)
    else:
        raise AssertionError("expected RuntimeError when no H.264 encoder exists")


def test_encoder_result_is_cached():
    _fake_encoders("V..... h264_videotoolbox\n")
    render.video_encoder_args("/fake/ffmpeg")

    def boom(*a, **k):  # noqa: ARG001
        raise AssertionError("should have used the cache")
    render.subprocess.run = boom
    assert render.video_encoder_args("/fake/ffmpeg")[:2] == ["-c:v", "h264_videotoolbox"]


# --- output settings ---------------------------------------------------------

def test_no_overrides_reported_when_config_matches_repo():
    defaults = cfgmod.load_output_defaults()
    assert defaults, "pipeline_defaults.json should ship with the repo"
    assert cfgmod.output_setting_overrides(dict(defaults)) == []


def test_override_is_detected():
    """The real case: me_gain_db -4.0 in the repo, 2.0 on one machine, so that
    operator's M&E bed sat 6 dB hotter than everyone else's. (That override was
    compensating for a distinction the single default didn't model -- see
    test_me_gain.py -- which is exactly why it needed surfacing.)"""
    overrides = cfgmod.output_setting_overrides({"me_gain_db": 2.0})
    assert len(overrides) == 1, overrides
    key, repo_default, local = overrides[0]
    assert key == "me_gain_db"
    assert local == 2.0
    assert repo_default != 2.0


def test_machine_local_keys_are_not_flagged():
    """drive_root and friends are legitimately per-machine."""
    assert cfgmod.output_setting_overrides(
        {"drive_root": "/somewhere", "ffmpeg_path": "/usr/bin/ffmpeg"}) == []


def test_defaults_cover_every_declared_key():
    defaults = cfgmod.load_output_defaults()
    missing = [k for k in cfgmod.OUTPUT_SETTING_KEYS if k not in defaults]
    assert not missing, f"pipeline_defaults.json is missing: {missing}"


# --- atomic .env write -------------------------------------------------------

def test_save_env_var_is_atomic_and_preserves_other_keys():
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / ".env"
        env.write_text("A=1\nB=2\n", encoding="utf-8")
        orig = cfgmod.ENV_PATH
        cfgmod.ENV_PATH = env
        try:
            cfgmod.save_env_var("B", "changed")
            cfgmod.save_env_var("C", "new")
            text = env.read_text(encoding="utf-8")
            assert "A=1" in text
            assert "B=changed" in text and "B=2" not in text
            assert "C=new" in text
            # no temp files left behind
            leftovers = [p.name for p in Path(td).iterdir() if p.name != ".env"]
            assert leftovers == [], leftovers
        finally:
            cfgmod.ENV_PATH = orig


def test_save_env_var_keeps_credentials_file_private():
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / ".env"
        orig = cfgmod.ENV_PATH
        cfgmod.ENV_PATH = env
        try:
            cfgmod.save_env_var("TOKEN", "secret")
            assert oct(env.stat().st_mode)[-3:] == "600", oct(env.stat().st_mode)
        finally:
            cfgmod.ENV_PATH = orig


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
