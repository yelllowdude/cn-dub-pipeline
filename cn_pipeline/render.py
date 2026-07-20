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

# CN dub subtitle style, per native-speaker review feedback: block sits lower
# (MarginV 30, was 50) so it blocks the visuals less; the Chinese line stays at
# the base size and the English line burns smaller (see _english_smaller_srt),
# keeping each language to a single line.
CNDUB_SUBTITLE_STYLE = (
    "FontName=PingFang SC,FontSize=20,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=30"
)
ENGLISH_LINE_FONTSIZE = 14


def _english_smaller_srt(srt_in: Path, srt_out: Path, en_fs: int = ENGLISH_LINE_FONTSIZE) -> Path:
    """Rewrite a bilingual srt (zh line 1, en line 2) so the English line burns
    at a smaller size via an inline ASS override tag, without changing the base
    style. Cues with only one text line are left untouched."""
    blocks = [b for b in srt_in.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()]
    out = []
    for b in blocks:
        lines = b.split("\n")
        if len(lines) >= 4 and not lines[3].lstrip().startswith("{\\fs"):
            lines[3] = "{\\fs%d}%s" % (en_fs, lines[3])
        out.append("\n".join(lines))
    srt_out.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    return srt_out


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


def render_cndub(master_video: Path, zh_vo_wav: Path, bilingual_cndub_srt: Path, out_path: Path, log_path: Path) -> Path:
    cfg = get_config()
    # Burn the CN dub subtitles with the review-adjusted style: block lower
    # (CNDUB_SUBTITLE_STYLE) and the English line smaller (inline override in a
    # styled copy of the srt, alongside the output so runs don't collide).
    styled_srt = out_path.with_name(out_path.stem + "_styled.srt")
    _english_smaller_srt(bilingual_cndub_srt, styled_srt)
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(master_video), "-i", str(zh_vo_wav),
        "-map", "0:v", "-map", "1:a",
        "-vf", f"subtitles={styled_srt}:force_style='{CNDUB_SUBTITLE_STYLE}'",
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
