"""
Native dub mode: dub-first, anchor-synced Chinese VO.

Why this exists: the cue-locked path (dub.py) tempo-stretches every chunk to
exactly fit the ENGLISH cue grid -- observed 1.18-1.26x on real videos, which
is what native reviewers keep flagging as "rushed / feels off", and the 1:1
cue translation locks Chinese sentence structure to English clause breaks.
Native mode inverts the priority: the operator writes natural spoken-Chinese
PASSAGES (one per anchor window, see anchors.py), TTS runs at natural pace,
and fit problems are fixed by TIGHTENING THE WORDING, never by speeding up
speech. Subtitles are derived from the finished dub afterwards (align-dub),
so cue breaks land at natural Chinese sentence boundaries by construction.

Fit rules (the inversion, authoritative here until cn_workflow.html gains a
native-mode section):
  - passage fits its window  -> place at anchor (+lead_ms); slack becomes
    trailing silence. NEVER stretch speech to fill dead air.
  - over by <= NATIVE_ATEMPO_MAX -> gentle uniform tempo fit, logged loudly.
    There is NO slow-down band at all (min is 1.0 by construction).
  - over by more              -> "overflow": flagged with a concrete
    cut-this-many-characters target for the operator to tighten the wording.
    finalize() hard-errors while any overflow remains -- drift must never
    push the next anchor.

zh_script.json schema (operator-written):
  {"passages": [{"anchor_id": "a01", "text": "一段自然的中文口播……"}, ...]}
One passage per anchor, same order. Product names stay English (glossary).
"""

import json
from pathlib import Path

from pydub import AudioSegment

from cn_pipeline import anchors as anchors_mod
from cn_pipeline.config import get_config
from cn_pipeline.dub import _apply_atempo, _tts_cached

NATIVE_ATEMPO_MAX = 1.06   # <=6% speed-up is below the "sounds rushed" threshold
ANCHOR_TOLERANCE_MS = 500  # verify: speech onset within this of anchor+lead
# pydub leading-silence detection for the onset check
ONSET_SILENCE_THRESH_DBFS = -40.0


def load_script(path: Path, anchor_data: dict) -> list[dict]:
    """Validated passages, 1:1 with anchors, in anchor order."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    passages = data.get("passages")
    if not isinstance(passages, list):
        raise ValueError("zh_script.json must have a 'passages' list")
    anchor_ids = [a["id"] for a in anchor_data["anchors"]]
    got_ids = [p.get("anchor_id") for p in passages]
    if got_ids != anchor_ids:
        raise ValueError(f"passages must match anchors 1:1 in order.\n"
                         f"  anchors:  {anchor_ids}\n  passages: {got_ids}")
    for p in passages:
        if not (p.get("text") or "").strip():
            raise ValueError(f"passage {p.get('anchor_id')}: empty text")
    return passages


def _passage_wav(raw_path: Path, ffmpeg_path: str) -> AudioSegment:
    """Decode a raw TTS take and trim lead/tail silence so placement starts
    on actual speech (ElevenLabs pads takes with breath room)."""
    import subprocess
    wav_path = raw_path.with_suffix(".trim.wav")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", str(raw_path), "-af",
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02:"
         "stop_periods=-1:stop_threshold=-45dB:stop_silence=0.10",
         "-ar", "44100", "-ac", "2", str(wav_path)],
        capture_output=True, check=True,
    )
    return AudioSegment.from_wav(wav_path)


def generate(anchor_data: dict, passages: list[dict], scratch_dir: Path) -> dict:
    """TTS every passage (cached against exact text -- a tightening edit only
    re-buys the edited passage) and produce the fit report. Writes
    native_generate_log.json; returns it."""
    cfg = get_config()
    p_dir = scratch_dir / "passages"
    p_dir.mkdir(exist_ok=True)
    wins = anchors_mod.windows(anchor_data)

    log = {"passages": [], "overflows": []}
    for win, passage in zip(wins, passages):
        aid = win["anchor_id"]
        raw = p_dir / f"{aid}_raw.mp3"
        _tts_cached(cfg.elevenlabs_api_key, [passage["text"]], raw, scratch_dir)
        clip = _passage_wav(raw, cfg.ffmpeg_path)
        usable_ms = win["end_ms"] - win["start_ms"] - win["lead_ms"]
        ratio = len(clip) / usable_ms if usable_ms else float("inf")
        entry = {
            "anchor_id": aid, "window_ms": usable_ms, "actual_ms": len(clip),
            "ratio": round(ratio, 4), "chars": len(passage["text"]),
        }
        if ratio <= 1.0:
            entry["status"] = "fits"
            entry["slack_ms"] = usable_ms - len(clip)
        elif ratio <= NATIVE_ATEMPO_MAX:
            entry["status"] = "tempo_fit"
            entry["atempo"] = round(ratio, 4)
        else:
            entry["status"] = "overflow"
            entry["over_ms"] = len(clip) - usable_ms
            entry["target_chars"] = int(len(passage["text"]) / ratio)
            log["overflows"].append(aid)
        log["passages"].append(entry)

    (scratch_dir / "native_generate_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return log


def finalize(anchor_data: dict, passages: list[dict], scratch_dir: Path) -> Path:
    """Assemble the anchor-timed track -> dub_master_final.wav (same name and
    place as cue-locked mode, so tighten/mix-me/render are reused unchanged).
    Hard-errors if any passage still overflows: an overflow here means the
    tightening loop was skipped, and pushing the next anchor is never OK."""
    cfg = get_config()
    gen_log = json.loads((scratch_dir / "native_generate_log.json").read_text(encoding="utf-8"))
    entries = {e["anchor_id"]: e for e in gen_log["passages"]}
    p_dir = scratch_dir / "passages"
    wins = anchors_mod.windows(anchor_data)

    still_over = [aid for aid in gen_log["overflows"]]
    if still_over:
        raise RuntimeError(
            f"passages still overflow their windows: {', '.join(still_over)} -- "
            "tighten the wording in zh_script.json and re-run `dub generate` "
            "(the cache only re-buys edited passages)")

    track = AudioSegment.silent(duration=0, frame_rate=44100)
    log = {"passages": []}
    for win, passage in zip(wins, passages):
        aid = win["anchor_id"]
        clip = _passage_wav(p_dir / f"{aid}_raw.mp3", cfg.ffmpeg_path)
        entry = entries[aid]
        if entry["status"] == "tempo_fit":
            fit_path = p_dir / f"{aid}_fit.wav"
            src_path = p_dir / f"{aid}_src.wav"
            clip.export(src_path, format="wav")
            _apply_atempo(cfg.ffmpeg_path, src_path, fit_path, entry["atempo"])
            clip = AudioSegment.from_wav(fit_path)
        onset = win["start_ms"] + win["lead_ms"]
        if len(track) < onset:
            track += AudioSegment.silent(duration=onset - len(track), frame_rate=44100)
        placed_start = len(track)
        track += clip
        if len(track) > win["end_ms"]:
            # tempo_fit rounding can land a few ms over; anything bigger is a bug
            over = len(track) - win["end_ms"]
            if over > 50:
                raise RuntimeError(f"{aid}: placed audio overruns its window by {over}ms")
            track = track[:win["end_ms"]]
        log["passages"].append({
            "anchor_id": aid, "placed_start_ms": placed_start,
            "speech_end_ms": placed_start + len(clip), "window_end_ms": win["end_ms"],
        })
    if len(track) < anchor_data["video_ms"]:
        track += AudioSegment.silent(duration=anchor_data["video_ms"] - len(track), frame_rate=44100)

    out_path = scratch_dir / "dub_master_final.wav"
    track.export(out_path, format="wav")
    log["total_ms"] = len(track)
    (scratch_dir / "native_finalize_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def verify_anchors(anchor_data: dict, scratch_dir: Path) -> list[dict]:
    """The native-mode close-out gate (run alongside `render verify`): prove
    from the ACTUAL assembled audio -- not the logs -- that every passage's
    speech starts within ANCHOR_TOLERANCE_MS of its anchor and never bleeds
    past the next one. Returns per-anchor results; caller decides exit code."""
    from pydub.silence import detect_leading_silence

    fin_log = json.loads((scratch_dir / "native_finalize_log.json").read_text(encoding="utf-8"))
    track = AudioSegment.from_wav(scratch_dir / "dub_master_final.wav")
    wins = {w["anchor_id"]: w for w in anchors_mod.windows(anchor_data)}

    results = []
    for entry in fin_log["passages"]:
        aid = entry["anchor_id"]
        win = wins[aid]
        expected_onset = win["start_ms"] + win["lead_ms"]
        span = track[win["start_ms"]:win["end_ms"]]
        lead = detect_leading_silence(span, silence_threshold=ONSET_SILENCE_THRESH_DBFS)
        actual_onset = win["start_ms"] + lead
        onset_drift = actual_onset - expected_onset
        bleed = entry["speech_end_ms"] > win["end_ms"]
        results.append({
            "anchor_id": aid,
            "expected_onset_ms": expected_onset,
            "actual_onset_ms": actual_onset,
            "onset_drift_ms": onset_drift,
            "slack_ms": win["end_ms"] - entry["speech_end_ms"],
            "ok": abs(onset_drift) <= ANCHOR_TOLERANCE_MS and not bleed,
        })
    return results
