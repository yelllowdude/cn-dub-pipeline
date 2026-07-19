"""In-screen text detection logic: IoU, event clustering + stability flag,
gap-bridging, brief-event filtering, and padded-box clamping."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import install, run_module

install()

from cn_pipeline import screentext as st

W, H = 1920, 1080


def _det(text, box, conf=0.9):
    return {"text": text, "box": box, "conf": conf}


def test_iou():
    assert st._iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert st._iou([0, 0, 10, 10], [100, 100, 10, 10]) == 0.0
    assert 0.1 < st._iou([0, 0, 10, 10], [5, 0, 10, 10]) < 0.5


def test_clustering_stable_vs_moving():
    frames = []
    for i, t in enumerate([0, 500, 1000, 1500, 2000]):
        frames.append({"t_ms": t, "dets": [
            _det("DEATH BY SQUATS", [100, 900, 600, 80]),      # fixed position
            _det("PULLING", [100 + i * 60, 100, 200, 60]),     # 60px/frame drift, boxes still overlap
        ]})
    events = {e["text"]: e for e in st.cluster_events(frames, W, H)}
    assert events["DEATH BY SQUATS"]["stable"] is True
    assert events["DEATH BY SQUATS"]["start_ms"] == 0 and events["DEATH BY SQUATS"]["end_ms"] == 2000
    assert events["PULLING"]["stable"] is False
    assert events["PULLING"]["drift_frac"] > st.STABLE_DRIFT_FRAC


def test_gap_bridged_single_dropout():
    frames = [
        {"t_ms": 0, "dets": [_det("REST 60s", [50, 50, 300, 70])]},
        {"t_ms": 500, "dets": []},                                    # one-sample OCR dropout
        {"t_ms": 1000, "dets": [_det("REST 60s", [50, 50, 300, 70])]},
    ]
    ev = st.cluster_events(frames, W, H)
    assert len(ev) == 1 and ev[0]["end_ms"] == 1000


def test_filter_drops_brief_events():
    frames = [
        {"t_ms": 0, "dets": [_det("HOLD", [10, 10, 100, 40])]},
        {"t_ms": 500, "dets": [_det("HOLD", [10, 10, 100, 40])]},
        {"t_ms": 900, "dets": [_det("FLASH", [500, 500, 80, 30])]},   # single sample -> 0ms span
    ]
    kept = {e["text"] for e in st.filter_events(st.cluster_events(frames, W, H), min_event_ms=500)}
    assert "HOLD" in kept and "FLASH" not in kept


def test_padded_box_clamps_to_frame():
    x, y, w, h = st._padded_box([10, 10, 100, 50], W, H)
    assert x >= 0 and y >= 0 and x + w <= W and y + h <= H
    x, y, w, h = st._padded_box([1900, 1060, 100, 50], W, H)
    assert x + w <= W and y + h <= H


if __name__ == "__main__":
    run_module(dict(globals()))
