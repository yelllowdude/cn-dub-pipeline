"""Pure-logic tests for native dub mode: anchor validation/windows and the
Chinese cue splitter. No TTS, no ffmpeg, no audio -- same discipline as the
other tests (see tests/README.md). Run: python tests/test_native_mode.py"""

import _bootstrap
_bootstrap.install()

from cn_pipeline.anchors import MIN_WINDOW_MS, validate_anchors, windows
from cn_pipeline.subtitles import ZH_MAX_CHARS, split_zh_cues

passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  ok: {name}")


# --- anchors: validation ---
good = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0, "note": "open"},
    {"id": "a02", "ms": 40000, "note": "product shot"},
    {"id": "a03", "ms": 90000, "note": "outro", "lead_ms": 300},
]}
check("valid anchors pass", validate_anchors(good) == [])

bad_order = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0}, {"id": "a02", "ms": 50000}, {"id": "a03", "ms": 30000}]}
check("non-monotonic rejected", any("strictly increasing" in e for e in validate_anchors(bad_order)))

bad_first = {"video_ms": 120000, "anchors": [{"id": "a01", "ms": 5000}]}
check("first anchor must be 0", any("must be at ms=0" in e for e in validate_anchors(bad_first)))

tiny = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0}, {"id": "a02", "ms": MIN_WINDOW_MS - 1}]}
check("short window rejected", any("minimum" in e for e in validate_anchors(tiny)))

dup = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0}, {"id": "a01", "ms": 60000}]}
check("duplicate id rejected", any("duplicate" in e for e in validate_anchors(dup)))

past_end = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0}, {"id": "a02", "ms": 130000}]}
check("anchor past video end rejected", any("past the end" in e for e in validate_anchors(past_end)))

# en_seg_range partition check
ranged = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0, "en_seg_range": [0, 9]},
    {"id": "a02", "ms": 60000, "en_seg_range": [10, 19]},
]}
check("clean seg partition passes", validate_anchors(ranged, n_segments=20) == [])
gappy = {"video_ms": 120000, "anchors": [
    {"id": "a01", "ms": 0, "en_seg_range": [0, 9]},
    {"id": "a02", "ms": 60000, "en_seg_range": [12, 19]},
]}
check("seg-range gap rejected", any("partition" in e for e in validate_anchors(gappy, n_segments=20)))

# --- anchors: windows ---
wins = windows(good)
check("one window per anchor", len(wins) == 3)
check("window chain is contiguous",
      wins[0]["end_ms"] == wins[1]["start_ms"] and wins[1]["end_ms"] == wins[2]["start_ms"])
check("last window ends at video end", wins[-1]["end_ms"] == 120000)
check("lead_ms carried through", wins[2]["lead_ms"] == 300)

# --- split_zh_cues ---
cues = split_zh_cues("你的饮食不健康。不是因为你选错了!真的吗?")
check("splits at sentence enders", cues == ["你的饮食不健康。", "不是因为你选错了!", "真的吗?"])

long_sentence = "这是一个特别长的句子,它有很多很多的逗号,而且每一段都不短,所以必须在逗号处被拆开才能放进字幕行。"
cues = split_zh_cues(long_sentence)
check("long sentence soft-breaks", len(cues) >= 2)
check("no cue is grossly over the line limit", all(len(c) <= ZH_MAX_CHARS + 10 for c in cues))
check("nothing lost in the split", "".join(cues) == long_sentence)

cues = split_zh_cues("短句。尾巴,对。")
check("tiny trailing fragment joins previous", all(len(c) >= 2 for c in cues))

check("unterminated final sentence kept", split_zh_cues("没有句号的结尾")[-1] == "没有句号的结尾")
check("empty passage -> no cues", split_zh_cues("  ") == [])

print(f"\nall {passed} checks passed")

# --- build_cndub_ass with empty English lines (Chinese-only native subs) ---
import tempfile, os
from cn_pipeline.render import build_cndub_ass

srt_body = "\n".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n第{i}句中文字幕。\n\n" for i in range(1, 6))
with tempfile.TemporaryDirectory() as td:
    srt_p = os.path.join(td, "in.srt"); ass_p = os.path.join(td, "out.ass")
    open(srt_p, "w", encoding="utf-8").write(srt_body)
    from pathlib import Path
    build_cndub_ass(Path(srt_p), Path(ass_p), 1920, 1080)
    dialogues = [l for l in open(ass_p, encoding="utf-8") if l.startswith("Dialogue")]
    check("empty-EN srt keeps every cue in the .ass", len(dialogues) == 5)
    check("no stray {\\fs} for the missing EN line", all("\\fs" not in d.split(",,")[-1] or "\\N" not in d for d in dialogues))

print(f"\nall {passed} checks passed (with ass)")
