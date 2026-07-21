"""
Native-mode sync anchors: the timing contract for dub-first localization.

In native dub mode (see dub_native.py) the Chinese VO is NOT timed against the
English cue grid -- it is timed against a short list of visual sync points
("anchors"): product shots, on-screen-text moments, chapter turns. Anchors
partition the video into windows; each window gets one natural spoken-Chinese
passage, and between anchors nothing constrains internal pacing. That freedom
is the whole point of the mode, which is why anchor SELECTION is a judgment
call: `detect` only produces candidates (scene cuts + speech gaps), and the
operator writes the final anchors.json by hand -- a scene cut mid-sentence is
a terrible anchor, and only someone who has watched the video knows which cuts
must sync.

anchors.json schema (operator-written, `validate` checks it):
{
  "video_ms": 623173,
  "anchors": [
    {"id": "a01", "ms": 0,     "note": "cold open"},
    {"id": "a02", "ms": 34160, "note": "cut to pyramid graphic",
     "en_seg_range": [9, 27],  # optional: the English segments this window covers
     "lead_ms": 300}           # optional: delay speech onset past the anchor
  ]
}
Window i runs anchors[i].ms -> anchors[i+1].ms (the last window ends at
video_ms). Windows are hard constraints because the master video is fixed.
"""

import json
import re
import subprocess
from pathlib import Path

# Candidate generation knobs. These only shape the SUGGESTION list the
# operator picks from -- they are not sync rules.
SCENE_CUT_THRESHOLD = 0.30       # ffmpeg scene-change score
SPEECH_GAP_MIN_MS = 1200         # an English VO pause this long marks a paragraph break
MIN_WINDOW_MS = 5000             # validate: no window shorter than this
ANCHOR_DENSITY_HINT_S = (30, 60)  # advisory only, printed by detect


def detect_scene_cuts(ffmpeg_path: str, video_path: Path) -> list[int]:
    """Scene-change timestamps (ms) via ffmpeg's select filter. Mechanical
    candidates only -- see module docstring for why these are never auto-picked."""
    result = subprocess.run(
        [ffmpeg_path, "-i", str(video_path),
         "-vf", f"select='gt(scene,{SCENE_CUT_THRESHOLD})',metadata=print",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    cuts = []
    for m in re.finditer(r"pts_time:(\d+\.?\d*)", result.stderr):
        cuts.append(round(float(m.group(1)) * 1000))
    return sorted(set(cuts))


def detect_speech_gaps(segments: list[dict]) -> list[dict]:
    """Inter-segment silences >= SPEECH_GAP_MIN_MS in the English VO --
    natural paragraph boundaries, usually the best anchor candidates."""
    from cn_pipeline.subtitles import parse_srt_time
    gaps = []
    prev_end = None
    for i, seg in enumerate(segments):
        start_s, end_s = seg["time"].split(" --> ")
        start, end = parse_srt_time(start_s), parse_srt_time(end_s)
        if prev_end is not None and start - prev_end >= SPEECH_GAP_MIN_MS:
            gaps.append({"after_seg": i - 1, "gap_ms": start - prev_end, "at_ms": prev_end})
        prev_end = end
    return gaps


def load_anchors(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_anchors(data)
    if errors:
        raise ValueError(f"{path} is not a valid anchors file:\n  " + "\n  ".join(errors))
    return data


def validate_anchors(data: dict, n_segments: int | None = None) -> list[str]:
    """Returns a list of human-readable problems (empty = valid)."""
    errors = []
    video_ms = data.get("video_ms")
    anchors = data.get("anchors")
    if not isinstance(video_ms, int) or video_ms <= 0:
        errors.append("video_ms must be a positive integer (probe the master)")
    if not isinstance(anchors, list) or not anchors:
        return errors + ["anchors must be a non-empty list"]

    seen_ids = set()
    prev_ms = None
    for i, a in enumerate(anchors):
        aid = a.get("id") or f"<anchor {i}>"
        if a.get("id") in seen_ids:
            errors.append(f"{aid}: duplicate id")
        seen_ids.add(a.get("id"))
        ms = a.get("ms")
        if not isinstance(ms, int) or ms < 0:
            errors.append(f"{aid}: ms must be a non-negative integer")
            continue
        if isinstance(video_ms, int) and ms >= video_ms:
            errors.append(f"{aid}: ms {ms} is past the end of the video ({video_ms})")
        if prev_ms is not None and ms <= prev_ms:
            errors.append(f"{aid}: anchors must be strictly increasing (got {ms} after {prev_ms})")
        if prev_ms is not None and ms - prev_ms < MIN_WINDOW_MS:
            errors.append(f"{aid}: window before it is {ms - prev_ms}ms, minimum is {MIN_WINDOW_MS}ms")
        lead = a.get("lead_ms", 0)
        if not isinstance(lead, int) or lead < 0:
            errors.append(f"{aid}: lead_ms must be a non-negative integer")
        prev_ms = ms

    if anchors and anchors[0].get("ms") != 0:
        errors.append("first anchor must be at ms=0 (the video start is always a window boundary)")
    if isinstance(video_ms, int) and prev_ms is not None and video_ms - prev_ms < MIN_WINDOW_MS:
        errors.append(f"last window is {video_ms - prev_ms}ms, minimum is {MIN_WINDOW_MS}ms")

    # en_seg_range, when present on all anchors, must partition the segments
    ranges = [a.get("en_seg_range") for a in anchors]
    if n_segments is not None and all(isinstance(r, list) and len(r) == 2 for r in ranges):
        expect = 0
        for a, r in zip(anchors, ranges):
            if r[0] != expect:
                errors.append(f"{a.get('id')}: en_seg_range starts at {r[0]}, expected {expect} "
                              "(ranges must partition segments with no gaps/overlaps)")
            expect = r[1] + 1
        if expect != n_segments:
            errors.append(f"en_seg_ranges end at {expect - 1}, but there are {n_segments} segments")
    return errors


def windows(data: dict) -> list[dict]:
    """[{anchor_id, start_ms, end_ms, lead_ms}] -- window i is anchor i to
    anchor i+1 (last one to video end)."""
    out = []
    anchors = data["anchors"]
    for i, a in enumerate(anchors):
        end = anchors[i + 1]["ms"] if i + 1 < len(anchors) else data["video_ms"]
        out.append({"anchor_id": a["id"], "start_ms": a["ms"], "end_ms": end,
                    "lead_ms": a.get("lead_ms", 0)})
    return out
