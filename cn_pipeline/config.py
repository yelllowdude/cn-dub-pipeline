"""
Environment resolution: drive root, ffmpeg binary, API keys.

Fails loudly and specifically rather than silently falling back --
a render missing libass support produces a video with no burned-in
subtitles, which is a worse failure than refusing to start.
"""

import json
import os
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


def _load_env():
    if not ENV_PATH.exists():
        raise ConfigError(
            f"No .env file at {ENV_PATH}. Run cn-pipeline-setup (or copy .env.example "
            "to .env yourself) and fill in ELEVENLABS_API_KEY and KIE_API_KEY "
            "(get these from Wayne via a secure channel)."
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
        # Two auth modes, checked in this order by cn_pipeline.frameio:
        #   1. OAuth Server-to-Server (preferred for automation): set
        #      FRAMEIO_CLIENT_ID + FRAMEIO_CLIENT_SECRET in .env; the code mints
        #      and refreshes short-lived access tokens from Adobe IMS itself.
        #   2. Static access token: paste a V4 access token as FRAMEIO_TOKEN
        #      (simplest, but expires ~24h and must be re-pasted).
        # Account/project ids are not secret, so they live in config.json.
        self.frameio_token = os.environ.get("FRAMEIO_TOKEN", "")
        self.frameio_client_id = os.environ.get("FRAMEIO_CLIENT_ID", "")
        self.frameio_client_secret = os.environ.get("FRAMEIO_CLIENT_SECRET", "")
        self.frameio_account_id = raw.get("frameio_account_id", "")
        self.frameio_project_id = raw.get("frameio_project_id", "")
        # IMS scopes for the S2S client_credentials grant. Overridable because
        # the exact scope string is account/integration-specific; the default
        # covers the roles-based authz Frame.io V4 uses.
        self.frameio_ims_scope = raw.get(
            "frameio_ims_scope",
            "openid, AdobeID, additional_info.roles, read_organizations",
        )

        drive_root = raw.get("drive_root")
        if not drive_root:
            raise ConfigError("config.json is missing 'drive_root'")
        self.drive_root = Path(drive_root).expanduser()
        if not self.drive_root.is_dir():
            raise ConfigError(
                f"drive_root '{self.drive_root}' doesn't exist or isn't a directory. "
                "Confirm Google Drive Desktop is installed and the Shared Drive has synced "
                "(see README step on Drive setup)."
            )

        self.whisper_model = raw.get("whisper_model", "small")
        self.ffmpeg_path = self._resolve_ffmpeg(raw.get("ffmpeg_path"))

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
        if override:
            p = Path(override)
            if not p.exists():
                raise ConfigError(f"config.json's ffmpeg_path override '{override}' doesn't exist")
            return str(p)

        default = Path(FFMPEG_FULL_DEFAULT)
        if default.exists():
            return str(default)

        raise ConfigError(
            f"ffmpeg-full not found at {FFMPEG_FULL_DEFAULT}. This pipeline needs the "
            "'ffmpeg-full' Homebrew formula specifically (plain 'ffmpeg' lacks libass, "
            "so subtitle burn-in silently fails). Run: brew install ffmpeg-full\n"
            "If it's installed somewhere else, set \"ffmpeg_path\" in config.json."
        )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
