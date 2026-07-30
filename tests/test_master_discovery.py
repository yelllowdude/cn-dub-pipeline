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


# --- master: filed in a delivery subfolder instead of the root ----------------------
# Real folders: `leg-gain-3-mistakes_2025-02-17/video/leg-gain-3-mistakes_2025-02-17.mp4`
# and `cardio-cali-gains-jump-ropes_2025-02-04/longform/video.mov`.

def test_master_in_a_video_subfolder_is_found():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "leg-gain_2025-02-17", ["video/leg-gain_2025-02-17.mp4"])
        found = paths.find_master_video(d)
        assert found.name == "leg-gain_2025-02-17.mp4"
        assert found.parent.name == "video"


def test_unprefixed_master_in_a_longform_subfolder_is_found():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "cardio_2025-02-04", ["longform/video.mov"])
        assert paths.find_master_video(d).name == "video.mov"


def test_root_master_beats_a_subfolder_one():
    """The root is authoritative; subfolders are a fallback, not a merge."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["proj_2025-01-01_video.mp4", "video/old_cut.mov"])
        assert paths.find_master_video(d).name == "proj_2025-01-01_video.mp4"


def test_staging_broll_is_never_adopted_as_the_master():
    """`staging/` holds screen recordings and source clips. With the real master
    in longform/, the staging clip must not even be a candidate."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "cardio_2025-02-04",
                  ["longform/video.mov", "staging/crossrope-app-screen-record.MP4"])
        assert paths.find_master_video(d).name == "video.mov"


def test_broll_only_project_still_reports_no_master():
    """No delivery subfolder at all -> unchanged loud failure, not a b-roll clip."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["staging/clip.mp4", "multi-images/anim.mov"])
        try:
            paths.find_master_video(d)
        except ProjectNotFoundError as e:
            assert "No master video" in str(e)
        else:
            raise AssertionError("a staging clip was adopted as the master")


def test_ambiguous_subfolder_masters_raise_with_the_list():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01", ["video/cut_a.mov", "video/cut_b.mov"])
        try:
            paths.find_master_video(d)
        except ProjectNotFoundError as e:
            assert "cut_a.mov" in str(e) and "cut_b.mov" in str(e)
        else:
            raise AssertionError("two non-conforming cuts must not be guessed between")


def test_master_in_a_nested_delivery_subfolder_is_found():
    """Real layout: `Videos/Final/P5_..._Final-4K.mp4` -- two levels down, and the
    filename does not start with the project id either."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "late-night_2025-05-22",
                  ["Videos/Final/P5_late-night_2025-05-22_Final-4K.mp4"])
        assert paths.find_master_video(d).name == "P5_late-night_2025-05-22_Final-4K.mp4"


def test_a_drafts_sibling_is_not_descended_into():
    """`Videos/Final/` is allowlisted at both levels; `Videos/Drafts/` is not, so
    the draft (a sponsorship-only segment in the real project) never competes."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "late-night_2025-05-22",
                  ["Videos/Final/real_final_cut.mp4",
                   "Videos/Drafts/late-night_2025-05-22_Draft1-SponsorshipVideoPart.mp4"])
        assert paths.find_master_video(d).name == "real_final_cut.mp4"


def test_delivery_subfolder_name_with_a_space_is_found():
    """Real layout: `Final Video/{id}_4K.mp4`, alongside a `Staging/` folder."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "your-legs_2025-05-25",
                  ["Final Video/your-legs_2025-05-25_4K.mp4", "Staging/bts.mp4"])
        assert paths.find_master_video(d).name == "your-legs_2025-05-25_4K.mp4"


def test_descent_does_not_run_away():
    """A deeply buried video is not adopted -- depth is bounded even when every
    level happens to be an allowlisted name."""
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["video/videos/final/exports/masters/buried.mp4"])
        try:
            paths.find_master_video(d)
        except ProjectNotFoundError:
            pass
        else:
            raise AssertionError("descent should be depth-bounded")


def test_rendered_output_in_a_subfolder_is_excluded():
    with tempfile.TemporaryDirectory() as td:
        d = _proj(td, "proj_2025-01-01",
                  ["video/proj_2025-01-01.mp4", "video/proj_2025-01-01_cndub.mp4"])
        assert paths.find_master_video(d).name == "proj_2025-01-01.mp4"


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
