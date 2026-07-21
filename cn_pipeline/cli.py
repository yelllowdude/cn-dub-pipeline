"""
CLI entrypoint: python -m cn_pipeline.cli <group> <command> --project-id <id>

This is the surface the Skill drives -- named commands, not one-off inline
Python reconstructed per run. Mechanical stages only; translation, title
pick, and thumbnail headline wording are live Claude/human judgment calls
made *between* these commands (see .claude/skills/localize-chinese/SKILL.md).

Per-run intermediate/tuning data (segments.json, zh.json, thumb_config.json,
dub_overrides.json, logs) lives under runs/{project-id}/ -- gitignored,
per-video data, not code.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from cn_pipeline import align, anchors, dub, dub_native, frameio, paths, publish, render, screentext, subtitles, thumbnail
from cn_pipeline.config import ConfigError, get_config


def _scratch(project_id: str) -> Path:
    return paths.run_scratch_dir(project_id)


def _dub_mode(scratch: Path) -> str:
    """Per-project dub mode from runs/{id}/project.json. Absent file or key =
    "cue_locked", so every pre-native project keeps its exact behavior."""
    pj = scratch / "project.json"
    if pj.exists():
        return json.loads(pj.read_text(encoding="utf-8")).get("dub_mode", "cue_locked")
    return "cue_locked"


def _stage_gate(args, outputs: list[Path], inputs: list[Path] = ()) -> bool:
    """SKIP_OK semantics per cn_workflow.html section 04: a done stage skips,
    only --force redoes it. "Done" is make-style -- all outputs exist and none
    is older than any existing input -- so forcing one stage automatically
    un-skips every downstream stage that consumes its output (their inputs
    become newer than their outputs). Returns True if the stage should run."""
    if getattr(args, "force", False):
        return True
    if any(not o.exists() for o in outputs):
        return True
    oldest_output = min(o.stat().st_mtime for o in outputs)
    newest_input = max((i.stat().st_mtime for i in inputs if i.exists()), default=None)
    if newest_input is not None and newest_input > oldest_output:
        return True
    print(f"SKIP_OK: {', '.join(o.name for o in outputs)} up to date -- use --force to redo")
    return False


def cmd_preflight(args):
    cfg = get_config()  # raises ConfigError loudly if anything's missing
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    me_wav = paths.me_wav_path(project_dir)
    out = paths.deliverable_paths(project_dir)
    print(f"project dir: {project_dir}")
    print(f"master video: {master}")
    print(f"me.wav present: {me_wav.exists()} ({me_wav if me_wav.exists() else 'not found, dub ships without a background bed'})")
    print(f"ffmpeg: {cfg.ffmpeg_path}")

    en_srt = out["en_srt"]
    print(f"en.srt present: {en_srt.exists()} "
          f"({en_srt if en_srt.exists() else 'not found -- transcribe stage will generate it, not a stop condition'})")

    done = [k for k, p in out.items() if p.exists()]
    if done:
        print(f"existing /CN/ deliverables (stages will SKIP_OK unless --force): {', '.join(sorted(done))}")

    print()
    print("Pre-flight OK (mechanical checks). Stage 1 items that are NOT checkable "
          "from here and stay owned by the skill run (see cn_workflow.html Stage 1):")
    print("  - winning title present in the source Notion row (missing -> fall back to "
          "live published title, tick 'Title provisional?', print it loud)")
    print("  - thumbnail source image located (text-free preferred; baked-text ok)")
    print("  - sponsor detection: after transcribe, scan the transcript + source row's "
          "sponsor field; verdict drives 'Contains ads?' and the ad-disclosure section")
    print("Proceed to transcription, then translation (live judgment -- see SKILL.md).")


def cmd_extract_audio(args):
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    scratch = _scratch(args.project_id)
    out = scratch / "audio_16k.wav"
    if not _stage_gate(args, [out], [master]):
        return
    align.extract_audio_16k(master, out)
    print(f"wrote {out}")


def cmd_transcribe(args):
    scratch = _scratch(args.project_id)
    audio = scratch / "audio_16k.wav"
    if not audio.exists():
        sys.exit(f"{audio} not found -- run `align extract-audio` first")
    out_srt = scratch / "whisper_raw.srt"
    if not _stage_gate(args, [out_srt], [audio]):
        return
    align.transcribe_to_srt(audio, out_srt)
    print(f"wrote {out_srt}")


def cmd_split_cues(args):
    scratch = _scratch(args.project_id)
    raw_srt = scratch / "whisper_raw.srt"
    if not raw_srt.exists():
        sys.exit(f"{raw_srt} not found -- run `align transcribe` first")
    if not _stage_gate(args, [scratch / "segments.json"], [raw_srt]):
        return
    segs = subtitles.split_cues(raw_srt)
    subtitles.save_segments(segs, scratch / "segments.json")
    print(f"wrote {len(segs)} cues to {scratch / 'segments.json'}")
    print("Next: translate segments.json's texts to Chinese (live judgment, "
          "against glossary/cn_glossary.md), write as a JSON array of strings "
          "in the same order, then run `subtitles ingest-translation`.")


def cmd_ingest_translation(args):
    scratch = _scratch(args.project_id)
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads(Path(args.zh_json).read_text(encoding="utf-8"))
    if len(zh) != len(segs):
        sys.exit(f"translation has {len(zh)} lines, segments.json has {len(segs)} -- must match 1:1")
    shutil.copy(args.zh_json, scratch / "zh.json")
    print(f"ingested {len(zh)} translated lines")


def cmd_build_srt(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir)
    outputs = [scratch / "bilingual_ensub.srt", out["en_srt"], out["zh_srt"]]
    if not _stage_gate(args, outputs, [scratch / "segments.json", scratch / "zh.json"]):
        return
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    subtitles.build_bilingual_srt(segs, zh, scratch / "bilingual_ensub.srt")
    subtitles.build_mono_srt(segs, zh, out["en_srt"], out["zh_srt"])
    print(f"wrote {scratch / 'bilingual_ensub.srt'}, {out['en_srt']}, {out['zh_srt']}")


def _print_capped_status(log: dict):
    capped = log["capped_chunks"]
    if capped:
        print(f"{len(capped)} chunk(s) hit the tempo cap: {capped} -- run `dub fix` next")
    else:
        print("no chunks capped -- skip `dub fix`, go straight to `dub finalize`")


def cmd_dub_generate(args):
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) == "native":
        inputs = [scratch / "anchors.json", scratch / "zh_script.json"]
        if not _stage_gate(args, [scratch / "native_generate_log.json"], inputs):
            return
        anchor_data = anchors.load_anchors(scratch / "anchors.json")
        passages = dub_native.load_script(scratch / "zh_script.json", anchor_data)
        log = dub_native.generate(anchor_data, passages, scratch)
        for e in log["passages"]:
            extra = (f" -- OVERFLOW by {e['over_ms']}ms, tighten to ~{e['target_chars']} chars"
                     if e["status"] == "overflow"
                     else f" (atempo {e['atempo']})" if e["status"] == "tempo_fit" else "")
            print(f"  {e['anchor_id']}: {e['status']}{extra}")
        if log["overflows"]:
            print(f"\n{len(log['overflows'])} passage(s) overflow -- tighten their wording in "
                  "zh_script.json, re-ingest, and re-run (the cache only re-buys edited passages).")
        else:
            print("\nall passages fit -- run `dub finalize`")
        return
    gen_log_path = scratch / "generate_log.json"
    if not _stage_gate(args, [gen_log_path], [scratch / "segments.json", scratch / "zh.json"]):
        # the orchestrator still needs the capped verdict to pick the next step
        _print_capped_status(json.loads(gen_log_path.read_text()))
        return
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    log = dub.generate(segs, zh, scratch)
    _print_capped_status(log)


def cmd_dub_fix(args):
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) == "native":
        # Native mode has no re-split mechanism: the fix for an overflow is
        # WORDING, not chunking. Print the tightening queue and stop.
        log = json.loads((scratch / "native_generate_log.json").read_text(encoding="utf-8"))
        overflows = [e for e in log["passages"] if e["status"] == "overflow"]
        if not overflows:
            print("no overflows -- nothing to fix; run `dub finalize`")
            return
        print("native mode: fix these by TIGHTENING the passage wording in zh_script.json "
              "(then re-ingest + `dub generate`):")
        for e in overflows:
            print(f"  {e['anchor_id']}: {e['chars']} chars, over by {e['over_ms']}ms "
                  f"-> aim for ~{e['target_chars']} chars")
        return
    if not _stage_gate(args, [scratch / "fix_log.json"], [scratch / "generate_log.json"]):
        return
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    dub.fix_overflow_chunks(segs, zh, scratch, gen_log["capped_chunks"])
    print("re-split + regenerated capped chunks. Run `dub finalize` next.")


def cmd_dub_finalize(args):
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) == "native":
        inputs = [scratch / "native_generate_log.json", scratch / "zh_script.json"]
        if not _stage_gate(args, [scratch / "dub_master_final.wav",
                                  scratch / "native_finalize_log.json"], inputs):
            return
        anchor_data = anchors.load_anchors(scratch / "anchors.json")
        passages = dub_native.load_script(scratch / "zh_script.json", anchor_data)
        out = dub_native.finalize(anchor_data, passages, scratch)
        print(f"wrote {out}")
        return
    inputs = [scratch / "generate_log.json", scratch / "fix_log.json",
              scratch / "segments.json", scratch / "zh.json"]
    if not _stage_gate(args, [scratch / "dub_master_final.wav", scratch / "finalize_log.json"], inputs):
        return
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    out = dub.finalize(segs, zh, scratch, gen_log["capped_chunks"])
    print(f"wrote {out}")


def cmd_dub_tighten(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    if not _stage_gate(args, [scratch / "dub_master_padded.wav"], [scratch / "dub_master_final.wav"]):
        return
    cfg = get_config()
    src_dur_ms = render.probe_duration_ms(cfg.ffmpeg_path, master)
    result = dub.tighten(scratch / "dub_master_final.wav", src_dur_ms, scratch / "dub_master_padded.wav")
    print(json.dumps(result, indent=2))
    if result["pad_ms"] > 3000:
        print(f"NOTE: padded {result['pad_ms']}ms of trailing silence -- confirm this matches "
              f"a real trailing pause in the source (check the tail isn't actually untranscribed "
              f"speech) before trusting it, per the last-chunk exception rule in cn_workflow.html Stage 4.")


def cmd_dub_mix_me(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    me_wav = paths.me_wav_path(project_dir)
    if not me_wav.exists():
        print(f"no {me_wav.name} found at project root -- skipping, dub ships without a background bed")
        return
    out_path = scratch / "dub_master_mixed.wav"
    if not _stage_gate(args, [out_path], [scratch / "dub_master_padded.wav", me_wav]):
        return
    result = dub.mix_me(scratch / "dub_master_padded.wav", me_wav, out_path,
                        me_gain_db=get_config().me_gain_db)
    print(json.dumps(result, indent=2))
    print(f"wrote {out_path}")


def cmd_align_dub(args):
    """Forced-align the finished dub audio back onto cue text -> bilingual_cndub.srt.
    In native mode the cue text comes from the dub-derived Chinese cues
    (zh_cues.json), not the English cue grid -- see _align_dub_native."""
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) == "native":
        _align_dub_native(args, scratch)
        return
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir, args.version)
    inputs = [scratch / "dub_master_final.wav", scratch / "finalize_log.json", scratch / "zh.json"]
    if not _stage_gate(args, [out["bilingual_cndub_srt"]], inputs):
        return
    cfg = get_config()

    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    finalize_log = json.loads((scratch / "finalize_log.json").read_text())
    actual_ms = {c["idx"]: c["final_ms"] for c in finalize_log["chunks"]}

    chunks = dub._chunk_segments(segs, dub.CHUNK_SIZE)
    # Same timeline finalize used to assemble the audio: lead silence + the
    # real inter-chunk gaps. Reading with the old gapless accumulation here
    # would extract each chunk from the wrong offset in the gapped track, so
    # the burned subtitles would slide off the voice.
    timeline = dub.chunk_timeline(segs, dub.CHUNK_SIZE)
    gaps = timeline["gaps"]
    all_cues = []
    chunk_abs_start = timeline["lead_ms"]
    align_dir = scratch / "align_chunks"
    align_dir.mkdir(exist_ok=True)

    for pos, c in enumerate(chunks):
        idx = c["idx"]
        chunk_abs_start += gaps[pos]
        chunk_dur = actual_ms[idx]
        zh_slice = zh[c["seg_start"]:c["seg_end"]]
        en_slice = [s["text"] for s in segs[c["seg_start"]:c["seg_end"]]]

        chunk_audio_path = align_dir / f"align_{idx:02d}.wav"
        subprocess.run([cfg.ffmpeg_path, "-y", "-i", str(scratch / "dub_master_final.wav"),
                         "-ss", f"{chunk_abs_start / 1000:.3f}", "-t", f"{chunk_dur / 1000:.3f}",
                         "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(chunk_audio_path)],
                        capture_output=True, check=True)

        cues = align.force_align_chunk(chunk_audio_path, zh_slice, en_slice, chunk_abs_start, chunk_dur)
        all_cues.extend(cues)
        print(f"chunk {idx}/{len(chunks)} aligned, {len(zh_slice)} cues")
        chunk_abs_start += chunk_dur

    clamped, overlaps = align.clamp_monotonic(all_cues)
    print(f"global monotonic clamp: {overlaps} overlap(s) fixed")
    align.write_aligned_srt(clamped, out["bilingual_cndub_srt"])
    print(f"wrote {len(clamped)} cues to {out['bilingual_cndub_srt']}")


def _align_dub_native(args, scratch):
    """Native mode: subtitles FROM the dub. Per passage, extract its span of
    the assembled track (offsets from native_finalize_log.json -- the same
    timeline finalize built, so cues can't slide off the voice) and force-align
    the dub-derived Chinese cue lines. English lines are empty by design:
    native-mode CN dub ships Chinese-only subs (the ENsub variant stays the
    bilingual option)."""
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir, args.version)
    inputs = [scratch / "dub_master_final.wav", scratch / "native_finalize_log.json",
              scratch / "zh_cues.json"]
    if not _stage_gate(args, [out["bilingual_cndub_srt"]], inputs):
        return
    cfg = get_config()
    fin_log = json.loads((scratch / "native_finalize_log.json").read_text(encoding="utf-8"))
    cue_groups = json.loads((scratch / "zh_cues.json").read_text(encoding="utf-8"))
    offsets = {e["anchor_id"]: e for e in fin_log["passages"]}

    align_dir = scratch / "align_passages"
    align_dir.mkdir(exist_ok=True)
    all_cues = []
    for group in cue_groups:
        aid = group["anchor_id"]
        entry = offsets[aid]
        start_ms = entry["placed_start_ms"]
        dur_ms = entry["speech_end_ms"] - start_ms
        span_path = align_dir / f"{aid}.wav"
        subprocess.run([cfg.ffmpeg_path, "-y", "-i", str(scratch / "dub_master_final.wav"),
                        "-ss", f"{start_ms / 1000:.3f}", "-t", f"{dur_ms / 1000:.3f}",
                        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(span_path)],
                       capture_output=True, check=True)
        zh_lines = group["lines"]
        cues = align.force_align_chunk(span_path, zh_lines, [""] * len(zh_lines), start_ms, dur_ms)
        all_cues.extend(cues)
        print(f"passage {aid}: {len(zh_lines)} cues aligned")

    clamped, overlaps = align.clamp_monotonic(all_cues)
    print(f"global monotonic clamp: {overlaps} overlap(s) fixed")
    align.write_aligned_srt(clamped, out["bilingual_cndub_srt"])
    print(f"wrote {len(clamped)} Chinese-only cues to {out['bilingual_cndub_srt']}")


def cmd_mode_set(args):
    """Set the per-project dub mode (native | cue_locked). Stored in
    runs/{id}/project.json so existing projects default to cue_locked."""
    scratch = _scratch(args.project_id)
    pj = scratch / "project.json"
    data = json.loads(pj.read_text(encoding="utf-8")) if pj.exists() else {}
    data["dub_mode"] = args.dub_mode
    pj.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"dub_mode = {args.dub_mode} ({pj})")


def cmd_mode_show(args):
    print(f"dub_mode = {_dub_mode(_scratch(args.project_id))}")


def cmd_anchors_detect(args):
    """Native mode: mechanical anchor CANDIDATES (scene cuts + English speech
    gaps). The operator picks the final anchors.json by hand -- see
    anchors.py's docstring for why selection is never automated."""
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) != "native":
        print("anchors detect is a native-mode stage -- run `mode set --dub-mode native` first")
        return
    out_path = scratch / "anchor_candidates.json"
    if not _stage_gate(args, [out_path], [scratch / "segments.json"]):
        return
    cfg = get_config()
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.effective_master(project_dir, scratch)
    segs = subtitles.load_segments(scratch / "segments.json")
    video_ms = round(render.probe_duration_ms(cfg.ffmpeg_path, master))
    print("detecting scene cuts (one decode pass, ~realtime/4) ...")
    cuts = anchors.detect_scene_cuts(cfg.ffmpeg_path, master)
    gaps = anchors.detect_speech_gaps(segs)
    out_path.write_text(json.dumps(
        {"video_ms": video_ms, "scene_cuts_ms": cuts, "speech_gaps": gaps},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}: {len(cuts)} scene cuts, {len(gaps)} speech gaps.")
    anchors_path = scratch / "anchors.json"
    if not anchors_path.exists():
        proposal = anchors.propose_anchors(video_ms, cuts, segs)
        anchors_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"auto-proposed {len(proposal['anchors'])} anchors -> {anchors_path}")
        print("Operator: review the proposal against the picture (must-sync moments: "
              "product shots, on-screen text, chapter turns), adjust, then `anchors validate`.")
    else:
        print(f"{anchors_path} already exists -- left untouched")


def cmd_anchors_validate(args):
    scratch = _scratch(args.project_id)
    path = scratch / "anchors.json"
    if not path.exists():
        sys.exit(f"{path} not found -- write it first (see anchor_candidates.json for suggestions)")
    data = json.loads(path.read_text(encoding="utf-8"))
    n_segs = None
    seg_path = scratch / "segments.json"
    if seg_path.exists():
        n_segs = len(subtitles.load_segments(seg_path))
    errors = anchors.validate_anchors(data, n_segments=n_segs)
    if errors:
        sys.exit("anchors.json problems:\n  " + "\n  ".join(errors))
    wins = anchors.windows(data)
    for w in wins:
        print(f"  {w['anchor_id']}: {w['start_ms'] / 1000:.1f}s -> {w['end_ms'] / 1000:.1f}s "
              f"({(w['end_ms'] - w['start_ms']) / 1000:.1f}s window)")
    print(f"OK: {len(wins)} windows")


def cmd_ingest_script(args):
    """Native mode: ingest the operator's zh_script.json (one natural
    spoken-Chinese paragraph per anchor window)."""
    scratch = _scratch(args.project_id)
    anchor_data = anchors.load_anchors(scratch / "anchors.json")
    src = Path(args.script_json)
    passages = dub_native.load_script(src, anchor_data)  # validates 1:1 + order
    shutil.copy(src, scratch / "zh_script.json")
    total = sum(len(p["text"]) for p in passages)
    print(f"ingested {len(passages)} passages ({total} chars)")


def cmd_split_zh_cues(args):
    """Native mode: derive subtitle cue lines from the Chinese script at
    natural sentence boundaries -> zh_cues.json (consumed by align-dub)."""
    scratch = _scratch(args.project_id)
    out_path = scratch / "zh_cues.json"
    if not _stage_gate(args, [out_path], [scratch / "zh_script.json"]):
        return
    anchor_data = anchors.load_anchors(scratch / "anchors.json")
    passages = dub_native.load_script(scratch / "zh_script.json", anchor_data)
    groups = [{"anchor_id": p["anchor_id"], "lines": subtitles.split_zh_cues(p["text"])}
              for p in passages]
    out_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    n = sum(len(g["lines"]) for g in groups)
    print(f"wrote {n} cue lines across {len(groups)} passages to {out_path}")


def cmd_dub_verify_anchors(args):
    """Native-mode close-out gate: prove from the assembled AUDIO that every
    passage starts within tolerance of its anchor and never bleeds past the
    next one. Run alongside `render verify`."""
    scratch = _scratch(args.project_id)
    if _dub_mode(scratch) != "native":
        print("verify-anchors is a native-mode gate; cue_locked projects use `render verify` alone")
        return
    anchor_data = anchors.load_anchors(scratch / "anchors.json")
    results = dub_native.verify_anchors(anchor_data, scratch)
    bad = [r for r in results if not r["ok"]]
    for r in results:
        mark = "ok " if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['anchor_id']}: onset drift {r['onset_drift_ms']:+d}ms, "
              f"slack {r['slack_ms']}ms")
    if bad:
        sys.exit(f"FAIL: {len(bad)} anchor(s) out of tolerance "
                 f"(±{dub_native.ANCHOR_TOLERANCE_MS}ms) -- diagnose before rendering")
    print(f"PASS: all {len(results)} anchors within ±{dub_native.ANCHOR_TOLERANCE_MS}ms")


def cmd_thumb_clean(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    cfg_path = scratch / "thumb_config.json"
    if not cfg_path.exists():
        sys.exit(f"{cfg_path} not found -- create it first (see thumbnail.py's schema docstring)")
    tc = thumbnail.load_thumb_config(cfg_path)
    out = scratch / "thumb_cleaned.png"
    if not _stage_gate(args, [out], [cfg_path, Path(tc["source_image"])]):
        return
    thumbnail.clean_source_thumbnail(
        Path(tc["source_image"]), tc["remove_text_description"], tc["scene_description"], out,
    )
    print(f"wrote {out}")


def cmd_thumb_render(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    out_paths = paths.deliverable_paths(project_dir)
    tc = thumbnail.load_thumb_config(scratch / "thumb_config.json")
    base = scratch / "thumb_cleaned.png" if tc.get("clean_first", True) else Path(tc["source_image"])
    if not _stage_gate(args, [out_paths["cover_jpg"]], [scratch / "thumb_config.json", base]):
        return
    thumbnail.render(base, tc, out_paths["cover_jpg"])
    print(f"wrote {out_paths['cover_jpg']}")


def cmd_render_ensub(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.effective_master(project_dir, scratch)
    out = paths.deliverable_paths(project_dir)
    if not _stage_gate(args, [out["ensub_mp4"]], [master, scratch / "bilingual_ensub.srt"]):
        return
    render.render_ensub(master, scratch / "bilingual_ensub.srt", out["ensub_mp4"], scratch / "render_ensub.log")
    print(f"wrote {out['ensub_mp4']} (video source: {master.name})")


def cmd_render_cndub(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.effective_master(project_dir, scratch)
    out = paths.deliverable_paths(project_dir, args.version)
    mixed_path = scratch / "dub_master_mixed.wav"
    zh_vo = mixed_path if mixed_path.exists() else scratch / "dub_master_padded.wav"
    if not _stage_gate(args, [out["cndub_mp4"]], [master, zh_vo, out["bilingual_cndub_srt"]]):
        return
    render.render_cndub(master, zh_vo, out["bilingual_cndub_srt"],
                         out["cndub_mp4"], scratch / "render_cndub.log")
    print(f"wrote {out['cndub_mp4']} (audio source: {zh_vo.name}, video source: {master.name})")


def cmd_publish_auth(args):
    """One-time YouTube sign-in for the Chinese channel. Run it with no flags to
    get the URL; sign in AS yellowdude.zh@gmail.com; re-run with --redirect-url
    to store the refresh token."""
    cfg = get_config()
    if not args.redirect_url:
        url = publish.build_authorize_url(cfg)
        print("1) Open this URL and sign in as the CHINESE channel account "
              "(yellowdude.zh@gmail.com -- not your own):\n")
        print(f"   {url}\n")
        print("2) After consent, the browser lands on a http://localhost/?code=... page")
        print("   that won't load -- copy the FULL URL from the address bar.")
        print("3) Re-run:  cn-pipeline publish auth --redirect-url '<paste it>'")
        return
    tok = publish.exchange_code_for_tokens(cfg, args.redirect_url)
    path = publish.save_refresh_token(tok["refresh_token"])
    print(f"Saved YOUTUBE_REFRESH_TOKEN to {path}. YouTube publish is ready.")


def cmd_publish_youtube(args):
    """Upload the CNdub to the Chinese YouTube channel as a PRIVATE draft and
    print the video link (goes into the Chinese DB's `CNdub YT link`)."""
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir, args.version)
    if not out["cndub_mp4"].exists():
        sys.exit(f"{out['cndub_mp4'].name} not found -- render it before publishing")
    if not args.title:
        sys.exit("--title is required (use the Notion row's V2/中配 title)")
    description = Path(args.description_file).read_text(encoding="utf-8") if args.description_file else ""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    result = publish.upload_youtube_draft(out["cndub_mp4"], args.title, description, tags)
    # Set the CN thumbnail too -- publishing without it was a review miss. Covers
    # aren't re-made per re-cut, so fall back to the unversioned one when a
    # --version render has no cover of its own.
    cover = out["cover_jpg"]
    if not cover.exists() and args.version:
        cover = paths.deliverable_paths(project_dir)["cover_jpg"]
    if cover.exists():
        thumb = publish.set_thumbnail(result["video_id"], cover)
        result["thumbnail"] = thumb["detail"]
        if not thumb["ok"]:
            print(f"WARNING: video uploaded but thumbnail failed -- {thumb['detail']}", file=sys.stderr)
    else:
        result["thumbnail"] = "no cover file found -- set it in Studio"
    print(json.dumps(result, indent=2))
    print(f"\nPrivate draft uploaded. Link for the Chinese DB's `CNdub YouTube` property:\n  {result['link']}")
    print("Flip it public in YouTube Studio when ready.")


def cmd_publish_bilibili(args):
    sys.exit(
        "Bilibili publish is not implemented yet -- the team is waiting on official "
        "API access from Bilibili. Once granted, this command will upload both the "
        "ENsub and CNdub drafts and print their BV links."
    )


def _require_screentext_enabled():
    if not get_config().screentext_enabled:
        sys.exit(
            "In-screen text localization is disabled (experimental). Set "
            '"screentext_enabled": true in config.json to try it. Renders '
            "use the raw master until then."
        )


def cmd_screentext_detect(args):
    _require_screentext_enabled()
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)  # detect on the RAW master, always
    events_json = scratch / "screentext" / "screentext_events.json"
    if not _stage_gate(args, [events_json], [master]):
        return
    result = screentext.detect_text_events(master, scratch)
    print(f"detected {result['event_count']} on-screen text event(s), "
          f"{result['unstable_count']} unstable (moving -- will be skipped + listed)")
    print(f"wrote {events_json}")
    print("Next: translate each event's `text` to Chinese (live judgment, glossary-checked, "
          "same as subtitles), write a JSON array of strings in the same order, then "
          "`screentext ingest-translation`. Review unstable events by eye -- their in-frame "
          "text moves, so a static patch can't cover them.")


def cmd_screentext_ingest_translation(args):
    _require_screentext_enabled()
    scratch = _scratch(args.project_id)
    data = screentext.load_events(scratch)
    zh = json.loads(Path(args.zh_json).read_text(encoding="utf-8"))
    n = len(data["events"])
    if len(zh) != n:
        sys.exit(f"translation has {len(zh)} lines, {n} text events detected -- must match 1:1")
    shutil.copy(args.zh_json, scratch / "screentext" / "screentext_zh.json")
    print(f"ingested {len(zh)} translated on-screen strings")


def cmd_screentext_localize(args):
    _require_screentext_enabled()
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)  # composite onto the RAW master
    st_dir = scratch / "screentext"
    out_path = paths.localized_master_path(scratch)
    inputs = [master, st_dir / "screentext_events.json", st_dir / "screentext_zh.json"]
    if not _stage_gate(args, [out_path], inputs):
        return
    data = screentext.load_events(scratch)
    zh = json.loads((st_dir / "screentext_zh.json").read_text(encoding="utf-8"))
    result = screentext.build_localized_master(
        master, data["events"], zh, data["frame_w"], data["frame_h"], scratch, out_path,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "skipped_unstable"}, indent=2))
    if result["skipped_unstable"]:
        print(f"\n{len(result['skipped_unstable'])} moving/unstable event(s) NOT localized "
              "-- static patch can't cover in-frame motion. Resolve by hand if they matter:")
        for s in result["skipped_unstable"]:
            print(f"  [{s['idx']}] @{s['start_ms']}ms drift={s['drift_frac']} "
                  f"\"{s['text']}\" -> \"{s['zh']}\"")
    if not result["localized"]:
        print("\nNo localized master written -- renders will use the raw master unchanged.")


def cmd_review_auth(args):
    """One-time Frame.io V4 sign-in (User Authentication). Without --redirect-url,
    prints the IMS authorize URL to open; with it, exchanges the returned code for
    a refresh token and saves it to .env so future runs authenticate unattended."""
    cfg = get_config()
    if not args.redirect_url:
        url = frameio.build_authorize_url(cfg)
        print("1) Open this URL in a browser, sign in, and approve access:\n")
        print(f"   {url}\n")
        print(f"2) Your browser will try to load {cfg.frameio_redirect_uri}?code=...")
        print("   (the page won't load -- that's fine; the code is in the address bar).")
        print("3) Copy the FULL redirected URL and re-run:\n")
        print("   cn-pipeline review auth --redirect-url '<paste the whole URL>'")
        return
    tok = frameio.exchange_code_for_tokens(cfg, args.redirect_url)
    path = frameio.save_refresh_token_to_env(tok["refresh_token"])
    print(f"Saved FRAMEIO_REFRESH_TOKEN to {path}.")
    print("Frame.io V4 auth is ready -- access tokens now refresh automatically.")


def cmd_review_submit(args):
    """Upload a cndub cut to Frame.io for native-speaker review. The first cut is
    shared on its own; each later --version is stacked as a NEW VERSION of the
    previous cut and shared as one compare link, so a reviewer can flip v1<->v2
    and check whether earlier comments are resolved. State lives in
    scratch/frameio_review.json so v3+ append to the same stack + share."""
    cfg = get_config()
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir, args.version)
    if not out["cndub_mp4"].exists():
        sys.exit(f"{out['cndub_mp4'].name} not found -- render it before submitting for review")
    scratch = _scratch(args.project_id)
    rec_path = scratch / "frameio_review.json"
    rec = (json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists()
           else {"versions": {}, "stack_id": None, "share_id": None, "review_link": None})
    label = args.version or "v1"

    account_id = frameio._account_id(cfg)
    prior_assets = list(rec["versions"].values())
    up = frameio.upload_file_for_review(out["cndub_mp4"])
    new_asset = up["asset_id"]
    rec["versions"][label] = new_asset

    # Fold the new cut into a version stack with the previous cut(s).
    if rec.get("stack_id"):
        frameio.add_to_version_stack(cfg, account_id, new_asset, rec["stack_id"])  # v3+
    elif prior_assets:
        folder = frameio._project_root_folder(cfg, account_id)
        rec["stack_id"] = frameio.create_version_stack(cfg, account_id, folder, prior_assets + [new_asset])
        # first stack: retire the single-file share so the stack gets its own
        if rec.get("share_id"):
            frameio.delete_share(cfg, account_id, rec["share_id"])
            rec["share_id"] = rec["review_link"] = None

    # One review share, on the stack when there is one; reuse it across versions
    # (a version stack's share auto-shows the newest version).
    target = rec.get("stack_id") or new_asset
    if not rec.get("share_id"):
        sid, link = frameio._create_review_share(cfg, account_id, target, f"CN dub review — {args.project_id}")
        rec["share_id"], rec["review_link"] = sid, link
    rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    link = rec.get("review_link") or up["view_url"]
    (scratch / "review_link.txt").write_text(link, encoding="utf-8")

    print(json.dumps({"version": label, "asset_id": new_asset,
                      "stack_id": rec.get("stack_id"), "review_link": link}, indent=2))
    print(f"\nReview link (paste into the Chinese DB's `Frame.io link` field, set Status: In review):\n  {link}")


def cmd_review_fetch(args):
    """Pull the reviewer's time-coded comments, resolve each to its cue, and
    classify into auto-fixable vs needs-human. Works live (--asset-id) or from
    an exported comments file (--comments-json)."""
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir)
    cues = frameio.parse_cndub_cues(out["bilingual_cndub_srt"])
    # Live V4 comments carry a framestamp, not seconds -- probe the local cndub's
    # fps to convert. (Frame.io's file object doesn't expose fps.) Offline exports
    # ignore fps.
    fps = None
    if not args.comments_json and out["cndub_mp4"].exists():
        fps = render.probe_fps(get_config().ffmpeg_path, out["cndub_mp4"])
    comments = frameio.fetch_comments(
        args.asset_id, Path(args.comments_json) if args.comments_json else None, fps=fps
    )
    report = frameio.build_review_report(comments, cues)
    (scratch / "review_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# Review report — {args.project_id}", "",
             f"{report['comment_count']} comment(s): "
             f"{report['auto_count']} auto-fixable, {report['human_count']} need a human", ""]
    if report["auto_fixable"]:
        lines.append("## Auto-fixable (term/typo with a concrete replacement)")
        for e in report["auto_fixable"]:
            lines.append(f"- cue {e['cue_idx']}: `{e['replacement']['old']}` → "
                         f"`{e['replacement']['new']}`  — {e['author']}: \"{e['text']}\"")
        lines.append("")
    if report["needs_human"]:
        lines.append("## Needs a human")
        for e in report["needs_human"]:
            loc = f"cue {e['cue_idx']}" if e["cue_idx"] else "unresolved"
            lines.append(f"- [{e['category']}] {loc}: \"{e['text']}\" — {e.get('human_reason','')}")
    (scratch / "review_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{report['comment_count']} comment(s): {report['auto_count']} auto-fixable, "
          f"{report['human_count']} need a human")
    print(f"wrote {scratch / 'review_report.json'} and review_report.md")


def cmd_review_apply(args):
    """Apply the auto-fixable term/typo swaps to zh.json (backing up the old
    one), and print the human queue + which stages to re-run."""
    scratch = _scratch(args.project_id)
    report = json.loads((scratch / "review_report.json").read_text(encoding="utf-8"))
    zh_path = scratch / "zh.json"
    zh = json.loads(zh_path.read_text(encoding="utf-8"))
    new_zh, changelog = frameio.apply_auto_fixes(report, zh)

    applied = [c for c in changelog if c["status"] == "applied"]
    if applied:
        shutil.copy(zh_path, scratch / "zh.pre_review.json")  # keep the pre-fix translation
        zh_path.write_text(json.dumps(new_zh, ensure_ascii=False, indent=2), encoding="utf-8")
    for c in changelog:
        print(json.dumps(c, ensure_ascii=False))
    print(f"\napplied {len(applied)} fix(es) to zh.json" if applied else "\nno auto-fixes applied")
    if applied:
        print("Re-run to propagate (SKIP_OK redoes only what's downstream of zh.json):")
        print("  cn-pipeline subtitles build-srt --project-id {id}")
        print("  cn-pipeline dub generate --project-id {id}   # text cache re-buys only changed chunks")
        print("  ...then dub finalize -> tighten -> mix-me -> align align-dub -> render (ensub/cndub/verify)")
    if report["needs_human"]:
        print(f"\n{report['human_count']} comment(s) still need a human (see review_report.md) -- "
              "pacing/sync and anything without a concrete replacement are never auto-applied.")


def cmd_render_verify(args):
    """Stage 5 close-out gate: both rendered files must match the source
    duration within render.DURATION_TOLERANCE_MS, or the run isn't done."""
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    out = paths.deliverable_paths(project_dir)
    results = render.verify_outputs(master, [out["ensub_mp4"], out["cndub_mp4"]])
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(json.dumps(r))
    if failed:
        sys.exit(
            f"FAIL: {', '.join(r['file'] for r in failed)} -- duration off by more than "
            f"{render.DURATION_TOLERANCE_MS}ms (or missing). Something upstream broke; "
            "diagnose it, don't re-render-and-hope (cn_workflow.html Stage 5)."
        )
    print(f"PASS: both outputs within {render.DURATION_TOLERANCE_MS}ms of source duration")


def main():
    p = argparse.ArgumentParser(prog="cn_pipeline")
    sub = p.add_subparsers(dest="group", required=True)

    def add(group_parsers, name, fn):
        sub_p = group_parsers.add_parser(name)
        sub_p.add_argument("--project-id", required=True)
        sub_p.add_argument("--force", action="store_true",
                           help="redo this stage even if its outputs are up to date "
                                "(downstream stages then rerun automatically -- their "
                                "inputs become newer than their outputs)")
        sub_p.add_argument("--version", default="",
                           help="revision suffix for deliverables (e.g. v2), so a review "
                                "re-cut writes {id}_cndub_v2.mp4 without overwriting v1")
        sub_p.set_defaults(func=fn)
        return sub_p

    preflight_p = sub.add_parser("preflight")
    preflight_p.add_argument("--project-id", required=True)
    preflight_p.set_defaults(func=cmd_preflight)

    mode_group = sub.add_parser("mode").add_subparsers(dest="cmd", required=True)
    mode_set = add(mode_group, "set", cmd_mode_set)
    mode_set.add_argument("--dub-mode", required=True, dest="dub_mode",
                          choices=["cue_locked", "native"],
                          help="cue_locked = classic English-cue-timed dub; native = "
                               "dub-first at natural pace, anchor-synced (see dub_native.py)")
    add(mode_group, "show", cmd_mode_show)

    align_group = sub.add_parser("align").add_subparsers(dest="cmd", required=True)
    add(align_group, "extract-audio", cmd_extract_audio)
    add(align_group, "transcribe", cmd_transcribe)
    add(align_group, "align-dub", cmd_align_dub)

    anchors_group = sub.add_parser("anchors").add_subparsers(dest="cmd", required=True)
    add(anchors_group, "detect", cmd_anchors_detect)
    add(anchors_group, "validate", cmd_anchors_validate)

    subs_group = sub.add_parser("subtitles").add_subparsers(dest="cmd", required=True)
    add(subs_group, "split-cues", cmd_split_cues)
    ingest = add(subs_group, "ingest-translation", cmd_ingest_translation)
    ingest.add_argument("--zh-json", required=True, dest="zh_json")
    add(subs_group, "build-srt", cmd_build_srt)
    ingest_script = add(subs_group, "ingest-script", cmd_ingest_script)
    ingest_script.add_argument("--script-json", required=True, dest="script_json")
    add(subs_group, "split-zh-cues", cmd_split_zh_cues)

    dub_group = sub.add_parser("dub").add_subparsers(dest="cmd", required=True)
    add(dub_group, "generate", cmd_dub_generate)
    add(dub_group, "fix", cmd_dub_fix)
    add(dub_group, "finalize", cmd_dub_finalize)
    add(dub_group, "tighten", cmd_dub_tighten)
    add(dub_group, "mix-me", cmd_dub_mix_me)
    add(dub_group, "verify-anchors", cmd_dub_verify_anchors)

    thumb_group = sub.add_parser("thumbnail").add_subparsers(dest="cmd", required=True)
    add(thumb_group, "clean", cmd_thumb_clean)
    add(thumb_group, "render", cmd_thumb_render)

    st_group = sub.add_parser("screentext").add_subparsers(dest="cmd", required=True)
    add(st_group, "detect", cmd_screentext_detect)
    st_ingest = add(st_group, "ingest-translation", cmd_screentext_ingest_translation)
    st_ingest.add_argument("--zh-json", required=True, dest="zh_json")
    add(st_group, "localize", cmd_screentext_localize)

    render_group = sub.add_parser("render").add_subparsers(dest="cmd", required=True)
    add(render_group, "ensub", cmd_render_ensub)
    add(render_group, "cndub", cmd_render_cndub)
    add(render_group, "verify", cmd_render_verify)

    review_group = sub.add_parser("review").add_subparsers(dest="cmd", required=True)
    # `auth` is global setup (no project-id), so it's registered directly.
    review_auth = review_group.add_parser("auth")
    review_auth.add_argument("--redirect-url", dest="redirect_url", default=None)
    review_auth.set_defaults(func=cmd_review_auth)
    add(review_group, "submit", cmd_review_submit)
    review_fetch = add(review_group, "fetch", cmd_review_fetch)
    review_fetch.add_argument("--asset-id", dest="asset_id", default=None)
    review_fetch.add_argument("--comments-json", dest="comments_json", default=None)
    add(review_group, "apply", cmd_review_apply)

    publish_group = sub.add_parser("publish").add_subparsers(dest="cmd", required=True)
    publish_auth = publish_group.add_parser("auth")
    publish_auth.add_argument("--redirect-url", dest="redirect_url", default=None)
    publish_auth.set_defaults(func=cmd_publish_auth)
    publish_yt = add(publish_group, "youtube", cmd_publish_youtube)
    publish_yt.add_argument("--title", default=None, help="video title (the Notion row's 中配 title)")
    publish_yt.add_argument("--description-file", dest="description_file", default=None,
                            help="path to a file holding the CN description")
    publish_yt.add_argument("--tags", default=None, help="comma-separated CN tags")
    publish_bili = publish_group.add_parser("bilibili")
    publish_bili.set_defaults(func=cmd_publish_bilibili)

    args = p.parse_args()
    try:
        args.func(args)
    except ConfigError as e:
        sys.exit(f"Config error: {e}")


if __name__ == "__main__":
    main()
