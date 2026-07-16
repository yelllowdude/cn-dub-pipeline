"""
Whisper transcription (Stage 1, English) and forced-alignment of the
Chinese dub audio back onto its cue text (Stage 4).

Runs in-process against the pipeline's own venv -- no separate
whisper-venv. Settings pinned to match what was validated by hand:
model="small", language="en" for the initial transcript, word_timestamps=True
for alignment. See README for the one-time equivalence check against a
known-good project before trusting this on a new one.
"""

import difflib
import subprocess
from pathlib import Path

import whisper

from cn_pipeline.config import get_config
from cn_pipeline.subtitles import fmt_srt_time, parse_srt_time

_model_cache = {}


def _get_model(name: str):
    if name not in _model_cache:
        _model_cache[name] = whisper.load_model(name)
    return _model_cache[name]


def transcribe_to_srt(audio_path: Path, out_srt_path: Path, language: str = "en") -> None:
    """Stage 1: raw English transcription of the source video's audio.

    temperature=0.0 pins greedy decoding so this is reproducible run-to-run
    (Whisper's default temperature fallback samples at higher temperatures
    when a segment is low-confidence, which makes output non-deterministic
    otherwise). Segment boundaries can still land differently than a past
    ad-hoc run near quiet/ambiguous audio -- e.g. a trailing outro with
    sparse speech -- that's expected, not a bug; see dub.tighten()'s
    sanity-check warning for exactly this case.
    """
    cfg = get_config()
    model = _get_model(cfg.whisper_model)
    result = model.transcribe(str(audio_path), language=language, word_timestamps=False, temperature=0.0)

    lines = []
    for i, seg in enumerate(result["segments"], 1):
        start = fmt_srt_time(seg["start"] * 1000)
        end = fmt_srt_time(seg["end"] * 1000)
        lines += [str(i), f"{start} --> {end}", seg["text"].strip(), ""]
    Path(out_srt_path).write_text("\n".join(lines), encoding="utf-8")


def extract_audio_16k(video_or_audio_path: Path, out_wav_path: Path) -> None:
    cfg = get_config()
    subprocess.run(
        [cfg.ffmpeg_path, "-y", "-i", str(video_or_audio_path), "-vn", "-ac", "1", "-ar", "16000", str(out_wav_path)],
        capture_output=True,
        check=True,
    )


def force_align_chunk(
    chunk_audio_path: Path,
    zh_lines: list[str],
    en_lines: list[str],
    chunk_abs_start_ms: int,
    chunk_dur_ms: int,
) -> list[tuple]:
    """Align one dub-audio chunk's actual TTS speech back onto its cue text.

    Returns a list of (abs_start_ms, abs_end_ms, zh_text, en_text) tuples,
    one per cue in this chunk, with timestamps in the full track's absolute
    timeline (chunk_abs_start_ms + this chunk's relative offset).
    """
    cfg = get_config()
    model = _get_model(cfg.whisper_model)
    result = model.transcribe(str(chunk_audio_path), language="Chinese", word_timestamps=True, temperature=0.0)

    asr_chars = []
    for seg in result["segments"]:
        for w in seg.get("words", []):
            word_text = w["word"].strip()
            if not word_text:
                continue
            w_start_ms = w["start"] * 1000
            w_end_ms = w["end"] * 1000
            n = len(word_text)
            for ci, ch in enumerate(word_text):
                t = w_start_ms + (w_end_ms - w_start_ms) * (ci / max(n, 1))
                asr_chars.append((ch, t))
    asr_text = "".join(c for c, _ in asr_chars)

    ref_chars = []
    for line in zh_lines:
        ref_chars.extend(list(line))
    ref_text = "".join(ref_chars)

    sm = difflib.SequenceMatcher(None, ref_text, asr_text, autojunk=False)
    blocks = sm.get_matching_blocks()

    cue_start_ref_pos = []
    pos = 0
    for line in zh_lines:
        cue_start_ref_pos.append(pos)
        pos += len(line)

    def ref_pos_to_time(ref_pos):
        best = None
        for b in blocks:
            if b.size == 0:
                continue
            if b.a <= ref_pos < b.a + b.size:
                offset = ref_pos - b.a
                asr_idx = b.b + offset
                if asr_idx < len(asr_chars):
                    return asr_chars[asr_idx][1]
                elif asr_chars:
                    return asr_chars[-1][1]
            if b.a <= ref_pos:
                best = b
        if best is not None:
            asr_idx = min(best.b + best.size - 1, len(asr_chars) - 1)
            if asr_idx >= 0:
                return asr_chars[asr_idx][1]
        return 0.0

    cue_times_ms = [ref_pos_to_time(p) for p in cue_start_ref_pos]
    for i in range(1, len(cue_times_ms)):
        if cue_times_ms[i] < cue_times_ms[i - 1]:
            cue_times_ms[i] = cue_times_ms[i - 1]

    out = []
    for i, (zh_line, en_line) in enumerate(zip(zh_lines, en_lines)):
        rel_start = cue_times_ms[i]
        rel_end = cue_times_ms[i + 1] if i + 1 < len(cue_times_ms) else chunk_dur_ms
        if rel_end <= rel_start:
            rel_end = rel_start + 300
        out.append((chunk_abs_start_ms + rel_start, chunk_abs_start_ms + rel_end, zh_line, en_line))
    return out


def write_aligned_srt(cues: list[tuple], out_path: Path) -> None:
    """cues: list of (start_ms, end_ms, zh_text, en_text), already globally
    monotonic-clamped (see clamp_monotonic)."""
    lines = []
    for i, (a, b, zh_t, en_t) in enumerate(cues, 1):
        lines += [str(i), f"{fmt_srt_time(a)} --> {fmt_srt_time(b)}", zh_t, en_t, ""]
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


def clamp_monotonic(cues: list[tuple]) -> tuple[list[tuple], int]:
    """Global safety pass across chunk boundaries -- the per-chunk alignment
    in force_align_chunk() already clamps within a chunk, but a chunk-boundary
    overlap can still slip through. Returns (clamped_cues, overlaps_fixed)."""
    cues = [list(c) for c in cues]
    overlaps = 0
    for i in range(1, len(cues)):
        if cues[i][0] < cues[i - 1][1]:
            overlaps += 1
            cues[i][0] = cues[i - 1][1]
        if cues[i][1] <= cues[i][0]:
            cues[i][1] = cues[i][0] + 300
    return [tuple(c) for c in cues], overlaps
