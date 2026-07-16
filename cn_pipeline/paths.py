"""
Project + file path resolution.

Drive layout (confirmed against real projects, not guessed):
    {drive_root}/_videos/youtube-longform/{project_id}/{project_id}_*.mp4   <- master video candidates
    {drive_root}/_videos/youtube-longform/{project_id}/CN/                  <- all CN deliverables go here

Local scratch data (per-run tuning, not checked into git) lives under
    {repo_root}/runs/{project_id}/
"""

from pathlib import Path

from cn_pipeline.config import get_config, REPO_ROOT

YOUTUBE_LONGFORM = "_videos/youtube-longform"


class ProjectNotFoundError(RuntimeError):
    pass


def resolve_project_dir(project_id: str) -> Path:
    cfg = get_config()
    base = cfg.drive_root / YOUTUBE_LONGFORM
    candidate = base / project_id
    if candidate.is_dir():
        return candidate

    matches = sorted(p for p in base.glob(f"{project_id}*") if p.is_dir())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ProjectNotFoundError(
            f"Ambiguous project id '{project_id}' -- multiple folders matched under {base}: "
            + ", ".join(m.name for m in matches)
        )
    raise ProjectNotFoundError(
        f"No project folder found for '{project_id}' under {base}. "
        "Check the project ID matches the Notion page exactly."
    )


def find_master_video(project_dir: Path) -> Path:
    candidates = sorted(project_dir.glob(f"{project_dir.name}*.mp4"))
    # exclude anything already inside a CN/ output subfolder
    candidates = [c for c in candidates if c.parent == project_dir]
    if not candidates:
        raise ProjectNotFoundError(f"No master video (*.mp4) found directly in {project_dir}")
    if len(candidates) > 1:
        # prefer the most recently modified, matching the convention used for
        # duplicate exports (e.g. "_1-video.mp4" vs "_1-video_2.mp4")
        candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]


def cn_output_dir(project_dir: Path) -> Path:
    out = project_dir / "CN"
    out.mkdir(exist_ok=True)
    return out


def me_wav_path(project_dir: Path) -> Path:
    return project_dir / f"{project_dir.name}_me.wav"


def run_scratch_dir(project_id: str) -> Path:
    d = REPO_ROOT / "runs" / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def deliverable_paths(project_dir: Path) -> dict:
    """Standard output filenames per cn_workflow.html's Drive structure convention."""
    pid = project_dir.name
    out = cn_output_dir(project_dir)
    return {
        "master": out / f"{pid}_master.mp4",
        "en_srt": out / f"{pid}_en.srt",
        "zh_srt": out / f"{pid}_zh.srt",
        "bilingual_ensub_srt": out / f"{pid}_bilingual_ensub.srt",
        "bilingual_cndub_srt": out / f"{pid}_bilingual_cndub.srt",
        "zh_vo_wav": out / f"{pid}_zh_vo.wav",
        "ensub_mp4": out / f"{pid}_ensub.mp4",
        "cndub_mp4": out / f"{pid}_cndub.mp4",
        "cover_jpg": out / f"{pid}_cover.jpg",
        "publish_kit": out / "publish_kit.md",
        "run_log": out / "run_log.md",
    }
