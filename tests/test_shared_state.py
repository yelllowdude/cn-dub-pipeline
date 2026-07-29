"""
Mount-mode shared state: the claim lock and the shared scratch sync that
protect two operators from double-spending TTS or forking the Frame.io review
stack. Pure filesystem -- no network, no config.

The claim POLICY is tested in test_gdrive.py (both modes share it). What's
tested here is the transport: that it round-trips on disk, that it survives a
corrupt file, and that the sync filter keeps the paid state while dropping the
gigabyte intermediates.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import shared_state  # noqa: E402
from cn_pipeline.gdrive import make_claim  # noqa: E402

# `host` is an opaque stable machine id (see gdrive.machine_id); `hostname` is
# the human-readable label shown in claim messages.
ALICE = {"operator": "alice", "host": "a1b2c3d4e5f6", "hostname": "alices-mac"}
BOB = {"operator": "bob", "host": "9f8e7d6c5b4a", "hostname": "bobs-mac"}


def _project(td: str) -> Path:
    d = Path(td) / "proj-a_2026-01-01"
    (d / "CN").mkdir(parents=True)
    return d


# --- claim round-trip -----------------------------------------------------------

def test_claim_release_round_trip():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        assert shared_state.status(proj) is None
        assert shared_state.claim(proj, ALICE) == "fresh"

        rec = shared_state.status(proj)
        assert rec["claimed"] is True and rec["operator"] == "alice"
        assert shared_state.claim(proj, ALICE) == "mine"

        shared_state.release(proj, ALICE)
        assert shared_state.status(proj)["claimed"] is False
        # released -> the next operator walks in clean
        assert shared_state.claim(proj, BOB) == "fresh"


def test_second_operator_is_refused():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        try:
            shared_state.claim(proj, BOB)
            raise AssertionError("expected ClaimError -- this is the double-spend race")
        except shared_state.ClaimError as e:
            assert "alices-mac" in str(e)
        assert shared_state.claim(proj, BOB, steal=True) == "stolen"


def test_claim_survives_a_changed_hostname():
    """The bug this guards: host used to be socket.gethostname(), which on
    macOS can be the DHCP address. A lease change made you a stranger to your
    own claim. Re-entrancy keys on the stable machine id, not the label."""
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        moved_desks = dict(ALICE, hostname="192.168.1.99")
        assert shared_state.claim(proj, moved_desks) == "mine"


def test_claim_file_lands_in_the_shared_pipeline_folder():
    """It must be under CN/ -- that's the directory Drive for Desktop syncs, so
    it's the only place the other machine will ever see it."""
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        assert shared_state.claim_path(proj) == proj / "CN" / "_pipeline" / "claim.json"
        assert shared_state.claim_path(proj).is_file()


def test_corrupt_claim_does_not_wedge_the_project():
    """A half-synced claim.json must not make every command fail forever."""
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        shared_state.claim_path(proj).write_text("{not json", encoding="utf-8")
        assert shared_state.status(proj) is None
        assert shared_state.claim(proj, BOB) == "fresh"
        assert json.loads(shared_state.claim_path(proj).read_text())["operator"] == "bob"


def test_atomic_write_leaves_no_temp_files():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        shared_state.release(proj, ALICE)
        leftovers = [p.name for p in shared_state.pipeline_dir(proj).iterdir()
                     if p.name != "claim.json"]
        assert leftovers == [], leftovers


# --- heartbeat -------------------------------------------------------------------

def test_heartbeat_refreshes_only_the_holder():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.claim(proj, ALICE)
        stale_stamp = "2026-07-01T00:00:00Z"
        rec = dict(shared_state.status(proj), last_seen=stale_stamp)
        shared_state.claim_path(proj).write_text(json.dumps(rec), encoding="utf-8")

        # a non-holder's heartbeat must be a no-op, not a silent takeover
        shared_state.heartbeat(proj, BOB)
        after = shared_state.status(proj)
        assert after["operator"] == "alice" and after["last_seen"] == stale_stamp

        shared_state.heartbeat(proj, ALICE)
        assert shared_state.status(proj)["last_seen"] != stale_stamp


def test_heartbeat_on_unclaimed_project_claims_nothing():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        shared_state.heartbeat(proj, ALICE)
        assert shared_state.status(proj) is None


# --- scratch sync ----------------------------------------------------------------

def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_sync_out_publishes_paid_state_and_drops_intermediates():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        scratch = Path(td) / "runs" / "proj-a"
        for rel in ["segments.json", "anchors.json", "zh.json", "zh_script.json",
                    "project.json", "api_spend.json", "frameio_review.json",
                    "chunks/01_raw.mp3"]:
            _write(scratch / rel)
        for rel in ["dub_master_mixed.wav", "audio_16k.wav",
                    "align_chunks/align_01.wav", "render_cndub.log"]:
            _write(scratch / rel)

        published = shared_state.sync_scratch_out(scratch, proj)
        shared = shared_state.shared_scratch_dir(proj)

        # the expensive-to-replace half goes up
        for rel in ["segments.json", "anchors.json", "zh.json", "zh_script.json",
                    "api_spend.json", "frameio_review.json", "chunks/01_raw.mp3"]:
            assert (shared / rel).is_file(), f"should have published {rel}"
        # the regenerable gigabytes do not
        for rel in ["dub_master_mixed.wav", "audio_16k.wav",
                    "align_chunks/align_01.wav", "render_cndub.log"]:
            assert not (shared / rel).exists(), f"should NOT have published {rel}"
        assert "segments.json" in published


def test_sync_in_restores_a_colleagues_tts_cache():
    """The whole point: inherit the paid chunks instead of re-buying them."""
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        alice_scratch = Path(td) / "runs" / "alice"
        _write(alice_scratch / "chunks/01_raw.mp3", "paid-audio")
        _write(alice_scratch / "frameio_review.json", '{"stack": "v1"}')
        shared_state.sync_scratch_out(alice_scratch, proj)

        bob_scratch = Path(td) / "runs" / "bob"
        restored = shared_state.sync_scratch_in(bob_scratch, proj)
        assert (bob_scratch / "chunks/01_raw.mp3").read_text() == "paid-audio"
        assert (bob_scratch / "frameio_review.json").read_text() == '{"stack": "v1"}'
        assert sorted(restored) == ["chunks/01_raw.mp3", "frameio_review.json"]


def test_sync_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        scratch = Path(td) / "runs" / "proj-a"
        _write(scratch / "segments.json")
        assert shared_state.sync_scratch_out(scratch, proj) == ["segments.json"]
        assert shared_state.sync_scratch_out(scratch, proj) == []


def test_sync_out_overwrites_an_older_shared_copy():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        scratch = Path(td) / "runs" / "proj-a"
        _write(scratch / "zh.json", "old")
        shared_state.sync_scratch_out(scratch, proj)

        _write(scratch / "zh.json", "new-and-longer")
        old = time.time() - 600
        os.utime(shared_state.shared_scratch_dir(proj) / "zh.json", (old, old))
        assert shared_state.sync_scratch_out(scratch, proj) == ["zh.json"]
        assert (shared_state.shared_scratch_dir(proj) / "zh.json").read_text() == "new-and-longer"


def test_sync_in_on_a_project_nobody_has_shared_yet():
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        assert shared_state.sync_scratch_in(Path(td) / "runs" / "proj-a", proj) == []


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
