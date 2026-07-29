"""
One-time credential handoff: `team export` on a set-up machine, `team import`
on a new one.

The team runs the pipeline against shared service accounts -- the ElevenLabs
and KIE keys are already team keys, and Frame.io authenticates as a dedicated
account under the workspace admin, not as any individual. Nothing about that
needs re-consenting per person, but every new machine was still running the
whole browser OAuth dance to arrive at credentials it could have been handed.

So this exports the shared half and leaves the rest alone. Per-person auth
still works exactly as before (`review auth`, `publish auth`) -- import is the
shortcut, not the only road, because a teammate who needs to re-auth a single
service shouldn't have to wait on anyone.

WHAT MUST NOT TRAVEL, and why each one matters:

  .machine_id       The claim lock's identity. Two machines sharing one id look
                    like the SAME machine to claim_verdict, so both would take
                    the claim re-entrantly and work the same project in silence
                    -- precisely the double-spend the lock exists to stop.
                    Copying this doesn't weaken the lock, it disables it.
  drive_root        Where Drive for Desktop mounted, which differs per machine
                    and per account.
  operator          The label a teammate is identified by. Inheriting someone
                    else's makes every claim message name the wrong person.
  ffmpeg_path       Wherever this machine's ffmpeg lives.
  mirror_dir        Machine-local scratch location for gdrive mode.
  GDRIVE_*          Genuinely per-person: mount mode needs no Google token at
                    all, and in gdrive mode the token should be the operator's
                    own Drive access, not a borrowed one.

The bundle contains live secrets. It is written 0600 and belongs in the team
password vault next to the keys already kept there -- not in the repo, and not
in the Shared Drive folder the pipeline itself reads.
"""

import json
import os
import stat
import tempfile
from pathlib import Path

# Shared service-account credentials: same values on every machine.
SHAREABLE_ENV_KEYS = (
    "ELEVENLABS_API_KEY",
    "KIE_API_KEY",
    "FRAMEIO_CLIENT_ID",
    "FRAMEIO_CLIENT_SECRET",
    "FRAMEIO_REFRESH_TOKEN",
    "FRAMEIO_SHARE_PASSPHRASE",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
)

# Never exported. See the module docstring for why each one is here.
MACHINE_LOCAL_ENV_KEYS = (
    "GDRIVE_CLIENT_ID",
    "GDRIVE_CLIENT_SECRET",
    "GDRIVE_REFRESH_TOKEN",
    "FRAMEIO_TOKEN",          # a hand-pasted ~24h token; stale before it lands
)

# config.json values that describe the TEAM's setup, not this machine's.
SHAREABLE_CONFIG_KEYS = (
    "storage",
    "gdrive_drive_name",
    "gdrive_drive_id",
    "frameio_account_id",
    "frameio_project_id",
    "frameio_redirect_uri",
)

MACHINE_LOCAL_CONFIG_KEYS = (
    "drive_root",
    "mirror_dir",
    "operator",
    "ffmpeg_path",
)

BUNDLE_VERSION = 1
DEFAULT_BUNDLE_NAME = "cn-pipeline-team-credentials.json"


class TeamBundleError(RuntimeError):
    pass


def _read_env(env_path: Path) -> dict:
    out = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def build_bundle(env: dict, config: dict) -> dict:
    """The shareable subset, plus a note of what was deliberately left out so
    the importing operator knows what they still have to do themselves."""
    creds = {k: env[k] for k in SHAREABLE_ENV_KEYS if env.get(k)}
    if not creds:
        raise TeamBundleError(
            "nothing shareable found in .env -- is this machine actually set up? "
            "Run `cn-pipeline doctor` to see what's missing.")
    return {
        "bundle_version": BUNDLE_VERSION,
        "credentials": creds,
        "config": {k: config[k] for k in SHAREABLE_CONFIG_KEYS if k in config},
        "excluded": {
            "credentials": [k for k in MACHINE_LOCAL_ENV_KEYS if env.get(k)],
            "config": [k for k in MACHINE_LOCAL_CONFIG_KEYS if k in config],
            "note": "machine-local or per-person -- set these on each machine. "
                    ".machine_id is never exported: sharing it would disable the "
                    "project claim lock.",
        },
    }


def write_bundle(bundle: dict, dest: Path) -> Path:
    """0600, atomically. This file holds live API keys and a refresh token."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".bundle-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest


def load_bundle(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise TeamBundleError(f"no bundle at {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TeamBundleError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(bundle, dict) or "credentials" not in bundle:
        raise TeamBundleError(
            f"{path} doesn't look like a cn-pipeline team bundle "
            "(no 'credentials' key). Was it produced by `cn-pipeline team export`?")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise TeamBundleError(
            f"bundle_version {bundle.get('bundle_version')!r}, this build expects "
            f"{BUNDLE_VERSION}. Re-export from an up-to-date machine.")
    return bundle


def plan_import(bundle: dict, env: dict, config: dict, overwrite: bool) -> dict:
    """What an import would change, decided before anything is written.

    Existing values are kept unless --overwrite: a teammate who has already
    authenticated a service themselves should not silently lose that by
    accepting the shared bundle.
    """
    creds = bundle.get("credentials", {})
    cfg_in = bundle.get("config", {})

    env_add = {k: v for k, v in creds.items() if not env.get(k)}
    env_conflict = {k: v for k, v in creds.items() if env.get(k) and env[k] != v}
    env_same = [k for k, v in creds.items() if env.get(k) == v]

    cfg_add = {k: v for k, v in cfg_in.items() if k not in config}
    cfg_conflict = {k: v for k, v in cfg_in.items()
                    if k in config and config[k] != v}

    # Machine-local keys are never touched, whatever the bundle says.
    refused = sorted(set(creds) & set(MACHINE_LOCAL_ENV_KEYS)
                     | set(cfg_in) & set(MACHINE_LOCAL_CONFIG_KEYS))

    return {
        "env_add": env_add,
        "env_conflict": env_conflict,
        "env_same": env_same,
        "config_add": cfg_add,
        "config_conflict": cfg_conflict,
        "refused": refused,
        "env_writes": {**env_add, **(env_conflict if overwrite else {})},
        "config_writes": {**cfg_add, **(cfg_conflict if overwrite else {})},
    }


def apply_import(plan: dict, save_env_var, config_path: Path) -> None:
    """Write the planned changes. save_env_var is injected so this reuses the
    atomic 0600 .env writer rather than growing a second one."""
    for k, v in plan["env_writes"].items():
        save_env_var(k, v)

    if not plan["config_writes"]:
        return
    config_path = Path(config_path)
    current = (json.loads(config_path.read_text(encoding="utf-8"))
               if config_path.exists() else {})
    current.update(plan["config_writes"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export_from_machine(env_path: Path, config_path: Path, dest: Path) -> tuple[Path, dict]:
    env = _read_env(Path(env_path))
    config = (json.loads(Path(config_path).read_text(encoding="utf-8"))
              if Path(config_path).exists() else {})
    bundle = build_bundle(env, config)
    return write_bundle(bundle, dest), bundle
