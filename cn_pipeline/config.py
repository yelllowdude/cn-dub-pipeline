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
CONFIG_PATH = REPO_ROOT / "config.json"
ENV_PATH = REPO_ROOT / ".env"

FFMPEG_FULL_DEFAULT = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"


class ConfigError(RuntimeError):
    pass


def _load_env():
    if not ENV_PATH.exists():
        raise ConfigError(
            f"No .env file at {ENV_PATH}. Copy .env.example to .env and fill in "
            "ELEVENLABS_API_KEY and KIE_API_KEY (get these from Wayne via a secure channel)."
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
            f"No config.json at {CONFIG_PATH}. Copy config.example.json to config.json "
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
