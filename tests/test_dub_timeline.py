"""dub.chunk_timeline: leading silence + inter-chunk gaps from the original srt.

Regression guard for the max-strength_2026-03-12 desync: inter-chunk gaps were
dropped on assembly and dumped as trailing silence, sliding the dub forward
against the picture. chunk_timeline must recover the exact lead + per-chunk
gaps so finalize and align-dub can place audio and subtitles on one timeline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import install, run_module

install()

from cn_pipeline import dub


def _fmt(ms):
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _seg(a, b):
    return {"time": f"{_fmt(a)} --> {_fmt(b)}", "text": "x"}


def test_lead_and_inter_chunk_gap():
    # chunk 1: 15 cues from 2000ms (lead=2000); last cue ends at 2000+14*500+400=9400
    c1 = [_seg(2000 + i * 500, 2000 + i * 500 + 400) for i in range(15)]
    # chunk 2: starts 13000ms -> inter-chunk gap 13000-9400=3600
    c2 = [_seg(13000 + i * 500, 13000 + i * 500 + 400) for i in range(5)]
    tl = dub.chunk_timeline(c1 + c2, 15)
    assert tl["lead_ms"] == 2000
    assert tl["gaps"] == [0, 3600], tl["gaps"]


def test_single_chunk_has_lead_no_gaps():
    c1 = [_seg(2000 + i * 500, 2000 + i * 500 + 400) for i in range(5)]
    tl = dub.chunk_timeline(c1, 15)
    assert tl["gaps"] == [0] and tl["lead_ms"] == 2000


def test_zero_lead():
    c0 = [_seg(i * 500, i * 500 + 400) for i in range(3)]
    assert dub.chunk_timeline(c0, 15)["lead_ms"] == 0


if __name__ == "__main__":
    run_module(dict(globals()))
