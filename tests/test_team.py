"""
Credential handoff between machines. The team authenticates as shared service
accounts, so the shared half can just be handed over -- but a few values must
never travel, and one of them (.machine_id) would silently DISABLE the claim
lock rather than merely weaken it. Most of these tests are about what stays
behind.
"""

import json
import os
import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import team  # noqa: E402

FULL_ENV = {
    "ELEVENLABS_API_KEY": "el-key",
    "KIE_API_KEY": "kie-key",
    "FRAMEIO_CLIENT_ID": "fio-id",
    "FRAMEIO_CLIENT_SECRET": "fio-secret",
    "FRAMEIO_REFRESH_TOKEN": "fio-refresh",
    "FRAMEIO_SHARE_PASSPHRASE": "share-pass",
    "YOUTUBE_CLIENT_ID": "yt-id",
    "YOUTUBE_CLIENT_SECRET": "yt-secret",
    "YOUTUBE_REFRESH_TOKEN": "yt-refresh",
    # must not travel
    "GDRIVE_REFRESH_TOKEN": "gd-refresh",
    "GDRIVE_CLIENT_ID": "gd-id",
    "FRAMEIO_TOKEN": "stale-24h-token",
}

FULL_CONFIG = {
    "storage": "mount",
    "frameio_account_id": "acct-1",
    "frameio_project_id": "proj-1",
    "frameio_redirect_uri": "https://localhost/redirect/",
    # must not travel
    "drive_root": "/Users/someone/Library/CloudStorage/GoogleDrive-x/Shared drives/General",
    "operator": "producer",
    "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
    "mirror_dir": "/somewhere/mirror",
}


# --- what travels ----------------------------------------------------------------

def test_shared_service_credentials_are_exported():
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    for k in ("ELEVENLABS_API_KEY", "KIE_API_KEY", "FRAMEIO_REFRESH_TOKEN",
              "FRAMEIO_SHARE_PASSPHRASE", "YOUTUBE_REFRESH_TOKEN"):
        assert b["credentials"][k] == FULL_ENV[k], k


def test_team_wide_config_is_exported():
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    assert b["config"]["frameio_project_id"] == "proj-1"
    assert b["config"]["storage"] == "mount"


def test_absent_keys_are_simply_omitted():
    b = team.build_bundle({"ELEVENLABS_API_KEY": "el"}, {})
    assert list(b["credentials"]) == ["ELEVENLABS_API_KEY"]
    assert b["config"] == {}


def test_export_from_an_unconfigured_machine_is_refused():
    try:
        team.build_bundle({}, {})
        raise AssertionError("expected TeamBundleError")
    except team.TeamBundleError as e:
        assert "doctor" in str(e)


# --- what must NOT travel ---------------------------------------------------------

def test_per_person_google_tokens_never_travel():
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    for k in ("GDRIVE_REFRESH_TOKEN", "GDRIVE_CLIENT_ID"):
        assert k not in b["credentials"], f"{k} is per-person"


def test_stale_pasted_frameio_token_never_travels():
    """FRAMEIO_TOKEN is a hand-pasted ~24h token -- expired before it lands."""
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    assert "FRAMEIO_TOKEN" not in b["credentials"]


def test_machine_local_config_never_travels():
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    for k in ("drive_root", "operator", "ffmpeg_path", "mirror_dir"):
        assert k not in b["config"], f"{k} is machine-local"


def test_machine_id_is_not_a_thing_the_bundle_knows_about():
    """The load-bearing one: two machines sharing a machine id are the SAME
    machine to claim_verdict, so both take the claim re-entrantly and work the
    same project in silence. Copying it disables the lock, not weakens it. It
    must not appear anywhere in the exported payload."""
    b = team.build_bundle(dict(FULL_ENV, **{".machine_id": "abc123"}),
                          dict(FULL_CONFIG))
    blob = json.dumps(b)
    assert "machine_id" not in b.get("credentials", {})
    assert "abc123" not in blob, "a machine id leaked into the bundle"
    # and it is called out as excluded, so a human reading the export knows
    assert "machine_id" in b["excluded"]["note"]


def test_excluded_list_names_what_was_left_behind():
    b = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
    assert "GDRIVE_REFRESH_TOKEN" in b["excluded"]["credentials"]
    assert "drive_root" in b["excluded"]["config"]


# --- file handling ----------------------------------------------------------------

def test_bundle_file_is_private():
    """It holds live keys and a refresh token."""
    with tempfile.TemporaryDirectory() as td:
        p = team.write_bundle(team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG)),
                              Path(td) / "b.json")
        assert oct(p.stat().st_mode)[-3:] == "600", oct(p.stat().st_mode)
        leftovers = [f.name for f in Path(td).iterdir() if f.name != "b.json"]
        assert leftovers == [], leftovers


def test_round_trip_through_a_file():
    with tempfile.TemporaryDirectory() as td:
        src = team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))
        p = team.write_bundle(src, Path(td) / "b.json")
        assert team.load_bundle(p) == src


def test_a_wrong_file_is_rejected_clearly():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "notabundle.json"
        p.write_text('{"hello": "world"}', encoding="utf-8")
        try:
            team.load_bundle(p)
            raise AssertionError("expected TeamBundleError")
        except team.TeamBundleError as e:
            assert "team export" in str(e)


def test_a_future_bundle_version_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.json"
        p.write_text(json.dumps({"bundle_version": 99, "credentials": {"A": "b"}}),
                     encoding="utf-8")
        try:
            team.load_bundle(p)
            raise AssertionError("expected TeamBundleError")
        except team.TeamBundleError as e:
            assert "Re-export" in str(e)


# --- import planning ---------------------------------------------------------------

def _bundle():
    return team.build_bundle(dict(FULL_ENV), dict(FULL_CONFIG))


def test_import_onto_a_blank_machine_adds_everything():
    plan = team.plan_import(_bundle(), {}, {}, overwrite=False)
    assert "FRAMEIO_REFRESH_TOKEN" in plan["env_writes"]
    assert plan["config_writes"]["frameio_project_id"] == "proj-1"
    assert plan["env_conflict"] == {}


def test_import_keeps_a_teammates_own_credential_by_default():
    """Someone who already authenticated a service must not silently lose it."""
    plan = team.plan_import(_bundle(), {"FRAMEIO_REFRESH_TOKEN": "their-own"}, {},
                            overwrite=False)
    assert "FRAMEIO_REFRESH_TOKEN" in plan["env_conflict"]
    assert "FRAMEIO_REFRESH_TOKEN" not in plan["env_writes"]


def test_overwrite_replaces_a_differing_credential():
    plan = team.plan_import(_bundle(), {"FRAMEIO_REFRESH_TOKEN": "their-own"}, {},
                            overwrite=True)
    assert plan["env_writes"]["FRAMEIO_REFRESH_TOKEN"] == "fio-refresh"


def test_identical_values_are_neither_conflict_nor_write():
    plan = team.plan_import(_bundle(), {"ELEVENLABS_API_KEY": "el-key"}, {},
                            overwrite=False)
    assert "ELEVENLABS_API_KEY" in plan["env_same"]
    assert "ELEVENLABS_API_KEY" not in plan["env_writes"]
    assert "ELEVENLABS_API_KEY" not in plan["env_conflict"]


def test_import_refuses_machine_local_keys_even_if_a_bundle_carries_them():
    """Defence in depth: a hand-edited or older bundle must not be able to
    overwrite drive_root or a per-person Google token."""
    hostile = {"bundle_version": team.BUNDLE_VERSION,
               "credentials": {"GDRIVE_REFRESH_TOKEN": "someone-elses"},
               "config": {"drive_root": "/wrong/path", "operator": "someone-else"}}
    plan = team.plan_import(hostile, {}, {}, overwrite=True)
    assert "GDRIVE_REFRESH_TOKEN" in plan["refused"]
    assert "drive_root" in plan["refused"]
    assert "operator" in plan["refused"]


def test_machine_local_config_is_preserved_across_a_real_import():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.json"
        cfg_path.write_text(json.dumps(
            {"drive_root": "/my/mount", "operator": "editor"}), encoding="utf-8")
        env_written = {}
        plan = team.plan_import(_bundle(),
                                {}, json.loads(cfg_path.read_text()), overwrite=False)
        team.apply_import(plan, lambda k, v: env_written.__setitem__(k, v), cfg_path)

        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert after["drive_root"] == "/my/mount", "clobbered a machine-local path"
        assert after["operator"] == "editor", "clobbered the claim label"
        assert after["frameio_project_id"] == "proj-1"
        assert env_written["FRAMEIO_REFRESH_TOKEN"] == "fio-refresh"


def test_apply_leaves_no_temp_files():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.json"
        plan = team.plan_import(_bundle(), {}, {}, overwrite=False)
        team.apply_import(plan, lambda k, v: None, cfg_path)
        assert sorted(f.name for f in Path(td).iterdir()) == ["config.json"]


def test_env_reader_ignores_comments_and_blanks():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".env"
        p.write_text("# a comment\n\nA=1\nB = 2 \nnotakeyvalue\n", encoding="utf-8")
        assert team._read_env(p) == {"A": "1", "B": "2"}


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
