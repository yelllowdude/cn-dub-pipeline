"""Stage-gate staleness (SKIP_OK / --force) and the per-run spend guard."""

import os
import sys
import time
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import install, run_module

install()

from cn_pipeline.cli import _stage_gate
from cn_pipeline.spend import record_call, SpendCapError


class _Args:
    force = False


def test_gate_runs_when_output_missing():
    tmp = Path(tempfile.mkdtemp())
    inp, out = tmp / "in.txt", tmp / "out.txt"
    inp.write_text("x")
    assert _stage_gate(_Args(), [out], [inp]) is True


def test_gate_skips_when_fresh():
    tmp = Path(tempfile.mkdtemp())
    inp, out = tmp / "in.txt", tmp / "out.txt"
    inp.write_text("x")
    out.write_text("y")
    assert _stage_gate(_Args(), [out], [inp]) is False


def test_gate_reruns_when_input_newer():
    tmp = Path(tempfile.mkdtemp())
    inp, out = tmp / "in.txt", tmp / "out.txt"
    inp.write_text("x")
    out.write_text("y")
    time.sleep(0.02)
    inp.write_text("x2")
    assert _stage_gate(_Args(), [out], [inp]) is True


def test_force_overrides_freshness():
    tmp = Path(tempfile.mkdtemp())
    inp, out = tmp / "in.txt", tmp / "out.txt"
    inp.write_text("x")
    out.write_text("y")
    a = _Args()
    a.force = True
    assert _stage_gate(a, [out], [inp]) is True


def test_spend_cap_enforced_and_not_over_recorded():
    tmp = Path(tempfile.mkdtemp())
    assert record_call(tmp, "tts", 2) == 1
    assert record_call(tmp, "tts", 2) == 2
    try:
        record_call(tmp, "tts", 2)
        raise AssertionError("cap not enforced")
    except SpendCapError as e:
        assert "max_tts_calls_per_run" in str(e)
    assert json.loads((tmp / "api_spend.json").read_text())["tts"] == 2


def test_spend_counter_thread_safe():
    tmp = Path(tempfile.mkdtemp())
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda _: record_call(tmp, "tts", 100), range(50)))
    assert json.loads((tmp / "api_spend.json").read_text())["tts"] == 50


if __name__ == "__main__":
    run_module(dict(globals()))
