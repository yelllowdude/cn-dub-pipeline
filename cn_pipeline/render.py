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
    cmd = [
        cfg.ffmpeg_path, "-y", "-i", str(master_video), "-i", str(zh_vo_wav),
        "-map", "0:v", "-map", "1:a",
        "-vf", f"subtitles={bilingual_cndub_srt}:force_style='{SUBTITLE_STYLE}'",
        "-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_path),
    ]
    _run(cmd, log_path)
    return out_path


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
