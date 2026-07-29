"""
Cross-operator shared state for "mount" storage -- the mode where the Shared
Drive is a Google Drive for Desktop folder at drive_root rather than something
reached over the REST API.

Mount mode was the unprotected one. cn_pipeline.gdrive already put the claim
lock and the shared scratch on Drive, but every `drive` command refused to run
unless storage was "gdrive", so the mode everyone actually uses had neither:
two operators could start the same project and silently double-spend the TTS
budget, or fork the Frame.io version stack into two review links nobody could
reconcile.

The fix is not a second policy -- it is the SAME policy over a different
transport. claim_verdict / make_claim / touch_claim / scratch_syncable all
come from cn_pipeline.gdrive, so both modes arbitrate claims and choose
shareable files identically. Only the read/write differs: Drive REST there,
plain files on the mount here.

What lives where, in mount mode:
  - CN/                      deliverables. Already shared -- it IS the Drive folder.
  - CN/_pipeline/claim.json  the advisory lock (same schema as gdrive mode).
  - CN/_pipeline/scratch/    the shareable half of runs/{id}/: segments, anchors,
                             zh/zh_script, project.json, api_spend.json,
                             frameio_review.json, and the PAID TTS chunk cache.
  - runs/{id}/               stays local and authoritative during a run. Huge
                             regenerable intermediates (dub_master_*.wav,
                             align_chunks/) never sync -- pushing gigabytes of
                             scratch through Drive for Desktop on every stage
                             would be slower than regenerating them.

Sync is explicit and tied to the claim: taking the claim restores shared
scratch in, releasing it saves shared scratch out. That is the handoff
boundary, and it keeps Drive for Desktop from fighting a running pipeline over
files that change every few seconds.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from cn_pipeline.gdrive import (CLAIM_FILENAME, PIPELINE_DIRNAME, SCRATCH_DIRNAME,
                                ClaimError, _iso_now, claim_verdict, make_claim,
                                scratch_syncable, touch_claim, whoami)

__all__ = ["ClaimError", "claim", "release", "status", "heartbeat",
           "sync_scratch_out", "sync_scratch_in", "pipeline_dir", "claim_path"]


def pipeline_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "CN" / PIPELINE_DIRNAME


def claim_path(project_dir: Path) -> Path:
    return pipeline_dir(project_dir) / CLAIM_FILENAME


def shared_scratch_dir(project_dir: Path) -> Path:
    return pipeline_dir(project_dir) / SCRATCH_DIRNAME


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write via a same-directory temp + os.replace. The claim file is read by
    other machines through Drive sync; a half-written one reads as corrupt and
    (by _read_claim below) as unclaimed, which is exactly the race the claim
    exists to prevent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".claim-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_claim(project_dir: Path) -> dict | None:
    p = claim_path(project_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Unreadable claim: treat as unclaimed rather than hard-failing every
        # command. The next claim rewrites it cleanly.
        return None


def status(project_dir: Path) -> dict | None:
    """The current claim record, or None if the project was never claimed."""
    return _read_claim(project_dir)


def claim(project_dir: Path, me: dict | None = None, steal: bool = False) -> str:
    """Take (or refresh) the claim. Returns 'fresh'|'mine'|'stolen'|'stale';
    raises ClaimError when someone else is actively holding it."""
    me = me or whoami()
    existing = _read_claim(project_dir)
    verdict = claim_verdict(existing, me, steal)
    data = touch_claim(existing, me) if verdict == "mine" else make_claim(me)
    _write_json_atomic(claim_path(project_dir), data)
    return verdict


def heartbeat(project_dir: Path, me: dict | None = None) -> None:
    """Refresh last_seen if we hold the claim. Never raises and never takes a
    claim it doesn't already have -- this runs on ordinary commands, and a
    read-only `mode show` should not be able to steal anything."""
    me = me or whoami()
    existing = _read_claim(project_dir)
    if not existing or not existing.get("claimed"):
        return
    if existing.get("operator") != me["operator"] or existing.get("host") != me["host"]:
        return
    try:
        _write_json_atomic(claim_path(project_dir), touch_claim(existing, me))
    except OSError:
        pass  # a heartbeat is best-effort; never fail a real command over it


def release(project_dir: Path, me: dict | None = None) -> None:
    """Hand the project back. Writes claimed=false rather than deleting, so the
    record of who had it last survives (and mount mode matches gdrive mode,
    which avoids deletes to stay off Drive delete permission)."""
    me = me or whoami()
    _write_json_atomic(claim_path(project_dir), {
        "claimed": False, "operator": me["operator"], "host": me["host"],
        "hostname": me.get("hostname", ""), "released_at": _iso_now(),
    })


# --- scratch sync ---------------------------------------------------------------

def _copy_newer(src: Path, dest: Path) -> bool:
    """Copy when the destination is missing or older. Size+mtime rather than a
    hash: these are small JSON/mp3 files on a local mount, and Drive for
    Desktop already preserves mtime."""
    if dest.exists():
        s, d = src.stat(), dest.stat()
        if s.st_size == d.st_size and int(s.st_mtime) <= int(d.st_mtime):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _sync_tree(src_root: Path, dest_root: Path) -> list[str]:
    if not src_root.is_dir():
        return []
    copied = []
    for f in sorted(src_root.rglob("*")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(src_root))
        if not scratch_syncable(rel):
            continue
        if _copy_newer(f, dest_root / rel):
            copied.append(rel)
    return copied


def sync_scratch_out(scratch_dir: Path, project_dir: Path) -> list[str]:
    """runs/{id}/ -> CN/_pipeline/scratch/. Publishes the paid and
    irreplaceable state so the next operator inherits it."""
    return _sync_tree(Path(scratch_dir), shared_scratch_dir(project_dir))


def sync_scratch_in(scratch_dir: Path, project_dir: Path) -> list[str]:
    """CN/_pipeline/scratch/ -> runs/{id}/. Restores a colleague's TTS cache
    and review state instead of re-buying them."""
    return _sync_tree(shared_scratch_dir(project_dir), Path(scratch_dir))
