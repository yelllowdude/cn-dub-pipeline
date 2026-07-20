"""
Publish stage: push finished Chinese cuts to their destination platforms.

Scope (deliberate):
  - YouTube (the Chinese channel @yellowdude_zh): upload the CNdub as a PRIVATE
    draft via the resumable upload API and return the video link immediately --
    a private video already has its final URL, so Notion can hold the link
    while a human decides when to flip it public in YouTube Studio.
  - Bilibili: NOT implemented -- the team is waiting on official API access.
    `cn-pipeline publish bilibili` exits with that message so nobody wires up
    a scraper in the meantime. Both ENsub and CNdub go there once access lands.

Who does what (same division of labor as the rest of the pipeline): this module
does the mechanical upload and returns links; deciding WHAT to publish (which
Notion rows have `Publish requested` checked) and writing links back to the
Chinese database is orchestration, owned by the Claude-side skill -- Notion is
the team's interface, and its writes stay with the layer that reads it.

Auth mirrors cn_pipeline.frameio: a Desktop OAuth client (id/secret in .env),
one browser sign-in via `publish auth` -- done signed in AS the channel account
(yellowdude.zh@gmail.com) -- then a stored refresh token mints short-lived
access tokens unattended. Plain requests, no Google SDK: the pipeline already
talks raw HTTP everywhere else, and two endpoints don't justify three deps.
"""

import json
import time
from pathlib import Path

import requests

from cn_pipeline.config import ConfigError, get_config, save_env_var

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# upload-only scope: least privilege that still returns the video id/link
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
# Desktop OAuth clients accept a bare loopback redirect; the sign-in ends on a
# localhost URL that won't load -- the code is copied from the address bar,
# exactly like the Frame.io `review auth` flow.
REDIRECT_URI = "http://localhost"

UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024  # resumable PUTs in 64MB chunks

_token_cache = {"value": None, "expires_at": 0.0}


# --- OAuth (one-time auth + unattended refresh) -------------------------------

def build_authorize_url(cfg) -> str:
    """Google sign-in URL. access_type=offline + prompt=consent forces a
    refresh_token in the exchange (Google omits it on silent re-consents)."""
    from urllib.parse import urlencode
    if not (cfg.youtube_client_id and cfg.youtube_client_secret):
        raise ConfigError(
            "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first (a Desktop "
            "OAuth client from Google Cloud Console, YouTube Data API v3 enabled)."
        )
    return f"{GOOGLE_AUTH_URL}?" + urlencode({
        "client_id": cfg.youtube_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })


def _code_from_redirect(redirect_or_code: str) -> str:
    """Accept the full localhost redirect URL or a bare pasted code."""
    from urllib.parse import urlparse, parse_qs
    s = redirect_or_code.strip().strip("'\"")
    if "code=" not in s:
        return s
    codes = parse_qs(urlparse(s).query).get("code")
    if not codes:
        raise ConfigError("no `code=` parameter found in the pasted redirect URL")
    return codes[0]


def _token_post(data: dict) -> dict:
    resp = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        raise ConfigError(f"Google token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def exchange_code_for_tokens(cfg, redirect_or_code: str) -> dict:
    tok = _token_post({
        "grant_type": "authorization_code",
        "client_id": cfg.youtube_client_id,
        "client_secret": cfg.youtube_client_secret,
        "code": _code_from_redirect(redirect_or_code),
        "redirect_uri": REDIRECT_URI,
    })
    if not tok.get("refresh_token"):
        raise ConfigError(
            "Google returned no refresh_token. Re-run `publish auth` and make sure the "
            "consent screen actually appears (prompt=consent should force it)."
        )
    return tok


def save_refresh_token(refresh_token: str) -> Path:
    return save_env_var("YOUTUBE_REFRESH_TOKEN", refresh_token)


def _access_token(cfg) -> str:
    if not (cfg.youtube_client_id and cfg.youtube_client_secret and cfg.youtube_refresh_token):
        raise ConfigError(
            "No YouTube credentials. Run `cn-pipeline publish auth` once, signed in as the "
            "Chinese channel account (yellowdude.zh@gmail.com), to store a refresh token."
        )
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    tok = _token_post({
        "grant_type": "refresh_token",
        "client_id": cfg.youtube_client_id,
        "client_secret": cfg.youtube_client_secret,
        "refresh_token": cfg.youtube_refresh_token,
    })
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


# --- upload -------------------------------------------------------------------

def build_video_body(title: str, description: str = "", tags: list[str] | None = None) -> dict:
    """videos.insert metadata. privacyStatus=private IS the draft: the video id
    (and thus its final watch URL) exists immediately; a human flips it public
    in Studio when ready. selfDeclaredMadeForKids must be explicit or Studio
    nags on every upload."""
    snippet = {"title": title[:100], "description": description[:4900]}
    if tags:
        snippet["tags"] = tags[:30]
    return {
        "snippet": snippet,
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }


def upload_youtube_draft(video_path: Path, title: str, description: str = "",
                         tags: list[str] | None = None) -> dict:
    """Resumable upload -> {video_id, link}. Two steps: an init POST returns a
    session URL in the Location header, then the file goes up in chunked PUTs
    with Content-Range (308 = chunk accepted, keep going)."""
    cfg = get_config()
    size = video_path.stat().st_size
    body = build_video_body(title, description, tags)

    init = requests.post(
        f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {_access_token(cfg)}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=body, timeout=60,
    )
    if init.status_code != 200:
        raise RuntimeError(f"upload init failed ({init.status_code}): {init.text[:400]}")
    session_url = init.headers.get("Location")
    if not session_url:
        raise RuntimeError("upload init returned no resumable session URL")

    sent = 0
    with open(video_path, "rb") as fh:
        while sent < size:
            chunk = fh.read(UPLOAD_CHUNK_BYTES)
            end = sent + len(chunk) - 1
            put = requests.put(
                session_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {sent}-{end}/{size}",
                },
                data=chunk, timeout=1800,
            )
            if put.status_code in (200, 201):
                data = put.json()
                vid = data.get("id")
                if not vid:
                    raise RuntimeError(f"upload finished but no video id: {put.text[:300]}")
                return {"video_id": vid, "link": f"https://youtu.be/{vid}",
                        "privacy": data.get("status", {}).get("privacyStatus")}
            if put.status_code != 308:
                raise RuntimeError(
                    f"chunk PUT failed at byte {sent} ({put.status_code}): {put.text[:300]}")
            sent = end + 1
    raise RuntimeError("upload loop ended without a completed response")


def set_thumbnail(video_id: str, image_path: Path) -> dict:
    """Set the video's custom thumbnail (thumbnails.set accepts the upload-only
    scope). Requires the channel to have custom thumbnails enabled (phone
    verification) -- a 403 here means verify the channel, not a code bug.
    Returns {ok, detail}."""
    cfg = get_config()
    suffix = image_path.suffix.lower()
    content_type = "image/png" if suffix == ".png" else "image/jpeg"
    resp = requests.post(
        f"{YOUTUBE_THUMBNAIL_URL}?videoId={video_id}",
        headers={
            "Authorization": f"Bearer {_access_token(cfg)}",
            "Content-Type": content_type,
        },
        data=image_path.read_bytes(), timeout=120,
    )
    if resp.status_code != 200:
        return {"ok": False, "detail": f"thumbnails.set {resp.status_code}: {resp.text[:300]}"}
    return {"ok": True, "detail": f"thumbnail set from {image_path.name}"}
