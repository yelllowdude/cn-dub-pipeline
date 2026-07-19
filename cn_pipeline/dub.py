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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from pydub import AudioSegment

from cn_pipeline.config import get_config
from cn_pipeline.spend import record_call
from cn_pipeline.subtitles import parse_srt_time

VOICE_ID = "MI36FIkp9wRP7cpWKPTl"  # Evan Zhao
MODEL_ID = "eleven_v3"

CHUNK_SIZE = 15
SUB_CHUNK_SIZE = 5
GEN_ATEMPO_MIN, GEN_ATEMPO_MAX = 0.85, 1.4
FIX_ATEMPO_MIN, FIX_ATEMPO_MAX = 0.85, 1.45

# Chunks are independent TTS calls -- nothing about chunk 5 depends on chunk 4
# (see cn_workflow.html Stage 4's "concurrently, not a for-loop" rule; this
# was documented behavior before it was implemented behavior). Kept modest so
# a burst can't trip ElevenLabs' concurrent-request limit.
TTS_CONCURRENCY = 4


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


def _tts_call(api_key: str, texts: list[str], scratch_dir: Path) -> bytes:
    cfg = get_config()
    record_call(scratch_dir, "tts", cfg.max_tts_calls_per_run)
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


def _tts_cached(api_key: str, texts: list[str], raw_path: Path, scratch_dir: Path) -> None:
    """Generate raw TTS audio at raw_path, reusing a cached take ONLY if it was
    generated from these exact texts. A bare raw_path.exists() check (the old
    behavior) reuses stale audio after a translation edit -- the rerun then
    ships a dub whose voice disagrees with the subtitles, silently. The texts
    that produced each take are stored alongside it and compared verbatim."""
    texts_path = raw_path.with_suffix(".texts.json")
    if raw_path.exists() and texts_path.exists():
        if json.loads(texts_path.read_text(encoding="utf-8")) == texts:
            return
    raw_path.write_bytes(_tts_call(api_key, texts, scratch_dir))
    texts_path.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")


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

    # TTS pass first, concurrently -- each chunk is an independent API call
    # writing its own file. _tts_cached no-ops for chunks whose cached take
    # already matches the current translation, so a resume or a rerun after
    # a partial translation edit only pays for what actually changed.
    with ThreadPoolExecutor(max_workers=TTS_CONCURRENCY) as pool:
        futures = {
            pool.submit(
                _tts_cached, cfg.elevenlabs_api_key,
                zh_lines[c["seg_start"]:c["seg_end"]],
                chunks_dir / f"{c['idx']:02d}_raw.mp3", scratch_dir,
            ): c["idx"]
            for c in chunks
        }
        for fut in as_completed(futures):
            fut.result()  # re-raise the first TTS/spend-cap failure loudly

    # Tempo-fit pass, sequential -- local ffmpeg work, and the log order
    # should match chunk order.
    for c in chunks:
        raw_path = chunks_dir / f"{c['idx']:02d}_raw.mp3"
        stretched_path = chunks_dir / f"{c['idx']:02d}_stretched.wav"

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
            _tts_cached(cfg.elevenlabs_api_key, sc["texts"], raw_path, scratch_dir)
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


CORRECTIVE_ATEMPO_MIN, CORRECTIVE_ATEMPO_MAX = 0.5, 2.0


def _last_chunk_gap_aware_rebuild(
    api_key: str, ffmpeg_path: str, seg_slice: list[dict], zh_slice: list[str], fix_dir: Path, idx: int
) -> AudioSegment:
    """Rebuild the LAST chunk cue-by-cue instead of as one blob, preserving any
    real internal gap between cues (e.g. a silent/non-verbal visual pause before
    a closing line like "Thanks for watching"). A single uniform stretch across
    the whole chunk would force-slow real speech to fill that gap instead --
    exactly the desync this function exists to avoid. Each cue gets its own
    TTS call and its own modest tempo fit (clamped like a normal chunk); gaps
    between consecutive cues are inserted as real silence, not stretched into."""
    out = AudioSegment.empty()
    prev_end_ms = None
    for j, (seg, zh) in enumerate(zip(seg_slice, zh_slice), 1):
        start_ms = parse_srt_time(seg["time"].split(" --> ")[0])
        end_ms = parse_srt_time(seg["time"].split(" --> ")[1])
        if prev_end_ms is not None:
            gap_ms = start_ms - prev_end_ms
            if gap_ms > 0:
                out += AudioSegment.silent(duration=gap_ms, frame_rate=44100)
        prev_end_ms = end_ms

        raw_path = fix_dir / f"{idx:02d}_cue{j:02d}_raw.mp3"
        stretched_path = fix_dir / f"{idx:02d}_cue{j:02d}_stretched.wav"
        # fix_dir sits directly under the run scratch dir, which is where the
        # spend counter lives
        _tts_cached(api_key, [zh], raw_path, fix_dir.parent)
        raw_clip = AudioSegment.from_mp3(raw_path)
        cue_target_ms = end_ms - start_ms
        ratio = len(raw_clip) / cue_target_ms if cue_target_ms else 1.0
        atempo = max(GEN_ATEMPO_MIN, min(GEN_ATEMPO_MAX, ratio))
        _apply_atempo(ffmpeg_path, raw_path, stretched_path, atempo)
        out += AudioSegment.from_wav(stretched_path)

    return out


def finalize(segments: list[dict], zh_lines: list[str], scratch_dir: Path, capped_chunks: list[int]) -> Path:
    """Stage 4c: assemble the final track. Reused chunks pass through as-is;
    rebuilt (previously-capped) chunks get an UNCONDITIONAL final corrective
    tempo pass so every chunk lands within ~50ms of target, no exceptions --
    EXCEPT the last chunk in the video, which is exempt from force-stretching
    (see docs/cn_workflow.html Stage 4): if it undershoots so far that hitting
    target would mean forcing real speech below CORRECTIVE_ATEMPO_MIN (a real
    internal or trailing pause, not just an ordinary miss), it's rebuilt
    cue-by-cue instead, with the true gap between cues inserted as silence
    rather than stretched into."""
    cfg = get_config()
    chunks_dir = scratch_dir / "chunks"
    fix_dir = scratch_dir / "chunks_fix"

    chunks = _chunk_segments(segments, CHUNK_SIZE)
    last_idx = chunks[-1]["idx"] if chunks else None
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

        if idx == last_idx and ratio < CORRECTIVE_ATEMPO_MIN:
            corrected = _last_chunk_gap_aware_rebuild(
                cfg.elevenlabs_api_key, cfg.ffmpeg_path,
                segments[c["seg_start"]:c["seg_end"]], zh_lines[c["seg_start"]:c["seg_end"]],
                fix_dir, idx,
            )
            final_master += corrected
            log["chunks"].append({
                "idx": idx, "action": "last_chunk_gap_aware_rebuild",
                "rebuilt_ms": len(rebuilt), "target_ms": target_ms,
                "ratio": round(ratio, 4), "final_ms": len(corrected),
            })
            continue

        final_path = fix_dir / f"{idx:02d}_final.wav"
        clamped_ratio = max(CORRECTIVE_ATEMPO_MIN, min(CORRECTIVE_ATEMPO_MAX, ratio))
        _apply_atempo(cfg.ffmpeg_path, rebuilt_path, final_path, clamped_ratio)
        corrected = AudioSegment.from_wav(final_path)
        final_master += corrected
        log["chunks"].append({
            "idx": idx, "action": "corrective_pass", "rebuilt_ms": len(rebuilt),
            "target_ms": target_ms, "ratio": round(ratio, 4),
            "clamped_ratio": round(clamped_ratio, 4), "final_ms": len(corrected),
        })

    out_path = scratch_dir / "dub_master_final.wav"
    final_master.export(out_path, format="wav")
    log["total_ms"] = len(final_master)
    (scratch_dir / "finalize_log.json").write_text(json.dumps(log, indent=2))
    return out_path


OVERSHOOT_AUTO_TRIM_MAX_MS = 5000


def tighten(dub_master_path: Path, source_video_duration_ms: int, out_path: Path) -> dict:
    """Last-chunk / trailing-gap exception: if the dub track's natural length
    undershoots the source video's actual duration (a real trailing pause the
    English original didn't fill with speech either -- confirm this against
    the source before trusting it, don't assume), pad with trailing silence
    rather than stretching speech to fill dead air.

    If the dub instead OVERSHOOTS the source by a small amount (observed cause:
    the last cue's Whisper-transcribed end timestamp running a second or two
    past the video's actual end -- a transcription-precision artifact, not a
    real content mismatch), trim the tail by the overshoot so the render-stage
    duration check (must match source within ~0.1s) can actually pass. This is
    capped at OVERSHOOT_AUTO_TRIM_MAX_MS: a larger overshoot means something
    upstream broke and should be flagged, not silently chopped."""
    master = AudioSegment.from_wav(dub_master_path)
    pad_ms = round(source_video_duration_ms - len(master))

    if pad_ms > 0:
        padded = master + AudioSegment.silent(duration=pad_ms, frame_rate=master.frame_rate)
        trimmed_ms = 0
    elif pad_ms < 0 and -pad_ms <= OVERSHOOT_AUTO_TRIM_MAX_MS:
        trimmed_ms = -pad_ms
        padded = master[:source_video_duration_ms]
    else:
        padded = master
        trimmed_ms = 0

    padded.export(out_path, format="wav")

    return {
        "dub_ms_before_pad": len(master), "pad_ms": max(0, pad_ms),
        "trimmed_ms": trimmed_ms, "final_ms": len(padded),
    }


ME_GAIN_DB = -4.0


def mix_me(vo_path: Path, me_wav_path: Path, out_path: Path, me_gain_db: float = ME_GAIN_DB) -> dict:
    """Mix the tightened Chinese VO track with the project's {id}_me.wav
    background bed (music + effects, no VO, language-agnostic -- see
    docs/cn_workflow.html Drive structure). This was previously documented
    ("me.wav present -> Stage 4 mixes it in") but never actually implemented
    anywhere in the pipeline -- `preflight` only checked for the file's
    existence and printed a note; nothing consumed it. This is that step.

    me.wav is attenuated by me_gain_db (default -4dB) so it sits under the
    dubbed VO rather than competing with it -- the original English mix's
    balance doesn't transfer directly since the new VO (TTS) has different
    loudness characteristics than the original human VO that was removed
    when me.wav was separated out.

    me.wav's length should already match the source video (it's derived
    from the same master), but is trimmed/padded defensively to the VO's
    exact length so a mismatch here can't silently desync the final render."""
    vo = AudioSegment.from_wav(vo_path)
    me = AudioSegment.from_wav(me_wav_path).apply_gain(me_gain_db)

    if len(me) < len(vo):
        me = me + AudioSegment.silent(duration=len(vo) - len(me), frame_rate=me.frame_rate)
    elif len(me) > len(vo):
        me = me[:len(vo)]

    mixed = vo.overlay(me)
    mixed.export(out_path, format="wav")

    return {"vo_ms": len(vo), "me_ms_before_trim": len(AudioSegment.from_wav(me_wav_path)), "final_ms": len(mixed)}
