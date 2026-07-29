"""
Tests for the doctor pre-flight.

The estimator is the part with money attached, so it gets most of the
coverage: it must mirror dub._tts_cached's cache rule exactly, or the
"will this batch finish?" answer is wrong in the expensive direction.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import doctor  # noqa: E402


def _project(tmp: Path, passages, cached=()):
    """Build a fake scratch dir: zh_script.json plus cached takes for the
    anchor ids in `cached` (sidecar text matching, as dub._tts_cached needs)."""
    scratch = tmp / "runs" / "proj"
    (scratch / "passages").mkdir(parents=True, exist_ok=True)
    (scratch / "zh_script.json").write_text(
        json.dumps({"passages": passages}, ensure_ascii=False), encoding="utf-8")
    for aid in cached:
        text = "".join(
            b["text"] for p in passages if p["anchor_id"] == aid for b in p["beats"])
        (scratch / "passages" / f"{aid}_raw.mp3").write_bytes(b"fake")
        (scratch / "passages" / f"{aid}_raw.texts.json").write_text(
            json.dumps([text], ensure_ascii=False), encoding="utf-8")
    return scratch


def _patch_scratch(scratch: Path):
    from cn_pipeline import paths
    paths.run_scratch_dir = lambda pid: scratch  # noqa: ARG005


PASSAGES = [
    {"anchor_id": "a01", "beats": [{"text": "一二三", "en_seg": 0}]},          # 3 chars
    {"anchor_id": "a02", "beats": [{"text": "四五", "en_seg": 1},
                                   {"text": "六七", "en_seg": 2}]},            # 4 chars
    {"anchor_id": "a03", "beats": [{"text": "八九十", "en_seg": 3}]},          # 3 chars
]


def test_estimate_counts_all_when_nothing_cached():
    with tempfile.TemporaryDirectory() as td:
        _patch_scratch(_project(Path(td), PASSAGES))
        est = doctor.estimate_tts_characters("proj")
        assert est["known"] is True
        assert est["passages"] == 3
        assert est["total_chars"] == 10, est
        assert est["billable_chars"] == 10, est
        assert est["pending"] == ["a01", "a02", "a03"], est


def test_estimate_skips_cached_passages():
    with tempfile.TemporaryDirectory() as td:
        _patch_scratch(_project(Path(td), PASSAGES, cached=("a01", "a03")))
        est = doctor.estimate_tts_characters("proj")
        # only a02's 4 chars are still billable
        assert est["billable_chars"] == 4, est
        assert est["pending"] == ["a02"], est


def test_estimate_treats_edited_text_as_billable():
    """The whole point of the sidecar check: audio exists but the script
    changed, so it WILL be re-bought. Counting it as free is the failure
    mode that strands a batch mid-run."""
    with tempfile.TemporaryDirectory() as td:
        scratch = _project(Path(td), PASSAGES, cached=("a01",))
        # simulate a tightening edit to a01 after its take was generated
        data = json.loads((scratch / "zh_script.json").read_text(encoding="utf-8"))
        data["passages"][0]["beats"][0]["text"] = "一二三四五"
        (scratch / "zh_script.json").write_text(json.dumps(data, ensure_ascii=False),
                                                encoding="utf-8")
        _patch_scratch(scratch)
        est = doctor.estimate_tts_characters("proj")
        assert "a01" in est["pending"], est
        assert est["billable_chars"] == 5 + 4 + 3, est


def test_estimate_unknown_without_script():
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "runs" / "proj"
        scratch.mkdir(parents=True)
        _patch_scratch(scratch)
        est = doctor.estimate_tts_characters("proj")
        assert est["known"] is False
        assert "zh_script" in est["reason"]


def test_check_project_fails_when_estimate_exceeds_balance():
    """The check that would have caught the mid-batch quota death."""
    with tempfile.TemporaryDirectory() as td:
        _patch_scratch(_project(Path(td), PASSAGES))
        from cn_pipeline import paths
        paths.resolve_project_dir = lambda pid: Path(td)  # noqa: ARG005
        paths.find_master_video = lambda d: Path(td) / "m.mp4"  # noqa: ARG005
        paths.me_wav_path = lambda d: Path(td) / "m_me.wav"  # noqa: ARG005

        checks = doctor.check_project("proj", el_remaining=5)
        est = [c for c in checks if c.name == "tts cost estimate"][0]
        assert est.status == doctor.FAIL, est.as_dict()
        assert "short by 5" in est.detail, est.detail

        checks = doctor.check_project("proj", el_remaining=50_000)
        est = [c for c in checks if c.name == "tts cost estimate"][0]
        assert est.status == doctor.PASS, est.as_dict()


def test_format_report_marks_failures_and_shows_hints():
    checks = [doctor.Check("a", doctor.PASS, "fine"),
              doctor.Check("b", doctor.FAIL, "broken", "do the thing")]
    out = doctor.format_report(checks)
    assert "FAIL" in out
    assert "do the thing" in out
    assert "1 FAIL" in out


def test_format_report_all_pass():
    out = doctor.format_report([doctor.Check("a", doctor.PASS, "fine")])
    assert "All checks passed" in out


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
