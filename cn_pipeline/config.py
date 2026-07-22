"""
Environment resolution: drive root, ffmpeg binary, API keys.

Fails loudly and specifically rather than silently falling back --
a render missing libass support produces a video with no burned-in
subtitles, which is a worse failure than refusing to start.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Machine-specific state (.env, config.json) lives in CLAUDE_PLUGIN_DATA when
# running as an installed plugin, since that directory survives plugin
# updates and REPO_ROOT (the plugin's synced copy) doesn't. Local dev via a
# direct clone has no CLAUDE_PLUGIN_DATA set, so this falls back to REPO_ROOT
# unchanged -- same behavior as before this was packaged as a plugin.
DATA_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_DATA", REPO_ROOT))
CONFIG_PATH = DATA_ROOT / "config.json"
ENV_PATH = DATA_ROOT / ".env"

FFMPEG_FULL_DEFAULT = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"


class ConfigError(RuntimeError):
    pass


def _has_libass(ffmpeg_path: str) -> bool:
    """True if this ffmpeg build can burn subtitles (libass compiled in)."""
    try:
        out = subprocess.run([ffmpeg_path, "-version"], capture_output=True,
                             text=True, timeout=10)
        return "--enable-libass" in out.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _load_env():
    if not ENV_PATH.exists():
        raise ConfigError(
            f"No .env file at {ENV_PATH}. Run cn-pipeline-setup (or copy .env.example "
            "to .env yourself) and fill in ELEVENLABS_API_KEY and KIE_API_KEY "
            "(the shared team keys live in the team password vault -- see README, "
            "'Credentials')."
        )
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise ConfigError(
            f"No config.json at {CONFIG_PATH}. Run cn-pipeline-setup (or copy "
            "config.example.json to config.json yourself) "
            "and fill in your drive_root path (see README)."
        )
    return json.loads(CONFIG_PATH.read_text())


def save_env_var(key: str, value: str) -> Path:
    """Persist KEY=value into the data-dir .env, replacing any existing line.
    Used by the one-time auth flows (Frame.io, YouTube) to store refresh tokens
    so later runs authenticate unattended."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return ENV_PATH


class Config:
    def __init__(self):
        _load_env()
        raw = _load_config()

        self.elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.kie_api_key = os.environ.get("KIE_API_KEY", "")
        if not self.elevenlabs_api_key:
            raise ConfigError("ELEVENLABS_API_KEY is empty in .env")
        if not self.kie_api_key:
            raise ConfigError("KIE_API_KEY is empty in .env")

        # Frame.io V4 review integration. All optional at startup -- only the
        # `review` stage needs them, and cn_pipeline.frameio raises a specific
        # message if a required one is missing when a review command runs.
        #
        # Auth modes, checked in this order by cn_pipeline.frameio._access_token:
        #   1. User Authentication (OAuth Web App) refresh token: set
        #      FRAMEIO_CLIENT_ID + FRAMEIO_CLIENT_SECRET + FRAMEIO_REFRESH_TOKEN.
        #      Get the refresh token once via `cn-pipeline review auth` (browser
        #      sign-in). The code then refreshes access tokens unattended.
        #   2. OAuth Server-to-Server (client_credentials): FRAMEIO_CLIENT_ID +
        #      FRAMEIO_CLIENT_SECRET only. Cleanest, but the S2S credential needs
        #      an Adobe Admin Console license many orgs don't have.
        #   3. Static access token: paste a V4 access token as FRAMEIO_TOKEN
        #      (simplest, but expires ~24h and must be re-pasted).
        # Account/project ids and the redirect URI are not secret -> config.json.
        self.frameio_token = os.environ.get("FRAMEIO_TOKEN", "")
        self.frameio_client_id = os.environ.get("FRAMEIO_CLIENT_ID", "")
        self.frameio_client_secret = os.environ.get("FRAMEIO_CLIENT_SECRET", "")
        self.frameio_refresh_token = os.environ.get("FRAMEIO_REFRESH_TOKEN", "")
        self.frameio_account_id = raw.get("frameio_account_id", "")
        self.frameio_project_id = raw.get("frameio_project_id", "")
        # Optional passphrase for review shares. When set, the public f.io link
        # requires it before an (external) reviewer can view/comment. From .env
        # since it gates access. Empty -> link is open to anyone who has it.
        self.frameio_share_passphrase = os.environ.get("FRAMEIO_SHARE_PASSPHRASE", "")

        # YouTube publish (Chinese channel, yellowdude.zh@gmail.com). Same shape
        # as the Frame.io auth: a Desktop OAuth client's id/secret plus a refresh
        # token minted once via `cn-pipeline publish auth` (browser sign-in AS the
        # channel account). All optional at startup -- only `publish` needs them.
        self.youtube_client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        self.youtube_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
        self.youtube_refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
        # Must be HTTPS and match a redirect URI pattern registered on the Adobe
        # Web App credential. The `review auth` flow only reads the code back
        # from the browser's address bar, so nothing needs to actually serve it.
        self.frameio_redirect_uri = raw.get("frameio_redirect_uri", "https://localhost/redirect/")
        # IMS scopes. offline_access is REQUIRED to receive a refresh token in
        # the User Authentication flow. Overridable per integration.
        self.frameio_ims_scope = raw.get(
            "frameio_ims_scope",
            "openid,AdobeID,email,profile,offline_access,additional_info.roles",
        )

        # Two storage modes for the video files on the Shared Drive:
        #   "gdrive" (team default): the CLI talks to the Drive REST API and works
        #       against a local mirror (see cn_pipeline.gdrive). Needs no Google
        #       Drive for Desktop install and no per-person mount path.
        #   "mount": the original Drive-for-Desktop path; drive_root points at the
        #       synced Shared Drive. Kept as a fallback for machines that already
        #       have the mount -- behavior is exactly as before this mode existed.
        # Absent "storage" key: infer from which config keys are filled, so every
        # pre-existing config.json (drive_root only) keeps working unchanged.
        self.gdrive_drive_id = raw.get("gdrive_drive_id", "") or ""
        self.gdrive_drive_name = raw.get("gdrive_drive_name", "") or ""
        self.storage = raw.get("storage") or (
            "gdrive" if (self.gdrive_drive_id or self.gdrive_drive_name) else "mount")
        if self.storage not in ("gdrive", "mount"):
            raise ConfigError(f"config.json 'storage' must be \"gdrive\" or \"mount\", "
                              f"got {self.storage!r}")

        # Drive API credentials (gdrive mode). The GDRIVE_ vars fall back to the
        # YouTube Desktop client from the same Google Cloud project -- one client
        # serves both APIs; only the consenting account differs (Drive: any team
        # member of the Shared Drive; YouTube: the channel account).
        self.gdrive_client_id = (os.environ.get("GDRIVE_CLIENT_ID", "")
                                 or os.environ.get("YOUTUBE_CLIENT_ID", ""))
        self.gdrive_client_secret = (os.environ.get("GDRIVE_CLIENT_SECRET", "")
                                     or os.environ.get("YOUTUBE_CLIENT_SECRET", ""))
        self.gdrive_refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "")
        # Optional operator label for the project claim file; defaults to the
        # OS username (see gdrive.whoami).
        self.operator = raw.get("operator", "") or ""

        if self.storage == "gdrive":
            if not (self.gdrive_drive_id or self.gdrive_drive_name):
                raise ConfigError(
                    "storage is \"gdrive\" but config.json has neither 'gdrive_drive_name' "
                    "nor 'gdrive_drive_id' (the Shared Drive holding _videos/, e.g. \"General\").")
            mirror = raw.get("mirror_dir") or str(DATA_ROOT / "mirror")
            # the mirror reproduces the Drive layout, so every path helper in
            # cn_pipeline.paths works on it unchanged
            self.drive_root = Path(mirror).expanduser()
            self.drive_root.mkdir(parents=True, exist_ok=True)
        else:
            drive_root = raw.get("drive_root")
            if not drive_root:
                raise ConfigError("config.json is missing 'drive_root' (mount mode) -- "
                                  "or set storage to \"gdrive\" (see README, 'Storage modes')")
            self.drive_root = Path(drive_root).expanduser()
            if not self.drive_root.is_dir():
                raise ConfigError(
                    f"drive_root '{self.drive_root}' doesn't exist or isn't a directory. "
                    "Confirm Google Drive Desktop is installed and the Shared Drive has synced "
                    "(see README step on Drive setup)."
                )

        self.whisper_model = raw.get("whisper_model", "small")
        self.ffmpeg_path = self._resolve_ffmpeg(raw.get("ffmpeg_path"))

        # Gain (dB) applied to the music/effects bed when `dub mix-me` lays it
        # under the Chinese VO. Tunable in config.json without a code change;
        # +6 dB is ~2x louder. Default -4 keeps it under the voice.
        self.me_gain_db = float(raw.get("me_gain_db", -4.0))

        # Per-run paid-call caps (see cn_pipeline.spend). Defaults are sized
        # so a normal run never notices them: ~10-15 TTS chunks + re-split
        # sub-chunks, and exactly 1 KIE thumbnail clean per video.
        self.max_tts_calls_per_run = int(raw.get("max_tts_calls_per_run", 60))
        self.max_kie_calls_per_run = int(raw.get("max_kie_calls_per_run", 5))
        # In-screen text localization cleans one region per detected text event,
        # so it needs its own (larger) budget separate from the thumbnail's
        # single clean -- a busy video can have dozens of on-screen labels.
        self.max_screentext_clean_calls_per_run = int(raw.get("max_screentext_clean_calls_per_run", 40))

        # In-screen text localization is EXPERIMENTAL and off by default. Flip
        # to true in config.json to try it. When false, `screentext` commands
        # refuse to run and renders always use the raw master -- so the feature
        # is inert until deliberately switched on, and trivial to abandon.
        # (To remove it wholesale: delete cn_pipeline/screentext.py, drop the
        # `screentext` group in cli.py, and revert paths.effective_master to
        # find_master_video. Nothing else depends on it.)
        self.screentext_enabled = bool(raw.get("screentext_enabled", False))

    def _resolve_ffmpeg(self, override: str | None) -> str:
        """Find a libass-capable ffmpeg. Probes, in order: the config override,
        Homebrew's ffmpeg-full (via brew --prefix, so Intel /usr/local and Apple
        Silicon /opt/homebrew both work), the two known brew paths directly, and
        finally whatever's on PATH. Each candidate must prove libass in its build
        config -- a render through an ffmpeg without libass produces a video with
        NO burned-in subtitles rather than an error, which is why this refuses to
        fall back silently."""
        if override:
            p = Path(override)
            if not p.exists():
                raise ConfigError(f"config.json's ffmpeg_path override '{override}' doesn't exist")
            if not _has_libass(str(p)):
                raise ConfigError(
                    f"ffmpeg_path override '{override}' has no libass support "
                    "(--enable-libass missing from its build config), so subtitle "
                    "burn-in would silently produce sub-less videos. Point it at "
                    "an ffmpeg built with libass (brew's 'ffmpeg-full')."
                )
            return str(p)

        candidates = []
        brew = shutil.which("brew")
        if brew:
            try:
                prefix = subprocess.run([brew, "--prefix", "ffmpeg-full"], capture_output=True,
                                        text=True, timeout=10).stdout.strip()
                if prefix:
                    candidates.append(f"{prefix}/bin/ffmpeg")
            except (subprocess.TimeoutExpired, OSError):
                pass
        candidates += [FFMPEG_FULL_DEFAULT, "/usr/local/opt/ffmpeg-full/bin/ffmpeg"]
        path_ffmpeg = shutil.which("ffmpeg")
        if path_ffmpeg:
            candidates.append(path_ffmpeg)

        checked = []
        for c in candidates:
            if c in checked or not Path(c).exists():
                continue
            checked.append(c)
            if _has_libass(c):
                return c

        raise ConfigError(
            "No libass-capable ffmpeg found (checked: "
            + (", ".join(checked) or "nothing on PATH or in the known Homebrew locations")
            + "). This pipeline needs an ffmpeg built with libass -- plain 'ffmpeg' "
            "lacks it, so subtitle burn-in silently fails. On macOS run: "
            "brew install ffmpeg-full ; on Linux the distro ffmpeg usually has libass "
            "already. If it's installed somewhere unusual, set \"ffmpeg_path\" in config.json."
        )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
