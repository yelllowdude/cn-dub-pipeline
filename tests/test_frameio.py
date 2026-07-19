"""Frame.io review logic: cue parsing, comment->cue resolution, replacement
extraction, classification, report split, and the SAFE per-cue auto-apply."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import install, run_module

install()

from cn_pipeline import frameio as fio

SRT = """1
00:00:01,000 --> 00:00:03,000
你的力量马上要 go bananas 了
Your strength is about to go bananas

2
00:00:03,500 --> 00:00:06,000
大腿后侧发力
Drive through your hamstrings
"""


def _cues():
    p = Path(tempfile.mktemp(suffix=".srt"))
    p.write_text(SRT, encoding="utf-8")
    return fio.parse_cndub_cues(p)


def test_cue_parse_and_zh_index_linkage():
    cues = _cues()
    assert len(cues) == 2
    assert cues[0]["zh_index"] == 0 and cues[1]["zh_index"] == 1
    assert cues[0]["start_ms"] == 1000 and cues[1]["end_ms"] == 6000


def test_resolve_inside_and_gap_snap():
    cues = _cues()
    assert fio.resolve_comment_to_cue(2000, cues)["idx"] == 1
    assert fio.resolve_comment_to_cue(5000, cues)["idx"] == 2
    assert fio.resolve_comment_to_cue(3400, cues)["idx"] == 2  # in gap -> nearest boundary


def test_extract_replacement_forms():
    assert fio.extract_replacement("腘绳肌 → 大腿后侧") == ("腘绳肌", "大腿后侧")
    assert fio.extract_replacement("这里把「黄头黄」改成「光头黄」") == ("黄头黄", "光头黄")
    assert fio.extract_replacement('"hamstring" should be "大腿后侧"') == ("hamstring", "大腿后侧")
    assert fio.extract_replacement("翻译得不太自然，可以更口语一点") is None


def test_classification():
    assert fio.classify_comment("腘绳肌 → 大腿后侧")["category"] == "term"
    assert fio.classify_comment("字幕太慢了，对不上口型")["category"] == "timing"
    assert fio.classify_comment("这里有个错别字")["category"] == "typo"
    assert fio.classify_comment("这句听起来怪怪的")["category"] == "unclear"


def _report():
    cues = [
        {"idx": 1, "zh_index": 0, "start_ms": 0, "end_ms": 3000, "zh": "用你的腘绳肌发力", "en": "x"},
        {"idx": 2, "zh_index": 1, "start_ms": 3000, "end_ms": 6000, "zh": "保持核心收紧", "en": "y"},
    ]
    comments = [
        {"id": "c1", "text": "腘绳肌 → 大腿后侧", "timestamp_ms": 1500, "author": "native"},   # old present -> auto
        {"id": "c2", "text": "节奏太快了", "timestamp_ms": 4000, "author": "native"},          # timing -> human
        {"id": "c3", "text": "应该是 大腿后侧", "timestamp_ms": 4200, "author": "native"},      # no old side -> human
    ]
    return fio.build_review_report(comments, cues)


def test_report_auto_vs_human():
    rep = _report()
    assert rep["auto_count"] == 1 and rep["human_count"] == 2
    assert rep["auto_fixable"][0]["cue_idx"] == 1 and rep["auto_fixable"][0]["zh_index"] == 0


def test_apply_is_per_cue_never_blanket():
    rep = _report()
    zh = ["用你的腘绳肌发力", "腘绳肌也在别处"]   # term appears in BOTH lines
    new_zh, changelog = fio.apply_auto_fixes(rep, zh)
    assert new_zh[0] == "用你的大腿后侧发力"        # fixed only in the resolved cue
    assert new_zh[1] == "腘绳肌也在别处"            # untouched elsewhere
    applied = [c for c in changelog if c["status"] == "applied"]
    assert len(applied) == 1 and applied[0]["old"] == "腘绳肌"


def test_normalize_timestamp_forms():
    assert fio._normalize_comment({"id": "a", "text": "x", "frame": 48, "fps": 24})["timestamp_ms"] == 2000
    assert fio._normalize_comment({"id": "b", "text": "y", "timestamp": 3.5})["timestamp_ms"] == 3500


if __name__ == "__main__":
    run_module(dict(globals()))
