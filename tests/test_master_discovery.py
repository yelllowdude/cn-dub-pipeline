"""
Master + M&E discovery against the filenames editors and staff actually
produce, rather than the ones the convention asks for.

Real folders that broke the old rules: `video_2025_01_25.mov`,
`how-often-...-calisthenics_final.mov`, `6-pull-up-tips-banana-back_v3.mov`
(note "pull-up" vs the folder's "pullup"), and an M&E named
"Build strong functional arms_01_ME.wav".
"""

import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import paths  # noqa: E402
from cn_pipeline.paths import ProjectNotFoundError  # noqa: E402


def _proj(td: str, name: str, files: list[str]) -> Path:
    d = Path(td) / name
    d.mkdir(parents=True)
    for f in files:
        p = d / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return d


# --- master: the conventional case keeps working -----------------------------------

def test_prefixed_mp4_still_wins():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["proj_2025-01-01_video.mp4"])
        assert paths.find_master_video(d).name == "proj_2025-01-01_video.mp4"


def test_prefix_beats_a_non_conforming_sibling():
    """A conforming name is authoritative even when other videos sit beside it."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["proj_2025-01-01_video.mp4", "old_render.mov", "bts.mov"])
        assert paths.find_master_video(d).name == "proj_2025-01-01_video.mp4"


def test_newest_wins_among_conforming_duplicates():
    import os, time  # noqa: E401
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["proj_2025-01-01_video.mp4", "proj_2025-01-01_video_2.mp4"])
        old = time.time() - 5000
        os.utime(d / "proj_2025-01-01_video.mp4", (old, old))
        assert paths.find_master_video(d).name == "proj_2025-01-01_video_2.mp4"


# --- master: the cases that were failing -------------------------------------------

def test_a_lone_mov_is_the_master_even_unprefixed():
    """`video_2025_01_25.mov` in winter-mornings_2025-01-20 -- one obvious
    master, previously reported as "no master video found"."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "winter-mornings_2025-01-20", ["video_2025_01_25.mov"])
        assert paths.find_master_video(d).name == "video_2025_01_25.mov"


def test_near_miss_project_name_still_resolves():
    """"6-pull-up-tips-banana-back_v3.mov" vs folder "6-pullup-tips-...":
    one hyphen apart, so the prefix rule can never match it."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "6-pullup-tips-banana-back_2025-01-21",
                  ["6-pull-up-tips-banana-back_v3.mov"])
        assert paths.find_master_video(d).name == "6-pull-up-tips-banana-back_v3.mov"


def test_subfolder_videos_are_ignored():
    """shorts/ and longform/ hold other cuts; only the root counts."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["final.mov", "shorts/clip.mp4", "longform/other.mov", "CN/x_cndub.mp4"])
        assert paths.find_master_video(d).name == "final.mov"


def test_rendered_outputs_are_never_mistaken_for_the_master():
    """A re-run must not pick up its own previous render sitting in the root."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["source.mov", "proj_2025-01-01_cndub.mp4", "proj_2025-01-01_ensub.mp4"])
        assert paths.find_master_video(d).name == "source.mov"


def test_ambiguous_non_conforming_candidates_error_with_the_list():
    """Two plausible masters and no convention to break the tie is a human's
    call -- guessing would silently localize the wrong cut."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["v1_final.mov", "v2_final.mov"])
        try:
            paths.find_master_video(d)
            raise AssertionError("expected ProjectNotFoundError")
        except ProjectNotFoundError as e:
            assert "v1_final.mov" in str(e) and "v2_final.mov" in str(e)
            assert "proj_2025-01-01" in str(e)


def test_no_video_at_all_names_both_extensions():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["storyboard.afpub"])
        try:
            paths.find_master_video(d)
            raise AssertionError("expected ProjectNotFoundError")
        except ProjectNotFoundError as e:
            assert ".mp4" in str(e) and ".mov" in str(e)


# --- M&E bed -----------------------------------------------------------------------

def test_canonical_me_name_wins():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["proj_2025-01-01_me.wav", "something_else_ME.wav"])
        assert paths.me_wav_path(d).name == "proj_2025-01-01_me.wav"


def test_stale_suffix_me_bed_is_found():
    """how-often-...-calisthenics_2025-01-05_ME.wav in a folder whose name ends
    _video -- the bed exists, the name is one token off."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "how-often_2025-01-05_video", ["how-often_2025-01-05_ME.wav"])
        assert paths.me_wav_path(d).name == "how-often_2025-01-05_ME.wav"


def test_human_titled_me_bed_is_found():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "build-strong-functional-arms_2025-01-05_video",
                  ["Build strong functional arms_01_ME.wav"])
        assert paths.me_wav_path(d).name == "Build strong functional arms_01_ME.wav"


def test_missing_bed_returns_the_canonical_path():
    """Callers test .exists() on the result, so the no-bed path must be
    unchanged: mix-me no-ops and the dub ships VO-only."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["proj_2025-01-01_video.mp4"])
        p = paths.me_wav_path(d)
        assert p.name == "proj_2025-01-01_me.wav"
        assert not p.exists()


def test_two_candidate_beds_are_not_guessed_between():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["a_ME.wav", "b_me.wav"])
        p = paths.me_wav_path(d)
        assert p.name == "proj_2025-01-01_me.wav"
        assert not p.exists(), "ambiguity must read as 'no bed', not a coin flip"


def test_unrelated_wavs_are_not_treated_as_beds():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["voiceover.wav", "music_bed.wav"])
        assert not paths.me_wav_path(d).exists()


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
