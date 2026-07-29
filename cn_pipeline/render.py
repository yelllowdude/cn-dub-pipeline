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

Requires an ffmpeg with libass (subtitle burn-in silently no-ops without it)
and an H.264 encoder -- see cn_pipeline.config. The encoder is chosen per
machine by video_encoder_args(): h264_videotoolbox where it exists (hardware,
Apple Silicon), libx264 otherwise, so this runs off macOS too.
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
        # Locate the timing line instead of assuming it's ls[1]: a cue with an
        # EMPTY English line (native mode ships Chinese-only subs) ends in
        # "zh\n\n", which makes the block separator swallow the blank line and
        # the NEXT block arrive with a leading "\n" -- positional indexing then
        # mistakes the index row for the timing row and drops the cue.
        ls = block.split("\n")
        ti = next((i for i, l in enumerate(ls) if " --> " in l), None)
        if ti is None:
            continue
        start, end = ls[ti].split(" --> ")
        zh = ls[ti + 1] if len(ls) > ti + 1 else ""
        en = ls[ti + 2] if len(ls) > ti + 2 else ""
        text = zh + (f"\\N{{\\fs{en_fs}}}{en}" if en else "")
        events.append(f"Dialogue: 0,{_srt_time_to_ass(start)},{_srt_time_to_ass(end)},ZH,,0,0,0,,{text}")
    Path(ass_out).write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_out


def _run(cmd: list[str], log_path: Path) -> None:
    with open(log_path, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (see {log_path}): {' '.join(cmd)}")


_encoder_cache: dict[str, list[str]] = {}


def video_encoder_args(ffmpeg_path: str) -> list[str]:
    """H.264 encoder flags for this machine.

    h264_videotoolbox is macOS-only. Hardcoding it meant a Linux or Windows
    teammate could not render at all -- the pipeline was silently
    single-platform. Prefer the hardware encoder where it exists (it's several
    times faster on Apple Silicon, and these are 4K masters), fall back to
    libx264 everywhere else.

    The two aren't bit-identical, but they're deliberately matched for
    perceptual quality rather than bitrate: videotoolbox is bitrate-driven
    (20M), libx264 is quality-driven (CRF 18 is visually transparent for this
    material). Duration and timing -- the things `render verify` and the anchor
    checks care about -- are unaffected by the choice.
    """
    if ffmpeg_path in _encoder_cache:
        return _encoder_cache[ffmpeg_path]
    try:
        enc = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError):
        enc = ""
    if "h264_videotoolbox" in enc:
        args = ["-c:v", "h264_videotoolbox", "-b:v", "20M"]
    elif "libx264" in enc:
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        raise RuntimeError(
            f"{ffmpeg_path} has neither h264_videotoolbox nor libx264, so it cannot "
            "encode H.264. Install an ffmpeg with libx264 (most distro builds have it; "
            "on macOS `brew install ffmpeg-full`). Run `cn-pipeline doctor` to confirm."
        )
    _encoder_cache[ffmpeg_path] = args
    return args


def render_ensub(master_video: Path, bilingual_ensub_srt: Path, out_path: Path, log_path: Path) -> Path:
    cfg = get_config()
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(master_video),
        "-vf", f"subtitles={bilingual_ensub_srt}:force_style='{SUBTITLE_STYLE}'",
        *video_encoder_args(cfg.ffmpeg_path), "-c:a", "copy",
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
        *video_encoder_args(cfg.ffmpeg_path), "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_path),
    ]
    _run(cmd, log_path)
    return out_path


REVIEW_PROXY_HEIGHT = 1080
REVIEW_PROXY_CRF = 23           # visually clean at 1080p; not a deliverable
# videotoolbox is bitrate-driven, not CRF-driven. 3 Mbps is the floor that
# still keeps burned subtitle edges clean at 1080p -- the reviewer is reading
# text, so mushy glyphs are the one artifact that would actually cost us.
# (Measured: 5M produced a 507 MB proxy from a 1.4 GB master -- barely worth
# uploading; 3M lands near 300 MB with no visible difference on this footage.)
REVIEW_PROXY_BITRATE = "3M"


def render_review_proxy(cndub_mp4: Path, out_path: Path, log_path: Path) -> Path:
    """A 1080p copy of the CN dub for native-speaker review.

    Review used to upload the 4K deliverable itself -- 1.3 GB for a 10-minute
    video. The reviewer is judging translation, register and lip-sync, none of
    which 4K carries: burned subtitles are sized as a fraction of frame height
    so they stay exactly as legible, and timing is untouched. A proxy is ~10x
    smaller, which turns a long upload (and a slow scrub for the reviewer, who
    is often not on a fast connection) into a short one.

    Timing is preserved exactly -- no -shortest, no re-timing, video and audio
    both copied through at the same duration -- because review comments are
    framestamps that `review fetch` maps back onto the real cues.
    """
    cfg = get_config()
    w, h = probe_dimensions(cfg.ffmpeg_path, cndub_mp4)
    if h <= REVIEW_PROXY_HEIGHT:
        # Already 1080p or smaller: re-encoding would only lose quality.
        return cndub_mp4
    args = video_encoder_args(cfg.ffmpeg_path)
    if "libx264" in args:
        args = ["-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(REVIEW_PROXY_CRF), "-pix_fmt", "yuv420p"]
    else:
        args = ["-c:v", "h264_videotoolbox", "-b:v", REVIEW_PROXY_BITRATE]
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(cndub_mp4),
        # -2 keeps the width even (H.264 requires it) and the aspect ratio exact
        "-vf", f"scale=-2:{REVIEW_PROXY_HEIGHT}",
        *args, "-c:a", "copy",
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
