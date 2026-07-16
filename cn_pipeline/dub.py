"""
Chinese dub generation: chunked TTS, tempo-fit-to-target, the mandatory
overflow-chunk gate (re-split once, then force-correct unconditionally),
and the last-chunk / trailing-gap silence-pad exception.

Consolidated from generate_dub.py, fix_dub.py, tighten_dub.py, finalize_dub.py
(previously 4-5 near-identical copies per project, differing only by a BASE
path and a hand-edited PROBLEM_CHUNKS list). PROBLEM_CHUNKS and observed-duration
data now live in a per-run dub_overrides.json (see paths.run_scratch_dir),
not in code.

Rules this encodes (see docs/cn_workflow.html Stage 4 for the authoritative
version -- re-read it, don't rely on this docstring, if a threshold changes):
  - Generate in CHUNK_SIZE-line chunks, one continuous TTS take per chunk,
    then a single uniform tempo stretch to fit the chunk's target window.
  - A chunk that hits the tempo cap gets re-split ONCE into SUB_CHUNK_SIZE-line
    sub-chunks and regenerated; cap this at one re-split attempt.
  - After the (at most one) re-split, apply a final corrective tempo pass
    UNCONDITIONALLY on every rebuilt chunk, even if it only drifted a few ms.
  - The LAST chunk in the video is exempt from force-stretching: if it
    undershoots because of a real trailing pause/outro, pad with silence
    instead of stretching speech to fill dead air.
"""

import json
import subprocess
import time
from pathlib import Path

import requests
from pydub import AudioSegment

from cn_pipeline.config import get_config
from cn_pipeline.subtitles import parse_srt_time

VOICE_ID = "MI36FIkp9wRP7cpWKPTl"  # Evan Zhao
MODEL_ID = "eleven_v3"

CHUNK_SIZE = 15
SUB_CHUNK_SIZE = 5
GEN_ATEMPO_MIN, GEN_ATEMPO_MAX = 0.85, 1.4
FIX_ATEMPO_MIN, FIX_ATEMPO_MAX = 0.85, 1.45


def _chunk_segments(segments: list[dict], chunk_size: int) -> list[dict]:
    chunks = []
    for i in range(0, len(segments), chunk_size):
        s = segments[i:i + chunk_size]
        start_ms = parse_srt_time(s[0]["time"].split(" --> ")[0])
        end_ms = parse_srt_time(s[-1]["time"].split(" --> ")[1])
        chunks.append({
            "idx": len(chunks) + 1,
            "seg_start": i,
            "seg_end": i + len(s),
            "target_ms": end_ms - start_ms,
        })
    return chunks


def _tts_call(api_key: str, texts: list[str]) -> bytes:
    combined = "\n".join(texts)
    last_err = None
    for _attempt in range(3):
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": combined,
                "model_id": MODEL_ID,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=90,
        )
        if resp.status_code == 200:
            return resp.content
        last_err = f"{resp.status_code}: {resp.text[:200]}"
        time.sleep(3)
    raise RuntimeError(f"ElevenLabs TTS failed after 3 attempts: {last_err}")


def _apply_atempo(ffmpeg_path: str, in_path: Path, out_path: Path, atempo: float) -> None:
    subprocess.run(
        [ffmpeg_path, "-y", "-i", str(in_path), "-filter:a", f"atempo={atempo:.4f}", str(out_path)],
        capture_output=True,
        check=True,
    )


def generate(segments: list[dict], zh_lines: list[str], scratch_dir: Path) -> dict:
    """Stage 4a: generate the initial chunked dub. Returns a log dict with
    per-chunk target/actual/ratio, and which chunks hit the tempo cap
    (['capped_chunks']) and therefore need fix_overflow_chunks()."""
    cfg = get_config()
    chunks_dir = scratch_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    chunks = _chunk_segments(segments, CHUNK_SIZE)
    log = {"chunks": [], "capped_chunks": []}

    for c in chunks:
        texts = zh_lines[c["seg_start"]:c["seg_end"]]
        raw_path = chunks_dir / f"{c['idx']:02d}_raw.mp3"
        stretched_path = chunks_dir / f"{c['idx']:02d}_stretched.wav"

        if not raw_path.exists():
            raw_path.write_bytes(_tts_call(cfg.elevenlabs_api_key, texts))

        raw_clip = AudioSegment.from_mp3(raw_path)
        actual_ms = len(raw_clip)
        target_ms = c["target_ms"]
        ratio = actual_ms / target_ms if target_ms else 1.0
        atempo = max(GEN_ATEMPO_MIN, min(GEN_ATEMPO_MAX, ratio))

        _apply_atempo(cfg.ffmpeg_path, raw_path, stretched_path, atempo)
        stretched_ms = len(AudioSegment.from_wav(stretched_path))

        capped = ratio > GEN_ATEMPO_MAX or ratio < GEN_ATEMPO_MIN
        log["chunks"].append({
            "idx": c["idx"], "target_ms": target_ms, "actual_ms": actual_ms,
            "ratio": round(ratio, 4), "atempo": round(atempo, 4),
            "stretched_ms": stretched_ms, "capped": capped,
        })
        if capped:
            log["capped_chunks"].append(c["idx"])

    (scratch_dir / "generate_log.json").write_text(json.dumps(log, indent=2))
    return log


def fix_overflow_chunks(segments: list[dict], zh_lines: list[str], scratch_dir: Path, capped_chunks: list[int]) -> dict:
    """Stage 4b: re-split each capped chunk into SUB_CHUNK_SIZE-line
    sub-chunks and regenerate. Cap at one re-split attempt -- do not loop."""
    cfg = get_config()
    chunks_dir = scratch_dir / "chunks"
    fix_dir = scratch_dir / "chunks_fix"
    fix_dir.mkdir(exist_ok=True)

    chunks = _chunk_segments(segments, CHUNK_SIZE)
    by_idx = {c["idx"]: c for c in chunks}
    log = {"rebuilt": []}

    for idx in capped_chunks:
        c = by_idx[idx]
        seg_slice = segments[c["seg_start"]:c["seg_end"]]
        zh_slice = zh_lines[c["seg_start"]:c["seg_end"]]

        sub_chunks = []
        for j in range(0, len(seg_slice), SUB_CHUNK_SIZE):
            sub_segs = seg_slice[j:j + SUB_CHUNK_SIZE]
            sub_zh = zh_slice[j:j + SUB_CHUNK_SIZE]
            s_start = parse_srt_time(sub_segs[0]["time"].split(" --> ")[0])
            s_end = parse_srt_time(sub_segs[-1]["time"].split(" --> ")[1])
            sub_chunks.append({"texts": sub_zh, "target_ms": s_end - s_start})

        rebuilt = AudioSegment.empty()
        for k, sc in enumerate(sub_chunks, 1):
            raw_path = fix_dir / f"{idx:02d}_{k:02d}_raw.mp3"
            stretched_path = fix_dir / f"{idx:02d}_{k:02d}_stretched.wav"
            if not raw_path.exists():
                raw_path.write_bytes(_tts_call(cfg.elevenlabs_api_key, sc["texts"]))
            raw_clip = AudioSegment.from_mp3(raw_path)
            ratio = len(raw_clip) / sc["target_ms"] if sc["target_ms"] else 1.0
            atempo = max(FIX_ATEMPO_MIN, min(FIX_ATEMPO_MAX, ratio))
            _apply_atempo(cfg.ffmpeg_path, raw_path, stretched_path, atempo)
            rebuilt += AudioSegment.from_wav(stretched_path)

        rebuilt_path = fix_dir / f"{idx:02d}_rebuilt.wav"
        rebuilt.export(rebuilt_path, format="wav")
        log["rebuilt"].append({"idx": idx, "target_ms": c["target_ms"], "rebuilt_ms": len(rebuilt)})

    (scratch_dir / "fix_log.json").write_text(json.dumps(log, indent=2))
    return log


def finalize(segments: list[dict], scratch_dir: Path, capped_chunks: list[int]) -> Path:
    """Stage 4c: assemble the final track. Reused chunks pass through as-is;
    rebuilt (previously-capped) chunks get an UNCONDITIONAL final corrective
    tempo pass so every chunk lands within ~50ms of target, no exceptions."""
    cfg = get_config()
    chunks_dir = scratch_dir / "chunks"
    fix_dir = scratch_dir / "chunks_fix"

    chunks = _chunk_segments(segments, CHUNK_SIZE)
    final_master = AudioSegment.empty()
    log = {"chunks": []}

    for c in chunks:
        idx = c["idx"]
        if idx not in capped_chunks:
            clip = AudioSegment.from_wav(chunks_dir / f"{idx:02d}_stretched.wav")
            final_master += clip
            log["chunks"].append({"idx": idx, "action": "reused", "final_ms": len(clip)})
            continue

        rebuilt_path = fix_dir / f"{idx:02d}_rebuilt.wav"
        rebuilt = AudioSegment.from_wav(rebuilt_path)
        target_ms = c["target_ms"]
        ratio = len(rebuilt) / target_ms if target_ms else 1.0

        final_path = fix_dir / f"{idx:02d}_final.wav"
        _apply_atempo(cfg.ffmpeg_path, rebuilt_path, final_path, ratio)
        corrected = AudioSegment.from_wav(final_path)
        final_master += corrected
        log["chunks"].append({
            "idx": idx, "action": "corrective_pass", "rebuilt_ms": len(rebuilt),
            "target_ms": target_ms, "ratio": round(ratio, 4), "final_ms": len(corrected),
        })

    out_path = scratch_dir / "dub_master_final.wav"
    final_master.export(out_path, format="wav")
    log["total_ms"] = len(final_master)
    (scratch_dir / "finalize_log.json").write_text(json.dumps(log, indent=2))
    return out_path


def tighten(dub_master_path: Path, source_video_duration_ms: int, out_path: Path) -> dict:
    """Last-chunk / trailing-gap exception: if the dub track's natural length
    undershoots the source video's actual duration (a real trailing pause the
    English original didn't fill with speech either -- confirm this against
    the source before trusting it, don't assume), pad with trailing silence
    rather than stretching speech to fill dead air. If the dub is already the
    same length or longer, this is a no-op copy."""
    master = AudioSegment.from_wav(dub_master_path)
    pad_ms = round(source_video_duration_ms - len(master))

    if pad_ms > 0:
        padded = master + AudioSegment.silent(duration=pad_ms, frame_rate=master.frame_rate)
    else:
        padded = master
    padded.export(out_path, format="wav")

    return {"dub_ms_before_pad": len(master), "pad_ms": max(0, pad_ms), "final_ms": len(padded)}
