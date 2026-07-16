"""
Subtitle assembly: split a raw Whisper SRT into cue-sized fragments,
then build the bilingual (CN+EN, same cue) and mono-language SRT files.

Consolidated from split_cues.py / build_bilingual_srt.py / build_mono_srt.py,
which were duplicated per-project with only a BASE path differing.
"""

import json
import re
from pathlib import Path

MAX_CHARS = 84  # ~2 lines * 42 chars, matches cn_workflow.html's cue-length rule
MIN_MS = 1000
MAX_MS = 6000


def parse_srt_time(t: str) -> int:
    h, m, s = t.split(":")
    s, ms = s.split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def fmt_srt_time(ms: int) -> str:
    ms = max(0, round(ms))
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_text(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    fragments = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= MAX_CHARS:
            fragments.append(p)
        else:
            subparts = re.split(r"(?<=,)\s+", p)
            buf = ""
            for sp in subparts:
                if buf and len(buf) + 1 + len(sp) > MAX_CHARS:
                    fragments.append(buf.strip())
                    buf = sp
                else:
                    buf = (buf + " " + sp).strip()
            if buf:
                fragments.append(buf.strip())
    return fragments


def split_cues(whisper_srt_path: Path) -> list[dict]:
    """Parse a raw Whisper SRT into cue-sized {time, text} fragments,
    proportionally splitting timing by character count within each
    original Whisper segment."""
    raw = Path(whisper_srt_path).read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.strip().split("\n\n") if b.strip()]
    segs = []
    for b in blocks:
        lines = b.split("\n")
        time_line = lines[1]
        text = " ".join(lines[2:]).strip()
        start, end = time_line.split(" --> ")
        segs.append((parse_srt_time(start), parse_srt_time(end), text))

    cues = []
    for start_ms, end_ms, text in segs:
        fragments = _split_text(text)
        if not fragments:
            continue
        total_chars = sum(len(f) for f in fragments)
        duration = end_ms - start_ms
        cur = start_ms
        for i, frag in enumerate(fragments):
            if i == len(fragments) - 1:
                frag_end = end_ms
            else:
                share = len(frag) / total_chars if total_chars else 1 / len(fragments)
                frag_dur = round(duration * share)
                frag_end = min(end_ms, cur + frag_dur)
            if frag_end <= cur:
                frag_end = cur + 1
            cues.append({"start": cur, "end": frag_end, "text": frag})
            cur = frag_end

    merged = []
    for c in cues:
        if merged and (c["end"] - c["start"]) < 500:
            merged[-1]["end"] = c["end"]
            merged[-1]["text"] += " " + c["text"]
        else:
            merged.append(c)

    return [
        {"time": f"{fmt_srt_time(c['start'])} --> {fmt_srt_time(c['end'])}", "text": c["text"]}
        for c in merged
    ]


def build_bilingual_srt(segments: list[dict], zh_lines: list[str], out_path: Path) -> None:
    """CN first line, EN second line, same cue -- for burning onto video."""
    assert len(segments) == len(zh_lines), (
        f"segments ({len(segments)}) and zh translation ({len(zh_lines)}) count mismatch"
    )
    lines = []
    for i, (s, z) in enumerate(zip(segments, zh_lines), 1):
        lines += [str(i), s["time"], z, s["text"], ""]
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


def build_mono_srt(segments: list[dict], zh_lines: list[str], en_out: Path, zh_out: Path) -> None:
    assert len(segments) == len(zh_lines)
    en_lines, zh_out_lines = [], []
    for i, (s, z) in enumerate(zip(segments, zh_lines), 1):
        en_lines += [str(i), s["time"], s["text"], ""]
        zh_out_lines += [str(i), s["time"], z, ""]
    Path(en_out).write_text("\n".join(en_lines), encoding="utf-8")
    Path(zh_out).write_text("\n".join(zh_out_lines), encoding="utf-8")


def load_segments(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_segments(segments: list[dict], path: Path) -> None:
    Path(path).write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
