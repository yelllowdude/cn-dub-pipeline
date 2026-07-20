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


def _mint_ims_token(cfg) -> str:
    """OAuth Server-to-Server: exchange client credentials for a short-lived
    access token, cached until ~60s before it expires."""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    resp = requests.post(
        IMS_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg.frameio_client_id,
            "client_secret": cfg.frameio_client_secret,
            "scope": cfg.frameio_ims_scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ConfigError(
            f"Adobe IMS token request failed ({resp.status_code}): {resp.text[:300]}. "
            "Check FRAMEIO_CLIENT_ID / FRAMEIO_CLIENT_SECRET and that your Adobe "
            "Developer Console project has the Frame.io API added with an OAuth "
            "Server-to-Server credential (requires an Adobe Admin Console account)."
        )
    tok = resp.json()
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


def _access_token(cfg) -> str:
    """Prefer S2S client credentials (auto-refresh); else a static pasted
    token; else a specific, actionable error."""
    if cfg.frameio_client_id and cfg.frameio_client_secret:
        return _mint_ims_token(cfg)
    if cfg.frameio_token:
        return cfg.frameio_token
    raise ConfigError(
        "No Frame.io V4 credentials. Set FRAMEIO_CLIENT_ID + FRAMEIO_CLIENT_SECRET "
        "in .env (OAuth Server-to-Server, recommended), or paste a V4 access token "
        "as FRAMEIO_TOKEN. Until then, `review fetch --comments-json <exported.json>` "
        "runs the resolve/classify/apply logic offline."
    )


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


def _api_upload_for_review(video_path: Path) -> dict:
    """Upload the cndub to Frame.io V4 and open a review share.
    Returns {asset_id, review_link, share_id}. Flow:
      1) create file (local_upload) -> presigned S3 part URLs
      2) PUT each part
      3) create a review share on the project and attach the file
    """
    cfg = get_config()
    account_id = _account_id(cfg)
    folder_id = _project_root_folder(cfg, account_id)
    size = video_path.stat().st_size
    media_type = "video/mp4"

    created = _api(
        "POST", f"/accounts/{account_id}/folders/{folder_id}/files/local_upload", cfg,
        json={"data": {"name": video_path.name, "file_size": size, "media_type": media_type}},
    )
    file_id = _first(created, "id")
    parts = _first(created, "upload_urls", "uploads", default=[])
    if not file_id or not parts:
        raise RuntimeError(f"unexpected local_upload response: {json.dumps(created)[:400]}")

    # PUT each presigned part. Use the per-part size the API returns when
    # present; otherwise split the file evenly across the returned URLs.
    with open(video_path, "rb") as fh:
        n = len(parts)
        even = -(-size // n)  # ceil division
        for i, part in enumerate(parts):
            url = part["url"] if isinstance(part, dict) else part
            psize = part.get("size") if isinstance(part, dict) else None
            chunk = fh.read(psize) if psize else fh.read(even)
            put = requests.put(url, data=chunk, headers={"Content-Type": media_type}, timeout=600)
            if put.status_code not in (200, 201, 204):
                raise RuntimeError(f"chunk {i + 1}/{n} PUT failed: {put.status_code} {put.text[:200]}")

    share = _api(
        "POST", f"/accounts/{account_id}/projects/{cfg.frameio_project_id}/shares", cfg,
        json={"data": {"name": f"CN dub review — {video_path.stem}", "type": "review"}},
    )
    share_id = _first(share, "id")
    review_link = _first(share, "short_url", "url", "review_link", "link")
    if share_id:
        _api("POST", f"/accounts/{account_id}/shares/{share_id}/assets", cfg,
             json={"data": {"asset_id": file_id}})
    return {"asset_id": file_id, "share_id": share_id, "review_link": review_link or ""}


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


def _api_fetch_comments(asset_id: str) -> list[dict]:
    """Pull comments for a file and normalize them. In V4 a comment's
    `timestamp` is a framestamp (1-based), so we fetch the file's fps to convert
    to ms. Follows `next` pagination if present."""
    cfg = get_config()
    account_id = _account_id(cfg)
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


def fetch_comments(asset_id: str | None, comments_json: Path | None) -> list[dict]:
    """Offline export if given, else the live API."""
    if comments_json:
        return load_comments_json(comments_json)
    if not asset_id:
        raise ValueError("need either --asset-id (live) or --comments-json (offline)")
    return _api_fetch_comments(asset_id)
