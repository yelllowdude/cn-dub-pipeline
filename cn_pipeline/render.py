"""
Stage 5: final render. Burns the bilingual subtitles onto the master video,
producing the two deliverables:
    {id}_ensub.mp4 = master video + original English audio + bilingual_ensub.srt burned
    {id}_cndub.mp4 = master video + Chinese dub audio + bilingual_cndub.srt burned
                     (the forced-aligned copy -- never the English-timed one)

No prior standalone script existed for this stage (it was run as ad-hoc
ffmpeg commands in-session) -- written fresh here from the exact invocation
used and verified against 100-body-squats_2026-04-11 (output durations
matched the source to within ~0.02s).

Requires ffmpeg-full (libass for subtitle burn-in, videotoolbox for hardware
encoding on Apple Silicon) -- see cn_pipeline.config.
"""

import subprocess
from pathlib import Path

from cn_pipeline.config import get_config

SUBTITLE_STYLE = (
    "FontName=PingFang SC,FontSize=20,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=50"
)

# CN dub subtitles, per native-speaker review feedback: each language on ONE
# line, the English line smaller than the Chinese, and the block lower so it
# blocks the visuals less. SRT + force_style can't size the two lines
# differently (ffmpeg's SRT reader strips inline {\fs} overrides), so the CN dub
# burns a generated .ass instead, where libass honours per-line overrides and
# WrapStyle=2 keeps each line unwrapped. Fractions are of the video height so it
# holds at any resolution.
CNDUB_ZH_FONT_FRAC = 0.0675     # Chinese line height ~= 6.75% of frame height
                                # (was 0.045; +50% per review — bigger, more legible)
CNDUB_EN_FONT_RATIO = 0.66      # English line ~= 66% of the Chinese size (scales with it)
CNDUB_MARGIN_V_FRAC = 0.04      # baseline ~= 4% of frame height off the bottom.
                                # The block is bottom-anchored (Alignment=2 + this fixed
                                # margin), so a bigger font grows the block UPWARD while the
                                # bottom edge stays put — subtitles never creep further down.


def _srt_time_to_ass(t: str) -> str:
    """'00:02:36,806' -> '0:02:36.80' (ASS uses centiseconds)."""
    hh, mm, rest = t.strip().split(":")
    ss, ms = rest.split(",")
    return f"{int(hh)}:{mm}:{ss}.{int(ms) // 10:02d}"


def build_cndub_ass(bilingual_srt: Path, ass_out: Path, video_w: int, video_h: int) -> Path:
    """Generate an .ass for the CN dub from the bilingual srt (zh line 1, en line
    2). One ZH style at CNDUB_ZH_FONT_FRAC of the height; the English line gets an
    inline {\\fs} at CNDUB_EN_FONT_RATIO of that. WrapStyle=2 => no auto-wrap, so
    each language stays a single line."""
    zh_fs = round(video_h * CNDUB_ZH_FONT_FRAC)
    en_fs = round(zh_fs * CNDUB_EN_FONT_RATIO)
    margin_v = round(video_h * CNDUB_MARGIN_V_FRAC)
    outline = max(2, round(video_h * 0.0018))
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\nPlayResY: {video_h}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: ZH,PingFang SC,{zh_fs},&H00FFFFFF,&H00000000,1,{outline},0,2,60,60,{margin_v}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for block in [b for b in Path(bilingual_srt).read_text(encoding="utf-8").strip().split("\n\n") if b.strip()]:
        ls = block.split("\n")
        if len(ls) < 2 or " --> " not in ls[1]:
            continue
        start, end = ls[1].split(" --> ")
        zh = ls[2] if len(ls) > 2 else ""
        en = ls[3] if len(ls) > 3 else ""
        text = zh + (f"\\N{{\\fs{en_fs}}}{en}" if en else "")
        events.append(f"Dialogue: 0,{_srt_time_to_ass(start)},{_srt_time_to_ass(end)},ZH,,0,0,0,,{text}")
    Path(ass_out).write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_out


def _run(cmd: list[str], log_path: Path) -> None:
    with open(log_path, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (see {log_path}): {' '.join(cmd)}")


def render_ensub(master_video: Path, bilingual_ensub_srt: Path, out_path: Path, log_path: Path) -> Path:
    cfg = get_config()
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(master_video),
        "-vf", f"subtitles={bilingual_ensub_srt}:force_style='{SUBTITLE_STYLE}'",
        "-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "copy",
        str(out_path),
    ]
    _run(cmd, log_path)
    return out_path


def probe_dimensions(cfg_ffmpeg_path: str, video_path: Path) -> tuple[int, int]:
    """(width, height) of the video's first stream."""
    ffprobe = str(Path(cfg_ffmpeg_path).with_name("ffprobe"))
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(video_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def render_cndub(master_video: Path, zh_vo_wav: Path, bilingual_cndub_srt: Path, out_path: Path, log_path: Path) -> Path:
    cfg = get_config()
    # Burn the CN dub subtitles from a generated .ass (one line per language, the
    # English line smaller, block low) -- see build_cndub_ass. The .ass sits in
    # the scratch dir (no spaces) to keep the ffmpeg filtergraph path clean.
    w, h = probe_dimensions(cfg.ffmpeg_path, master_video)
    ass = log_path.with_name(out_path.stem + ".ass")
    build_cndub_ass(bilingual_cndub_srt, ass, w, h)
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(master_video), "-i", str(zh_vo_wav),
        "-map", "0:v", "-map", "1:a",
        "-vf", f"subtitles={ass}",
        "-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_path),
    ]
    _run(cmd, log_path)
    return out_path


DURATION_TOLERANCE_MS = 100  # "within ~0.1s" per cn_workflow.html Stage 5


def verify_outputs(master_video: Path, outputs: list[Path]) -> list[dict]:
    """The Stage 5 close-out gate: both rendered files' durations must match
    the source video within DURATION_TOLERANCE_MS. A bigger mismatch means
    something upstream broke -- not something to re-render-and-hope past.
    Previously a manual "confirm both durations" instruction in SKILL.md;
    this makes it one command anyone can run and trust."""
    cfg = get_config()
    src_ms = probe_duration_ms(cfg.ffmpeg_path, master_video)
    results = []
    for p in outputs:
        if not p.exists():
            results.append({"file": p.name, "ok": False, "reason": "missing",
                            "source_ms": round(src_ms)})
            continue
        dur_ms = probe_duration_ms(cfg.ffmpeg_path, p)
        delta_ms = dur_ms - src_ms
        results.append({
            "file": p.name, "ok": abs(delta_ms) <= DURATION_TOLERANCE_MS,
            "duration_ms": round(dur_ms), "source_ms": round(src_ms),
            "delta_ms": round(delta_ms),
        })
    return results


def probe_duration_ms(cfg_ffmpeg_path: str, video_path: Path) -> float:
    # swap just the binary name, not a blanket string replace -- ffmpeg-full's
    # own directory name also contains "ffmpeg" and would get mangled otherwise
    ffprobe = str(Path(cfg_ffmpeg_path).with_name("ffprobe"))
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip()) * 1000


def probe_fps(cfg_ffmpeg_path: str, video_path: Path) -> float | None:
    """Frames per second as a float, or None if it can't be read. Frame.io
    comment timestamps are framestamps, so review-fetch needs this to convert
    them to milliseconds. r_frame_rate comes back as a rational like '30000/1001'."""
    ffprobe = str(Path(cfg_ffmpeg_path).with_name("ffprobe"))
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        raw = result.stdout.strip()
        if "/" in raw:
            num, den = raw.split("/", 1)
            return float(num) / float(den) if float(den) else None
        return float(raw) if raw else None
    except (subprocess.CalledProcessError, ValueError, ZeroDivisionError):
        return None
