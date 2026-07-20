"""
Frame.io native-speaker review loop.

Why this exists: for Chinese the team can review at upload time, but for a
proper QC pass (and required for languages nobody on staff speaks) a native
speaker watches the dub on Frame.io and leaves time-coded comments. This
module turns those comments into actionable, cue-resolved fixes instead of a
list a human has to reconcile by hand.

The flow, and where the value is:
  submit  -> upload the cndub for review, get a share link (put it in Notion)
  fetch   -> pull the reviewer's time-coded comments, resolve each to the exact
             subtitle/dub cue it lands on, and classify it
  apply   -> auto-apply the mechanical fixes (a term swap or typo where the
             reviewer gave a concrete replacement), and route pacing/unclear
             feedback to a human queue -- never guess on those

Design honesty: the comment RESOLUTION and CLASSIFICATION logic below is pure
and unit-tested -- it's the part that was validated against real reviewer data.
The live Frame.io V4 HTTP calls (_api_*) are isolated in one place. Endpoint
PATHS are confirmed against the V4 migration guide; a few response FIELD names
are read defensively (_first) and get pinned on the first live call. Everything
downstream also works from an exported comments JSON (`fetch --comments-json
...`), so the pipeline stays usable and testable independent of the raw API --
the API is a thin, swappable adapter, not the substance.

A cue's index maps 1:1 to the translation: cue N in {id}_bilingual_cndub.srt
is zh.json[N-1] (align-dub emits one cue per translated line, in order). That
linkage is what lets an `apply` edit the actual translation source, not just
annotate the srt.
"""

import json
import re
import time
from pathlib import Path

import requests

from cn_pipeline.config import ConfigError, get_config
from cn_pipeline.subtitles import parse_srt_time

FRAMEIO_API_BASE = "https://api.frame.io/v4"


# --- srt cue parsing (pure) --------------------------------------------------

def parse_cndub_cues(srt_path: Path) -> list[dict]:
    """Parse {id}_bilingual_cndub.srt into cues. cue['zh_index'] is the index
    into zh.json this cue's Chinese line came from (idx-1), the seam that lets
    a comment become a translation edit."""
    raw = Path(srt_path).read_text(encoding="utf-8")
    cues = []
    for block in [b for b in raw.strip().split("\n\n") if b.strip()]:
        lines = block.split("\n")
        idx = int(lines[0])
        start, end = lines[1].split(" --> ")
        text_lines = lines[2:]
        cues.append({
            "idx": idx, "zh_index": idx - 1,
            "start_ms": parse_srt_time(start), "end_ms": parse_srt_time(end),
            "zh": text_lines[0] if text_lines else "",
            "en": text_lines[1] if len(text_lines) > 1 else "",
        })
    return cues


def resolve_comment_to_cue(timestamp_ms: int, cues: list[dict]) -> dict | None:
    """The cue whose time window contains the comment. If it lands in a gap
    between cues, snap to the nearest cue boundary -- a reviewer scrubbing to a
    line often lands a few frames early. Returns None only if there are no cues."""
    if not cues:
        return None
    for c in cues:
        if c["start_ms"] <= timestamp_ms <= c["end_ms"]:
            return c
    return min(cues, key=lambda c: min(abs(c["start_ms"] - timestamp_ms), abs(c["end_ms"] - timestamp_ms)))


# --- classification (pure) ---------------------------------------------------

# A reviewer's correction is auto-appliable only when it names both the old and
# the new string. These forms cover how the native reviewers actually wrote
# them (verified against real comments): an arrow, 改成/应该是 with the old term
# quoted, or English "X" should be "Y".
_ARROW = re.compile(r"[「\"']?([^\"'「」→\->]{1,40}?)[」\"']?\s*(?:→|->|—>|=>)\s*[「\"']?([^\"'「」]{1,40}?)[」\"']?\s*$")
_GAISUCHENG = re.compile(r"[「\"']?([^\"'「」]{1,40}?)[」\"']?\s*(?:改成|应该(?:是|为)|应该翻译成|应翻译为)\s*[「\"']?([^\"'「」]{1,40}?)[」\"']?\s*[。.!！]?$")
_EN_SHOULD_BE = re.compile(r"[\"']([^\"']{1,40})[\"']\s+should be\s+[\"']([^\"']{1,40})[\"']", re.I)

_TERM_HINTS = ("翻译", "术语", "用词", "错译", "不对", "wrong", "should be", "term", "translate", "改成", "应该")
_TYPO_HINTS = ("错别字", "typo", "拼写", "spelling", "别字", "少了", "多了个", "写错")
_TIMING_HINTS = ("太快", "太慢", "对不上", "不同步", "字幕慢", "字幕快", "早了", "晚了", "sync", "timing",
                 "too fast", "too slow", "lag", "delay", "out of sync", "节奏", "语速")


def extract_replacement(text: str) -> tuple[str, str] | None:
    """Pull an (old, new) pair from an explicit correction, or None. Only the
    forms that name BOTH sides -- 'just say it better' gives no pair and must
    go to a human."""
    for pat in (_ARROW, _EN_SHOULD_BE, _GAISUCHENG):
        m = pat.search(text.strip())
        if m:
            old, new = m.group(1).strip(), m.group(2).strip()
            if old and new and old != new:
                return old, new
    return None


def classify_comment(text: str) -> dict:
    """Return {category, replacement?}. Categories:
      term   - word-choice/translation fix (auto-fixable iff a replacement pair
               is present AND later found in the cue)
      typo   - spelling/character error (same auto-fix rule)
      timing - pacing / subtitle-sync feedback -> human (a translation edit
               can't fix timing)
      unclear- anything else -> human
    """
    low = text.lower()
    repl = extract_replacement(text)
    if repl:
        # an explicit A->B is a term/typo correction regardless of surrounding words
        cat = "typo" if any(h in low for h in _TYPO_HINTS) else "term"
        return {"category": cat, "replacement": {"old": repl[0], "new": repl[1]}}
    if any(h in low for h in _TIMING_HINTS):
        return {"category": "timing"}
    if any(h in low for h in _TYPO_HINTS):
        return {"category": "typo"}
    if any(h in low for h in _TERM_HINTS):
        return {"category": "term"}
    return {"category": "unclear"}


def build_review_report(comments: list[dict], cues: list[dict]) -> dict:
    """comments: normalized [{id, text, timestamp_ms, author}]. Produces a
    report pairing each comment with its cue and classification, split into an
    auto-fixable queue and a human queue."""
    auto, human = [], []
    for c in comments:
        cue = resolve_comment_to_cue(c["timestamp_ms"], cues)
        cls = classify_comment(c["text"])
        entry = {
            "comment_id": c.get("id"), "author": c.get("author"),
            "text": c["text"], "timestamp_ms": c["timestamp_ms"],
            "cue_idx": cue["idx"] if cue else None,
            "zh_index": cue["zh_index"] if cue else None,
            "cue_zh": cue["zh"] if cue else None,
            "category": cls["category"], "replacement": cls.get("replacement"),
        }
        # auto-fixable only if we have a concrete old->new AND the old string is
        # actually in the resolved cue's Chinese line (otherwise we'd edit blind)
        rep = cls.get("replacement")
        if rep and cue and rep["old"] in cue["zh"]:
            entry["auto_fixable"] = True
            auto.append(entry)
        else:
            entry["auto_fixable"] = False
            if rep and cue and rep["old"] not in cue["zh"]:
                entry["human_reason"] = f"replacement target '{rep['old']}' not found in cue {cue['idx']}"
            elif cls["category"] in ("timing", "unclear"):
                entry["human_reason"] = f"{cls['category']} feedback -- needs judgment"
            else:
                entry["human_reason"] = "no concrete replacement given"
            human.append(entry)
    return {
        "comment_count": len(comments),
        "auto_fixable": auto, "needs_human": human,
        "auto_count": len(auto), "human_count": len(human),
    }


def apply_auto_fixes(report: dict, zh_lines: list[str]) -> tuple[list[str], list[dict]]:
    """Apply the auto-fixable term/typo swaps to the translation list. Returns
    (new_zh_lines, changelog). Each fix replaces old->new only within its own
    resolved cue's line -- never a blanket document-wide replace, so a term
    that's correct elsewhere is untouched."""
    new = list(zh_lines)
    changelog = []
    for e in report["auto_fixable"]:
        i = e["zh_index"]
        rep = e["replacement"]
        if i is None or not (0 <= i < len(new)) or rep["old"] not in new[i]:
            changelog.append({"cue_idx": e["cue_idx"], "status": "skipped",
                              "reason": "target no longer present (already fixed?)"})
            continue
        before = new[i]
        new[i] = before.replace(rep["old"], rep["new"])
        changelog.append({"cue_idx": e["cue_idx"], "status": "applied",
                          "old": rep["old"], "new": rep["new"],
                          "before": before, "after": new[i]})
    return new, changelog


# --- Frame.io V4 HTTP adapter (isolated) -------------------------------------
#
# Auth uses Adobe IMS. Preferred: OAuth Server-to-Server (client_credentials),
# so the pipeline mints/refreshes its own access tokens unattended. Fallback:
# a static V4 access token pasted as FRAMEIO_TOKEN (expires ~24h).
#
# Endpoint PATHS are from the V4 migration guide (confirmed):
#   local upload : POST /accounts/{acct}/folders/{folder}/files/local_upload
#   list comments: GET  /accounts/{acct}/files/{file}/comments   (timestamp = framestamp, 1-based)
#   create share : POST /accounts/{acct}/projects/{proj}/shares  {"data":{"name","type":"review"}}
#   add to share : POST /accounts/{acct}/shares/{share}/assets
# Response FIELD names are read defensively via _first() because they vary
# slightly per resource; the first live call is where any remaining name is
# pinned down. Everything downstream already works from an exported comments
# JSON, so this adapter is the only part that needs a live token to verify.

IMS_AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

_token_cache = {"value": None, "expires_at": 0.0}


def _first(d: dict, *keys, default=None):
    """First present, non-None value among keys, looking at the object and one
    level into a 'data' envelope. Absorbs minor V4 response-shape variance
    without scattering .get() chains through the call sites."""
    sources = [d]
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        sources.append(d["data"])
    for src in sources:
        if isinstance(src, dict):
            for k in keys:
                if src.get(k) is not None:
                    return src[k]
    return default


def _unwrap(payload):
    """V4 wraps single resources and lists under 'data'."""
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _ims_post(data: dict) -> dict:
    """POST the IMS token endpoint and return the parsed token response."""
    resp = requests.post(
        IMS_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
    )
    if resp.status_code != 200:
        raise ConfigError(f"Adobe IMS token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _cached_token(mint) -> str:
    """Return a cached access token, refreshing via `mint` ~60s before expiry."""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    tok = mint()
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


def _access_token(cfg) -> str:
    """Resolve a usable V4 access token from whatever auth is configured:
    User-Auth refresh token > S2S client credentials > static pasted token."""
    cid, secret = cfg.frameio_client_id, cfg.frameio_client_secret
    if cid and secret and cfg.frameio_refresh_token:
        return _cached_token(lambda: _ims_post({
            "grant_type": "refresh_token", "client_id": cid, "client_secret": secret,
            "refresh_token": cfg.frameio_refresh_token, "scope": cfg.frameio_ims_scope,
        }))
    if cid and secret:
        return _cached_token(lambda: _ims_post({
            "grant_type": "client_credentials", "client_id": cid, "client_secret": secret,
            "scope": cfg.frameio_ims_scope,
        }))
    if cfg.frameio_token:
        return cfg.frameio_token
    raise ConfigError(
        "No Frame.io V4 credentials. Run `cn-pipeline review auth` to sign in and store a "
        "refresh token (needs FRAMEIO_CLIENT_ID/SECRET from an Adobe Web App credential); or "
        "set FRAMEIO_CLIENT_ID/SECRET alone for Server-to-Server; or paste a V4 access token "
        "as FRAMEIO_TOKEN. Until then, `review fetch --comments-json <exported.json>` runs offline."
    )


# --- one-time User-Authentication (OAuth) helper -----------------------------

def build_authorize_url(cfg) -> str:
    """The IMS sign-in URL for the User-Auth flow. offline_access must be in the
    scope (config) so the exchanged token carries a refresh token."""
    from urllib.parse import urlencode
    if not (cfg.frameio_client_id and cfg.frameio_client_secret):
        raise ConfigError(
            "Set FRAMEIO_CLIENT_ID and FRAMEIO_CLIENT_SECRET (from an Adobe Developer "
            "Console 'User Authentication' Web App credential) before `review auth`."
        )
    q = urlencode({
        "client_id": cfg.frameio_client_id,
        "redirect_uri": cfg.frameio_redirect_uri,
        "scope": cfg.frameio_ims_scope,
        "response_type": "code",
    })
    return f"{IMS_AUTHORIZE_URL}?{q}"


def _code_from_redirect(redirect_or_code: str) -> str:
    """Accept either the full redirected URL (…/redirect/?code=XYZ) or a bare code."""
    from urllib.parse import urlparse, parse_qs
    s = redirect_or_code.strip().strip("'\"")
    if "code=" not in s:
        return s  # assume a bare code was pasted
    codes = parse_qs(urlparse(s).query).get("code")
    if not codes:
        raise ConfigError("no `code=` parameter found in the pasted redirect URL")
    return codes[0]


def exchange_code_for_tokens(cfg, redirect_or_code: str) -> dict:
    """Exchange the one-time auth code for {access_token, refresh_token, ...}."""
    tok = _ims_post({
        "grant_type": "authorization_code",
        "client_id": cfg.frameio_client_id,
        "client_secret": cfg.frameio_client_secret,
        "code": _code_from_redirect(redirect_or_code),
    })
    if not tok.get("refresh_token"):
        raise ConfigError(
            "IMS returned no refresh_token. Ensure `offline_access` is in frameio_ims_scope "
            "and the Web App credential is allowed to request it."
        )
    return tok


def save_refresh_token_to_env(refresh_token: str) -> Path:
    """Persist FRAMEIO_REFRESH_TOKEN into the data-dir .env, replacing any prior line."""
    from cn_pipeline.config import save_env_var
    return save_env_var("FRAMEIO_REFRESH_TOKEN", refresh_token)


def _headers(cfg, json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {_access_token(cfg)}", "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _api(method: str, path: str, cfg, **kw) -> dict:
    """One V4 request against the account-prefixed base, with a readable error."""
    url = path if path.startswith("http") else f"{FRAMEIO_API_BASE}{path}"
    resp = requests.request(method, url, headers=_headers(cfg), timeout=120, **kw)
    if resp.status_code >= 400:
        raise RuntimeError(f"Frame.io {method} {path} -> {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else {}


def _account_id(cfg) -> str:
    """The configured account, or the first the token can see."""
    if cfg.frameio_account_id:
        return cfg.frameio_account_id
    accounts = _unwrap(_api("GET", "/accounts", cfg))
    if isinstance(accounts, list) and accounts:
        return _first(accounts[0], "id")
    if isinstance(accounts, dict):
        return _first(accounts, "id")
    raise RuntimeError("no Frame.io accounts visible to this token; set frameio_account_id")


def _project_root_folder(cfg, account_id: str) -> str:
    if not cfg.frameio_project_id:
        raise ConfigError("config.json is missing 'frameio_project_id' (where to upload the review copy)")
    proj = _api("GET", f"/accounts/{account_id}/projects/{cfg.frameio_project_id}", cfg)
    root = _first(proj, "root_folder_id", "root_asset_id", "folder_id")
    if not root:
        raise RuntimeError(f"no root folder id in project response: {json.dumps(proj)[:300]}")
    return root


def _put_upload_part(url: str, chunk: bytes, media_type: str) -> None:
    """PUT one presigned S3 part. The presigned URL signs a specific set of
    headers (X-Amz-SignedHeaders); we must send exactly those, or S3 403s. In
    practice Frame.io signs content-type;host;x-amz-acl -- we set the two we can."""
    from urllib.parse import urlparse, parse_qs
    signed = parse_qs(urlparse(url).query).get("X-Amz-SignedHeaders", [""])[0].lower()
    headers = {}
    if "content-type" in signed:
        headers["Content-Type"] = media_type
    if "x-amz-acl" in signed:
        headers["x-amz-acl"] = "private"
    put = requests.put(url, data=chunk, headers=headers, timeout=600)
    if put.status_code not in (200, 201, 204):
        raise RuntimeError(f"part PUT failed: {put.status_code} {put.text[:200]}")


def _create_review_share(cfg, account_id: str, file_id: str, name: str):
    """Create a PUBLIC review share and attach the file; return (share_id, short_url).
    The V4 share create body is a discriminated schema (verified live):
      {"data": {"name": ..., "type": "asset", "access": "public"}}
    -- type='asset' selects the share variant, access must be the "public" enum.
    The share is created empty, then the file is attached via
    .../shares/{id}/assets {"data": {"asset_id": ...}}. Best-effort: on any
    failure returns (None, None) so the caller falls back to the file view_url."""
    try:
        data = {"name": name, "type": "asset", "access": "public"}
        if cfg.frameio_share_passphrase:
            data["passphrase"] = cfg.frameio_share_passphrase
        share = _api(
            "POST", f"/accounts/{account_id}/projects/{cfg.frameio_project_id}/shares", cfg,
            json={"data": data},
        )
        share_id = _first(share, "id")
        url = _first(share, "short_url", "url")
        if share_id and file_id:
            _api("POST", f"/accounts/{account_id}/shares/{share_id}/assets", cfg,
                 json={"data": {"asset_id": file_id}})
        return share_id, url
    except Exception:
        return None, None


def upload_file_for_review(video_path: Path) -> dict:
    """Upload one cndub cut to Frame.io V4 (create file -> PUT presigned parts).
    Returns {asset_id, view_url}. Sharing and version-stacking are the caller's
    job so a re-cut can be stacked onto the previous version, not just re-shared."""
    cfg = get_config()
    account_id = _account_id(cfg)
    folder_id = _project_root_folder(cfg, account_id)
    size = video_path.stat().st_size
    created = _api(
        "POST", f"/accounts/{account_id}/folders/{folder_id}/files/local_upload", cfg,
        json={"data": {"name": video_path.name, "file_size": size}},
    )
    file_id = _first(created, "id")
    media_type = _first(created, "media_type") or "video/mp4"
    view_url = _first(created, "view_url") or ""
    parts = _first(created, "upload_urls", "uploads", default=[])
    if not file_id or not parts:
        raise RuntimeError(f"unexpected local_upload response: {json.dumps(created)[:400]}")
    with open(video_path, "rb") as fh:
        for part in parts:
            psize = part.get("size") if isinstance(part, dict) else None
            url = part["url"] if isinstance(part, dict) else part
            _put_upload_part(url, fh.read(psize) if psize else fh.read(), media_type)
    return {"asset_id": file_id, "view_url": view_url}


def create_version_stack(cfg, account_id: str, folder_id: str, file_ids: list[str]) -> str:
    """Merge 2-10 uploaded files into a version stack (verified live):
      POST /accounts/{acct}/folders/{folder}/version_stacks {"data":{"file_ids":[...]}}
    Order is oldest->newest; the last id becomes the current version. So reviewers
    get Frame.io's version dropdown + Compare, and prior-version comments carry
    over for check-off. Returns the version_stack id."""
    res = _api("POST", f"/accounts/{account_id}/folders/{folder_id}/version_stacks", cfg,
               json={"data": {"file_ids": file_ids}})
    return _first(res, "id")


def add_to_version_stack(cfg, account_id: str, file_id: str, stack_id: str) -> None:
    """Move an already-uploaded file into an existing version stack (for v3+):
      PATCH /accounts/{acct}/files/{file}/move  with the stack as the new parent."""
    _api("PATCH", f"/accounts/{account_id}/files/{file_id}/move", cfg,
         json={"data": {"parent_id": stack_id}})


def delete_share(cfg, account_id: str, share_id: str) -> None:
    """Best-effort delete of a share (used to retire a single-file share once its
    file is folded into a version stack that gets its own share)."""
    try:
        _api("DELETE", f"/accounts/{account_id}/shares/{share_id}", cfg)
    except Exception:
        pass


def _api_file_fps(cfg, account_id: str, file_id: str):
    """A V4 comment's timestamp is a framestamp; converting to ms needs the
    file's fps. Best-effort -- returns None if the field isn't present."""
    try:
        f = _api("GET", f"/accounts/{account_id}/files/{file_id}", cfg)
    except Exception:
        return None
    fps = _first(f, "fps", "frame_rate")
    if fps is None:
        media = _first(f, "media_info", "metadata")
        if isinstance(media, dict):
            fps = media.get("fps") or media.get("frame_rate")
    try:
        return float(fps) if fps else None
    except (TypeError, ValueError):
        return None


def _api_fetch_comments(asset_id: str, fps: float | None = None) -> list[dict]:
    """Pull comments for a file and normalize them. In V4 a comment's
    `timestamp` is a framestamp (1-based), so an fps is needed to convert to ms.
    Frame.io's file object doesn't expose fps, so callers pass it (probed from
    the local cndub); we only fall back to the API metadata if none is given.
    Follows `next` pagination if present."""
    cfg = get_config()
    account_id = _account_id(cfg)
    if fps is None:
        fps = _api_file_fps(cfg, account_id, asset_id)
    out, path = [], f"/accounts/{account_id}/files/{asset_id}/comments"
    while path:
        payload = _api("GET", path, cfg)
        for c in _unwrap(payload) or []:
            out.append(_normalize_comment(c, fps=fps))
        nxt = None
        if isinstance(payload, dict):
            links = payload.get("links") or {}
            nxt = links.get("next") or payload.get("next") or payload.get("next_cursor")
        path = nxt if isinstance(nxt, str) else None
    return out


def _normalize_comment(raw: dict, fps: float | None = None) -> dict:
    """Map a raw Frame.io comment to our schema {id, text, timestamp_ms, author}.
    Timestamp resolution, in priority order:
      1. explicit `timestamp_ms`         -- offline exports should provide this
      2. V4 framestamp (`framestamp`, or `timestamp` when fps is known), 1-based
      3. seconds `timestamp`             -- legacy / seconds-based exports
      4. `frame` + `fps` pair
    fps is passed by the live V4 fetch (from the file) and left None offline, so
    an export's seconds `timestamp` is never misread as a framestamp."""
    if raw.get("timestamp_ms") is not None:
        ts_ms = int(raw["timestamp_ms"])
    elif fps and (raw.get("framestamp") is not None or raw.get("timestamp") is not None):
        frame = raw.get("framestamp", raw.get("timestamp"))
        ts_ms = int(max(0.0, float(frame) - 1) / fps * 1000)
    elif raw.get("timestamp") is not None:
        ts_ms = int(float(raw["timestamp"]) * 1000)
    elif raw.get("frame") is not None and raw.get("fps"):
        ts_ms = int(raw["frame"] / float(raw["fps"]) * 1000)
    else:
        ts_ms = 0
    author = raw.get("author")
    if author is None:
        owner = raw.get("owner")
        author = owner.get("name") if isinstance(owner, dict) else raw.get("author_name")
    return {
        "id": raw.get("id"),
        "text": raw.get("text", raw.get("body", "")),
        "timestamp_ms": ts_ms,
        "author": author,
    }


def load_comments_json(path: Path) -> list[dict]:
    """Load an exported/offline comments file and normalize it. Accepts a bare
    list or a {"comments":[...]} / {"data":[...]} wrapper."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("comments") or data.get("data") or []
    return [_normalize_comment(c) for c in data]


def fetch_comments(asset_id: str | None, comments_json: Path | None,
                   fps: float | None = None) -> list[dict]:
    """Offline export if given, else the live API. fps (probed from the local
    cndub) converts V4 framestamps to ms; ignored for offline exports."""
    if comments_json:
        return load_comments_json(comments_json)
    if not asset_id:
        raise ValueError("need either --asset-id (live) or --comments-json (offline)")
    return _api_fetch_comments(asset_id, fps=fps)
