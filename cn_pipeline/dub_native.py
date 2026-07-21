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

zh_script.json schema (operator-written). Two forms:
  {"passages": [{"anchor_id": "a01", "text": "一段自然的中文口播……"}, ...]}
or, BEAT-TAGGED (preferred — reviewer round 2026-07-21 showed free-running
passages drift off the picture mid-window and pool all slack as dead air at
the window end):
  {"passages": [{"anchor_id": "a01", "beats": [
      {"text": "第一拍。", "en_seg": 0},
      {"text": "第二拍,含义对应原片第5句。", "en_seg": 5}]}, ...]}
Each beat is 1-2 sentences tagged with the ENGLISH segment whose meaning it
carries. The English cue grid IS the visual beat map (the animation was timed
to it), so finalize places each beat at its segment's timestamp: slack becomes
many small natural pauses exactly where the original narrator breathed,
instead of one silent blob before the next anchor. Speech is never stretched:
a beat running long just flows on and later pauses absorb it.
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
# beat cutting: a cut must land inside a real inter-sentence silence of the
# take; alignment gives the approximate boundary, the nearest detected silence
# gives the safe cut point (never mid-word)
BEAT_SILENCE_MIN_MS = 160
BEAT_SILENCE_THRESH_DBFS = -42.0
BEAT_CUT_SEARCH_MS = 900   # how far from the aligned boundary a silence may be
BEAT_FADE_MS = 25          # anti-click at cut points
MIN_INTER_BEAT_GAP_MS = 120


def load_script(path: Path, anchor_data: dict) -> list[dict]:
    """Validated passages, 1:1 with anchors, in anchor order. Beat-tagged
    passages are normalized so p["text"] is always the full joined text (the
    TTS cache keys on it, so re-partitioning a passage into beats without
    changing a character re-buys nothing)."""
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
        beats = p.get("beats")
        if beats:
            for b in beats:
                if not (b.get("text") or "").strip():
                    raise ValueError(f"passage {p.get('anchor_id')}: empty beat text")
            p["text"] = "".join(b["text"] for b in beats)
        elif (p.get("text") or "").strip():
            p["beats"] = [{"text": p["text"], "en_seg": None}]
        else:
            raise ValueError(f"passage {p.get('anchor_id')}: empty text")
    return passages


def _passage_wav(raw_path: Path, ffmpeg_path: str) -> AudioSegment:
    """Decode a raw TTS take and trim lead/tail silence so placement starts
    on actual speech (ElevenLabs pads takes with breath room). The tail keeps
    0.4s of natural decay — a 0.1s chop reads as "cut off violently"
    (reviewer, stop-training v2 @2:28)."""
    import subprocess
    wav_path = raw_path.with_suffix(".trim.wav")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", str(raw_path), "-af",
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02:"
         "stop_periods=-1:stop_threshold=-45dB:stop_silence=0.40",
         "-ar", "44100", "-ac", "2", str(wav_path)],
        capture_output=True, check=True,
    )
    return AudioSegment.from_wav(wav_path)


def _cut_beats(take: AudioSegment, beat_texts: list[str], take_wav: Path,
               scratch_dir: Path) -> list[AudioSegment]:
    """Cut one passage take into per-beat pieces. Forced alignment locates the
    approximate boundary between consecutive beats; the actual cut lands in
    the middle of the nearest DETECTED silence (TTS pauses ~0.2-0.5s at
    sentence ends), so a cut can never clip a word. Tiny fades kill clicks."""
    if len(beat_texts) == 1:
        return [take]
    from pydub.silence import detect_silence
    from cn_pipeline import align

    import subprocess
    cfg = get_config()
    ali_wav = take_wav.with_suffix(".16k.wav")
    subprocess.run([cfg.ffmpeg_path, "-y", "-i", str(take_wav),
                    "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(ali_wav)],
                   capture_output=True, check=True)
    cues = align.force_align_chunk(ali_wav, beat_texts, [""] * len(beat_texts), 0, len(take))
    silences = detect_silence(take, min_silence_len=BEAT_SILENCE_MIN_MS,
                              silence_thresh=BEAT_SILENCE_THRESH_DBFS)

    cuts = []
    for i in range(1, len(beat_texts)):
        approx = cues[i][0]  # aligned start of beat i
        best = None
        for a, b in silences:
            mid = (a + b) // 2
            if abs(mid - approx) < (abs(best - approx) if best is not None else 10 ** 9):
                best = mid
        cut = best if best is not None and abs(best - approx) <= BEAT_CUT_SEARCH_MS else approx
        cut = max(cuts[-1] + 100 if cuts else 100, min(cut, len(take) - 100))
        cuts.append(cut)

    pieces = []
    bounds = [0] + cuts + [len(take)]
    for i in range(len(beat_texts)):
        piece = take[bounds[i]:bounds[i + 1]]
        pieces.append(piece.fade_in(BEAT_FADE_MS).fade_out(BEAT_FADE_MS))
    return pieces


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


def _seg_start_ms(segments: list[dict], idx) -> int | None:
    """Start time (ms) of English segment idx -- the visual beat target."""
    if idx is None or not (0 <= idx < len(segments)):
        return None
    from cn_pipeline.subtitles import parse_srt_time
    return parse_srt_time(segments[idx]["time"].split(" --> ")[0])


def finalize(anchor_data: dict, passages: list[dict], scratch_dir: Path) -> Path:
    """Assemble the anchor-timed track -> dub_master_final.wav (same name and
    place as cue-locked mode, so tighten/mix-me/render are reused unchanged).

    Beat placement: within a window, each beat is placed at its tagged English
    segment's timestamp (the visual beat the animation was timed to) -- unless
    the previous beat is still running, in which case it follows after a small
    natural gap. Never stretched. A feasibility clamp guarantees all remaining
    beats still fit before the window end, so a hard cut at the next anchor is
    impossible. Hard-errors if any passage still overflows its window."""
    cfg = get_config()
    gen_log = json.loads((scratch_dir / "native_generate_log.json").read_text(encoding="utf-8"))
    entries = {e["anchor_id"]: e for e in gen_log["passages"]}
    p_dir = scratch_dir / "passages"
    wins = anchors_mod.windows(anchor_data)
    seg_path = scratch_dir / "segments.json"
    segments = json.loads(seg_path.read_text(encoding="utf-8")) if seg_path.exists() else []

    still_over = [aid for aid in gen_log["overflows"]]
    if still_over:
        raise RuntimeError(
            f"passages still overflow their windows: {', '.join(still_over)} -- "
            "tighten the wording in zh_script.json and re-run `dub generate` "
            "(the cache only re-buys edited passages)")

    track = AudioSegment.silent(duration=0, frame_rate=44100)
    log = {"passages": [], "beats": []}
    for win, passage in zip(wins, passages):
        aid = win["anchor_id"]
        raw = p_dir / f"{aid}_raw.mp3"
        clip = _passage_wav(raw, cfg.ffmpeg_path)
        entry = entries[aid]
        if entry["status"] == "tempo_fit":
            fit_path = p_dir / f"{aid}_fit.wav"
            src_path = p_dir / f"{aid}_src.wav"
            clip.export(src_path, format="wav")
            _apply_atempo(cfg.ffmpeg_path, src_path, fit_path, entry["atempo"])
            clip = AudioSegment.from_wav(fit_path)

        beats = passage.get("beats") or [{"text": passage["text"], "en_seg": None}]
        pieces = _cut_beats(clip, [b["text"] for b in beats], raw.with_suffix(".trim.wav"),
                            scratch_dir)

        onset = win["start_ms"] + win["lead_ms"]
        if len(track) < onset:
            track += AudioSegment.silent(duration=onset - len(track), frame_rate=44100)
        placed_start = len(track)

        remaining = sum(len(p) for p in pieces)
        for bi, (beat, piece) in enumerate(zip(beats, pieces)):
            target = _seg_start_ms(segments, beat.get("en_seg"))
            cursor = len(track)
            start = cursor if bi == 0 else max(cursor + MIN_INTER_BEAT_GAP_MS,
                                               target if target is not None else cursor)
            # feasibility clamp: every remaining beat must still fit the window
            start = min(start, win["end_ms"] - remaining)
            start = max(start, cursor)
            if start > cursor:
                track += AudioSegment.silent(duration=start - cursor, frame_rate=44100)
            track += piece
            remaining -= len(piece)
            log["beats"].append({
                "anchor_id": aid, "beat": bi, "en_seg": beat.get("en_seg"),
                "target_ms": target, "placed_ms": start,
                "end_ms": start + len(piece),
                "drift_ms": (start - target) if target is not None else None,
            })

        if len(track) > win["end_ms"]:
            over = len(track) - win["end_ms"]
            if over > 50:
                raise RuntimeError(f"{aid}: placed audio overruns its window by {over}ms")
            track = track[:win["end_ms"]]
        log["passages"].append({
            "anchor_id": aid, "placed_start_ms": placed_start,
            "speech_end_ms": len(track), "window_end_ms": win["end_ms"],
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
