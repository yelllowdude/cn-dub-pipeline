"""
M&E gain selection. The bed arrives two ways -- staff-prepped at full level, or
separated out of the master by Demucs and noticeably quieter -- and one gain
cannot serve both: whichever value you pick is wrong for the other case.
"""

import json
import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import config as cfgmod  # noqa: E402
from cn_pipeline import dub  # noqa: E402
from cn_pipeline.cli import _me_source  # noqa: E402


class FakeCfg:
    me_gain_db = -4.0
    me_gain_db_generated = 2.0


def test_provided_bed_is_attenuated():
    """Staff-prepped comes in at full level and must sit UNDER the voice."""
    assert dub.gain_for_source(FakeCfg(), "provided") == -4.0


def test_generated_bed_is_boosted():
    """Demucs separation comes back quiet; the same attenuation would bury it."""
    assert dub.gain_for_source(FakeCfg(), "generated") == 2.0


def test_the_two_cases_actually_differ():
    cfg = FakeCfg()
    assert dub.gain_for_source(cfg, "provided") != dub.gain_for_source(cfg, "generated")


def test_unknown_source_falls_back_to_attenuating():
    """The safe direction: boosting a full-level bed buries the voice, so an
    unrecognised value must never resolve to the boost."""
    assert dub.gain_for_source(FakeCfg(), "") == -4.0
    assert dub.gain_for_source(FakeCfg(), "something-else") == -4.0


# --- provenance from project.json -----------------------------------------------

def test_absent_project_json_means_provided():
    with tempfile.TemporaryDirectory() as td:
        assert _me_source(Path(td)) == "provided"


def test_recorded_source_is_read_back():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "project.json").write_text(
            json.dumps({"dub_mode": "native", "me_source": "generated"}), encoding="utf-8")
        assert _me_source(Path(td)) == "generated"


def test_project_json_without_the_key_means_provided():
    """Every project predating this setting: unchanged behaviour."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "project.json").write_text(
            json.dumps({"dub_mode": "native"}), encoding="utf-8")
        assert _me_source(Path(td)) == "provided"


def test_corrupt_project_json_does_not_crash_the_mix():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "project.json").write_text("{broken", encoding="utf-8")
        assert _me_source(Path(td)) == "provided"


# --- both gains are repo-versioned ------------------------------------------------

def test_both_gains_ship_with_the_repo():
    """The whole point of the fix: neither case is a per-machine config edit."""
    defaults = cfgmod.load_output_defaults()
    assert "me_gain_db" in defaults
    assert "me_gain_db_generated" in defaults
    assert defaults["me_gain_db_generated"] > defaults["me_gain_db"], \
        "the separated bed should be boosted relative to the staff-prepped one"


def test_generated_gain_override_is_surfaced_like_the_other():
    overrides = cfgmod.output_setting_overrides({"me_gain_db_generated": 9.0})
    assert [k for k, _, _ in overrides] == ["me_gain_db_generated"], overrides


def test_comment_keys_in_defaults_are_not_treated_as_settings():
    """pipeline_defaults.json documents itself inline; those keys must not read
    as overrides or as missing settings."""
    defaults = cfgmod.load_output_defaults()
    assert not any(k.startswith("_") for k in defaults), defaults.keys()


# --- doctor's report -------------------------------------------------------------

def test_doctor_me_gain_check_runs():
    """Regression: check_me_gain referenced get_config() without importing it,
    and nothing exercised the function, so the suite stayed green while
    `doctor --project-id` crashed with NameError."""
    import wave

    from cn_pipeline import doctor

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "probe_me.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x10" * 8000)

        real = cfgmod.get_config
        cfgmod.get_config = lambda: FakeCfg()
        try:
            checks = doctor.check_me_gain("no-such-project_2026-01-01", wav)
        finally:
            cfgmod.get_config = real

    assert len(checks) == 1
    assert checks[0].name == "M&E bed"
    # unrecorded provenance -> provided -> the attenuating gain
    assert "-4" in checks[0].detail, checks[0]


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
