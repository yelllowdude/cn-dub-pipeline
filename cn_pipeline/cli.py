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

from cn_pipeline import align, dub, paths, render, subtitles, thumbnail
from cn_pipeline.config import ConfigError, get_config


def _scratch(project_id: str) -> Path:
    return paths.run_scratch_dir(project_id)


def cmd_preflight(args):
    cfg = get_config()  # raises ConfigError loudly if anything's missing
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    me_wav = paths.me_wav_path(project_dir)
    print(f"project dir: {project_dir}")
    print(f"master video: {master}")
    print(f"me.wav present: {me_wav.exists()} ({me_wav if me_wav.exists() else 'not found, dub ships without a background bed'})")
    print(f"ffmpeg: {cfg.ffmpeg_path}")
    print("Pre-flight OK. Proceed to translation (live judgment -- see SKILL.md), then `subtitles ingest-translation`.")


def cmd_extract_audio(args):
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    scratch = _scratch(args.project_id)
    out = scratch / "audio_16k.wav"
    align.extract_audio_16k(master, out)
    print(f"wrote {out}")


def cmd_transcribe(args):
    scratch = _scratch(args.project_id)
    audio = scratch / "audio_16k.wav"
    if not audio.exists():
        sys.exit(f"{audio} not found -- run `align extract-audio` first")
    out_srt = scratch / "whisper_raw.srt"
    align.transcribe_to_srt(audio, out_srt)
    print(f"wrote {out_srt}")


def cmd_split_cues(args):
    scratch = _scratch(args.project_id)
    raw_srt = scratch / "whisper_raw.srt"
    if not raw_srt.exists():
        sys.exit(f"{raw_srt} not found -- run `align transcribe` first")
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
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    subtitles.build_bilingual_srt(segs, zh, scratch / "bilingual_ensub.srt")
    subtitles.build_mono_srt(segs, zh, out["en_srt"], out["zh_srt"])
    print(f"wrote {scratch / 'bilingual_ensub.srt'}, {out['en_srt']}, {out['zh_srt']}")


def cmd_dub_generate(args):
    scratch = _scratch(args.project_id)
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    log = dub.generate(segs, zh, scratch)
    capped = log["capped_chunks"]
    if capped:
        print(f"{len(capped)} chunk(s) hit the tempo cap: {capped} -- run `dub fix` next")
    else:
        print("no chunks capped -- skip `dub fix`, go straight to `dub finalize`")


def cmd_dub_fix(args):
    scratch = _scratch(args.project_id)
    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    dub.fix_overflow_chunks(segs, zh, scratch, gen_log["capped_chunks"])
    print("re-split + regenerated capped chunks. Run `dub finalize` next.")


def cmd_dub_finalize(args):
    scratch = _scratch(args.project_id)
    segs = subtitles.load_segments(scratch / "segments.json")
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    out = dub.finalize(segs, scratch, gen_log["capped_chunks"])
    print(f"wrote {out}")


def cmd_dub_tighten(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    cfg = get_config()
    src_dur_ms = render.probe_duration_ms(cfg.ffmpeg_path, master)
    result = dub.tighten(scratch / "dub_master_final.wav", src_dur_ms, scratch / "dub_master_padded.wav")
    print(json.dumps(result, indent=2))
    if result["pad_ms"] > 3000:
        print(f"NOTE: padded {result['pad_ms']}ms of trailing silence -- confirm this matches "
              f"a real trailing pause in the source (check the tail isn't actually untranscribed "
              f"speech) before trusting it, per the last-chunk exception rule in cn_workflow.html Stage 4.")


def cmd_align_dub(args):
    """Forced-align the finished dub audio back onto cue text -> bilingual_cndub.srt."""
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    out = paths.deliverable_paths(project_dir)
    cfg = get_config()

    segs = subtitles.load_segments(scratch / "segments.json")
    zh = json.loads((scratch / "zh.json").read_text(encoding="utf-8"))
    gen_log = json.loads((scratch / "generate_log.json").read_text())
    finalize_log = json.loads((scratch / "finalize_log.json").read_text())
    actual_ms = {c["idx"]: c["final_ms"] for c in finalize_log["chunks"]}

    chunks = dub._chunk_segments(segs, dub.CHUNK_SIZE)
    all_cues = []
    chunk_abs_start = 0
    align_dir = scratch / "align_chunks"
    align_dir.mkdir(exist_ok=True)

    for c in chunks:
        idx = c["idx"]
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


def cmd_thumb_clean(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    cfg_path = scratch / "thumb_config.json"
    if not cfg_path.exists():
        sys.exit(f"{cfg_path} not found -- create it first (see thumbnail.py's schema docstring)")
    tc = thumbnail.load_thumb_config(cfg_path)
    out = scratch / "thumb_cleaned.png"
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
    thumbnail.render(base, tc, out_paths["cover_jpg"])
    print(f"wrote {out_paths['cover_jpg']}")


def cmd_render_ensub(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    out = paths.deliverable_paths(project_dir)
    render.render_ensub(master, scratch / "bilingual_ensub.srt", out["ensub_mp4"], scratch / "render_ensub.log")
    print(f"wrote {out['ensub_mp4']}")


def cmd_render_cndub(args):
    scratch = _scratch(args.project_id)
    project_dir = paths.resolve_project_dir(args.project_id)
    master = paths.find_master_video(project_dir)
    out = paths.deliverable_paths(project_dir)
    render.render_cndub(master, scratch / "dub_master_padded.wav", out["bilingual_cndub_srt"],
                         out["cndub_mp4"], scratch / "render_cndub.log")
    print(f"wrote {out['cndub_mp4']}")


def main():
    p = argparse.ArgumentParser(prog="cn_pipeline")
    sub = p.add_subparsers(dest="group", required=True)

    def add(group_parsers, name, fn):
        sub_p = group_parsers.add_parser(name)
        sub_p.add_argument("--project-id", required=True)
        sub_p.set_defaults(func=fn)
        return sub_p

    preflight_p = sub.add_parser("preflight")
    preflight_p.add_argument("--project-id", required=True)
    preflight_p.set_defaults(func=cmd_preflight)

    align_group = sub.add_parser("align").add_subparsers(dest="cmd", required=True)
    add(align_group, "extract-audio", cmd_extract_audio)
    add(align_group, "transcribe", cmd_transcribe)
    add(align_group, "align-dub", cmd_align_dub)

    subs_group = sub.add_parser("subtitles").add_subparsers(dest="cmd", required=True)
    add(subs_group, "split-cues", cmd_split_cues)
    ingest = add(subs_group, "ingest-translation", cmd_ingest_translation)
    ingest.add_argument("--zh-json", required=True, dest="zh_json")
    add(subs_group, "build-srt", cmd_build_srt)

    dub_group = sub.add_parser("dub").add_subparsers(dest="cmd", required=True)
    add(dub_group, "generate", cmd_dub_generate)
    add(dub_group, "fix", cmd_dub_fix)
    add(dub_group, "finalize", cmd_dub_finalize)
    add(dub_group, "tighten", cmd_dub_tighten)

    thumb_group = sub.add_parser("thumbnail").add_subparsers(dest="cmd", required=True)
    add(thumb_group, "clean", cmd_thumb_clean)
    add(thumb_group, "render", cmd_thumb_render)

    render_group = sub.add_parser("render").add_subparsers(dest="cmd", required=True)
    add(render_group, "ensub", cmd_render_ensub)
    add(render_group, "cndub", cmd_render_cndub)

    args = p.parse_args()
    try:
        args.func(args)
    except ConfigError as e:
        sys.exit(f"Config error: {e}")


if __name__ == "__main__":
    main()
