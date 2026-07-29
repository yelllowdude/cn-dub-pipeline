"""
gdrive storage-layer logic: master selection parity with find_master_video,
the scratch sync filter, and claim arbitration. Pure -- no network, no config;
the DriveClient/API surface is exercised against the live account per
docs/VALIDATE.md, not here.
"""

import calendar
import time

import _bootstrap
_bootstrap.install()

from cn_pipeline.gdrive import (ClaimError, claim_verdict, make_claim,  # noqa: E402
                                pick_master, scratch_syncable, touch_claim)

FOLDER_MIME = "application/vnd.google-apps.folder"


def _f(name, modified="2026-01-01T00:00:00Z", mime="video/mp4"):
    return {"id": f"id-{name}", "name": name, "modifiedTime": modified, "mimeType": mime}


def test_pick_master_prefers_newest_matching():
    entries = [
        _f("proj-a_2026-01-01-video.mp4", "2026-01-01T00:00:00Z"),
        _f("proj-a_2026-01-01-video_2.mp4", "2026-03-01T00:00:00Z"),
        _f("unrelated.mp4", "2026-06-01T00:00:00Z"),   # doesn't share the project prefix
        _f("proj-a_2026-01-01_me.wav"),                # not an mp4
        _f("CN", mime=FOLDER_MIME),                    # folders never match
    ]
    got = pick_master(entries, "proj-a_2026-01-01")
    assert got["name"] == "proj-a_2026-01-01-video_2.mp4", got


def test_pick_master_none_when_no_candidates():
    assert pick_master([_f("CN", mime=FOLDER_MIME)], "proj-a") is None
    assert pick_master([], "proj-a") is None


def test_scratch_filter_keeps_paid_and_state_drops_regenerable():
    kept = ["segments.json", "zh.json", "project.json", "zh_script.json",
            "frameio_review.json", "api_spend.json", "finalize_log.json",
            "chunks/chunk_01_raw.mp3", "chunks/chunk_01.mp3", "review_report.md"]
    dropped = ["audio_16k.wav", "dub_master_final.wav", "dub_master_padded.wav",
               "dub_master_mixed.wav", "align_chunks/align_01.wav",
               "align_passages/a01.wav", "screentext/master_localized.mp4",
               "render_cndub.log", "__pycache__/x.pyc"]
    for rel in kept:
        assert scratch_syncable(rel), f"should sync: {rel}"
    for rel in dropped:
        assert not scratch_syncable(rel), f"should NOT sync: {rel}"


ME = {"operator": "alice", "host": "alices-mac"}
# An ACTIVE claim by someone else. It has to carry a live heartbeat: a claim
# whose last_seen is a month old is abandoned, and abandoned claims are
# takeable (see the staleness tests below).
OTHER = dict(make_claim({"operator": "bob", "host": "bobs-mac"}))


def test_claim_fresh_when_absent_or_released():
    assert claim_verdict(None, ME, steal=False) == "fresh"
    released = {"claimed": False, "operator": "bob", "host": "bobs-mac"}
    assert claim_verdict(released, ME, steal=False) == "fresh"


def test_claim_reentrant_for_same_operator_and_host():
    mine = make_claim(ME)
    assert claim_verdict(mine, ME, steal=False) == "mine"
    # same person, different machine is NOT re-entrant -- the scratch state
    # (TTS cache, spend counter) is per-machine, so this is a real handoff
    other_host = dict(mine, host="alices-laptop")
    try:
        claim_verdict(other_host, ME, steal=False)
        assert False, "expected ClaimError"
    except ClaimError:
        pass


def test_claim_refuses_other_operator_unless_steal():
    try:
        claim_verdict(OTHER, ME, steal=False)
        assert False, "expected ClaimError"
    except ClaimError as e:
        assert "bob@bobs-mac" in str(e)
    assert claim_verdict(OTHER, ME, steal=True) == "stolen"


def test_make_claim_shape():
    c = make_claim(ME)
    assert c["claimed"] is True and c["operator"] == "alice" and c["claimed_at"]
    assert c["last_seen"], "a claim needs a heartbeat or it can never go stale"


# --- staleness: a crashed run must not lock a project forever -------------------

def test_abandoned_claim_is_takeable_without_steal():
    stale = dict(OTHER, last_seen="2026-07-01T00:00:00Z")
    now = calendar.timegm(time.strptime("2026-07-28T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    assert claim_verdict(stale, ME, steal=False, now_epoch=now) == "stale"


def test_claim_just_under_the_threshold_still_refuses():
    """The boundary matters: a colleague mid-render must not be evicted."""
    base = calendar.timegm(time.strptime("2026-07-28T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    fresh_enough = dict(OTHER, last_seen="2026-07-27T12:30:00Z")  # 11.5h
    try:
        claim_verdict(fresh_enough, ME, steal=False, now_epoch=base)
        raise AssertionError("expected ClaimError 30min before the stale threshold")
    except ClaimError:
        pass


def test_heartbeat_keeps_a_long_run_alive():
    """claimed_at ages but last_seen doesn't: a 20-hour render stays claimed."""
    old = {"claimed": True, "operator": "bob", "host": "bobs-mac",
           "claimed_at": "2026-07-27T00:00:00Z", "last_seen": "2026-07-27T23:00:00Z"}
    now = calendar.timegm(time.strptime("2026-07-28T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    try:
        claim_verdict(old, ME, steal=False, now_epoch=now)
        raise AssertionError("expected ClaimError -- holder was active an hour ago")
    except ClaimError:
        pass


def test_touch_preserves_claimed_at_and_owner():
    c = make_claim(ME)
    t = touch_claim(dict(c, claimed_at="2026-07-01T00:00:00Z"), ME)
    assert t["claimed_at"] == "2026-07-01T00:00:00Z"
    assert t["operator"] == "alice" and t["claimed"] is True


def test_undated_claim_is_never_silently_stolen():
    """A claim with no parsable timestamp has unknown age -- refuse, don't
    assume it's abandoned."""
    undated = {"claimed": True, "operator": "bob", "host": "bobs-mac"}
    try:
        claim_verdict(undated, ME, steal=False)
        raise AssertionError("expected ClaimError for an undated claim")
    except ClaimError:
        pass


if __name__ == "__main__":
    _bootstrap.run_module(globals())
