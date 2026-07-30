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
    hint = ("Run `cn-pipeline drive pull --project-id ...` to fetch it from Drive first."
            if cfg.storage == "gdrive"
            else "Check the project ID matches the Notion page exactly.")
    raise ProjectNotFoundError(
        f"No project folder found for '{project_id}' under {base}. {hint}"
    )


# Editors export masters as .mov as often as .mp4 (both are ISO-BMFF carrying
# H.264 here, so ffmpeg reads either identically -- the extension is the only
# difference the pipeline ever saw).
MASTER_EXTS = (".mp4", ".mov")

# Deliverables the pipeline itself writes into the project root or CN/. A
# rendered output must never be mistaken for the source master on a re-run.
_OUTPUT_MARKERS = ("_cndub", "_ensub", "_master", "_zh_vo", "_proxy")

# Subfolders editors file the finished program under when they don't leave it in
# the root: `video/`, `longform/`, `Final Video/`, `Videos/Final/`. Consulted ONLY
# when the root holds no candidate at all, and deliberately an allowlist rather
# than "any subfolder" -- the same projects also carry `staging/`,
# `multi-images/`, `Thumbnails/` and `Videos/Drafts/` full of b-roll, screen
# recordings and draft cuts (one of those drafts is a 75MB sponsorship-only
# segment). Adopting one of those as the master would be silent and wrong, so an
# unrecognized layout keeps failing loudly instead of guessing.
_DELIVERY_SUBDIRS = ("video", "videos", "longform", "final", "export", "exports",
                     "master", "masters", "final video", "final videos",
                     "final cut", "finals")

# How deep the allowlisted descent goes. Real layouts nest one more level
# (`Videos/Final/`), and every step must itself be an allowlisted name -- which is
# what keeps `Videos/Drafts/` out while letting `Videos/Final/` through.
_DELIVERY_MAX_DEPTH = 3


def _videos_in(directory: Path) -> list[Path]:
    """Every plausible master sitting directly in `directory`."""
    out = []
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.suffix.lower() not in MASTER_EXTS:
            continue
        if any(m in p.stem for m in _OUTPUT_MARKERS):
            continue
        out.append(p)
    return out


def _root_videos(project_dir: Path) -> list[Path]:
    """Every plausible master sitting directly in the project root."""
    return _videos_in(project_dir)


def _delivery_subdir_videos(project_dir: Path, _depth: int = 1) -> list[Path]:
    """Plausible masters below the root, in recognized delivery subfolders.

    Descends through nested delivery names (`Videos/Final/`) but never through an
    unrecognized one, so a sibling `Videos/Drafts/` contributes nothing.
    """
    if _depth > _DELIVERY_MAX_DEPTH:
        return []
    out = []
    for d in sorted(project_dir.iterdir()):
        if d.is_dir() and d.name.lower() in _DELIVERY_SUBDIRS:
            out.extend(_videos_in(d))
            out.extend(_delivery_subdir_videos(d, _depth + 1))
    return out


def find_master_video(project_dir: Path) -> Path:
    """The source master for this project.

    Preferred: a root-level video whose name starts with the project id, which
    is the documented convention. But editors name exports for themselves --
    real folders hold `video_2025_01_25.mov`, `..._final.mov`, `..._v3.mov` --
    and the old prefix-only rule failed on all of them, reporting "no master
    video" for a folder with exactly one obvious master in it. Renaming the
    file is the wrong fix: these sit next to live DaVinci projects that
    reference them.

    So: prefix match wins when one exists; otherwise, if the root holds exactly
    ONE video, that's unambiguously the master. Several non-conforming
    candidates is a genuine ambiguity a human must settle, and it errors with
    the list rather than guessing.

    Editors also file the program in a subfolder rather than the root
    (`video/{id}.mp4`, `longform/video.mov`). When the root holds nothing, a
    recognized delivery subfolder is searched too -- see _DELIVERY_SUBDIRS for
    why that's an allowlist and not a walk.
    """
    videos = _root_videos(project_dir)
    searched = str(project_dir)
    if not videos:
        videos = _delivery_subdir_videos(project_dir)
        searched = (f"{project_dir} or its "
                    f"{'/'.join(_DELIVERY_SUBDIRS[:3])}/... subfolders")
    if not videos:
        raise ProjectNotFoundError(
            f"No master video ({' or '.join(MASTER_EXTS)}) found in {searched}")

    prefixed = [p for p in videos if p.name.startswith(project_dir.name)]
    pool = prefixed or videos
    if len(pool) > 1:
        if prefixed:
            # duplicate conforming exports (e.g. "_video.mp4" vs "_video_2.mp4"):
            # newest wins, as before
            pool = sorted(pool, key=lambda p: p.stat().st_mtime)
        else:
            raise ProjectNotFoundError(
                f"Several candidate masters in {searched} and none matches the "
                f"project id, so which one is the master is a judgment call: "
                + ", ".join(p.name for p in pool)
                + f". Rename the real one to start with '{project_dir.name}'."
            )
    return pool[-1]


def cn_output_dir(project_dir: Path) -> Path:
    out = project_dir / "CN"
    out.mkdir(exist_ok=True)
    return out


def me_wav_path(project_dir: Path) -> Path:
    """The music-and-effects bed for this project.

    Canonical name is {project_id}_me.wav, and that's what gets returned when
    nothing else is found -- so callers that only test .exists() behave exactly
    as before. But staff name these by hand and the real folders disagree with
    the convention in small ways: a stale suffix ("..._2025-01-05_ME.wav" in a
    "..._2025-01-05_video" folder), or a human title entirely ("Build strong
    functional arms_01_ME.wav"). Silently shipping a VO-only dub because a bed
    was there under a slightly different name is the failure being avoided --
    it's inaudible in the logs and obvious in the video.

    So fall back to any single *_me.wav / *_ME.wav in the root. Two or more is
    ambiguous, and the canonical path is returned so the caller reports the
    normal "no bed" path rather than picking one at random.
    """
    canonical = project_dir / f"{project_dir.name}_me.wav"
    if canonical.exists():
        return canonical
    found = [p for p in sorted(project_dir.glob("*.wav"))
             if p.is_file() and p.stem.lower().endswith("_me")]
    return found[0] if len(found) == 1 else canonical


def run_scratch_dir(project_id: str) -> Path:
    d = REPO_ROOT / "runs" / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def localized_master_path(scratch_dir: Path) -> Path:
    """The in-screen-text-localized video, if the screentext stage produced one.
    A large derived intermediate -- kept in scratch (gitignored), not /CN/."""
    return scratch_dir / "screentext" / "master_localized.mp4"


def effective_master(project_dir: Path, scratch_dir: Path) -> Path:
    """The video the render stage should burn subtitles onto: the localized
    master when in-screen text localization is enabled AND has produced one,
    else the raw master. This is the single seam that lets the screentext
    stage be entirely optional -- disable the flag (or never run the stage)
    and renders use the raw master exactly as before, even if a localized
    master lingers in scratch from an earlier experiment."""
    localized = localized_master_path(scratch_dir)
    if localized.exists() and get_config().screentext_enabled:
        return localized
    return find_master_video(project_dir)


def deliverable_paths(project_dir: Path, version: str = "") -> dict:
    """Standard output filenames per cn_workflow.html's Drive structure convention.

    `version` (e.g. "v2") suffixes every deliverable so a revision produced from
    review feedback never overwrites the previous cut -- {id}_cndub.mp4 stays,
    {id}_cndub_v2.mp4 is written alongside it. Empty version = the base names."""
    pid = project_dir.name
    out = cn_output_dir(project_dir)
    suf = f"_{version}" if version else ""
    return {
        "master": out / f"{pid}_master{suf}.mp4",
        "en_srt": out / f"{pid}_en{suf}.srt",
        "zh_srt": out / f"{pid}_zh{suf}.srt",
        "bilingual_ensub_srt": out / f"{pid}_bilingual_ensub{suf}.srt",
        "bilingual_cndub_srt": out / f"{pid}_bilingual_cndub{suf}.srt",
        "zh_vo_wav": out / f"{pid}_zh_vo{suf}.wav",
        "ensub_mp4": out / f"{pid}_ensub{suf}.mp4",
        "cndub_mp4": out / f"{pid}_cndub{suf}.mp4",
        "cover_jpg": out / f"{pid}_cover{suf}.jpg",
        "publish_kit": out / f"publish_kit{suf}.md",
        "run_log": out / f"run_log{suf}.md",
    }
