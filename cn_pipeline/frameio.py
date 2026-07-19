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
The live Frame.io HTTP calls (_api_*) are isolated in one place and marked
UNVERIFIED: exact v4 endpoints/paths still need confirming against a real
token. Everything downstream works today from an exported comments JSON
(`fetch --comments-json ...`), so the pipeline is usable and testable before
the raw API is nailed down -- the API is a thin, swappable adapter, not the
substance.

A cue's index maps 1:1 to the translation: cue N in {id}_bilingual_cndub.srt
is zh.json[N-1] (align-dub emits one cue per translated line, in order). That
linkage is what lets an `apply` edit the actual translation source, not just
annotate the srt.
"""

import json
import re
from pathlib import Path

import requests

from cn_pipeline.config import ConfigError, get_config
from cn_pipeline.subtitles import parse_srt_time

FRAMEIO_API_BASE = "https://api.frame.io/v4"  # UNVERIFIED: confirm against a real token


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


# --- Frame.io HTTP adapter (ISOLATED, UNVERIFIED endpoints) ------------------

def _token() -> str:
    tok = get_config().frameio_token
    if not tok:
        raise ConfigError(
            "FRAMEIO_TOKEN is empty in .env. Register a Frame.io / Adobe "
            "Developer Console app (assets-read + comments-read + upload scope) "
            "and add FRAMEIO_TOKEN=... to .env. Until then, use "
            "`review fetch --comments-json <exported.json>` to run the "
            "resolve/classify/apply logic offline."
        )
    return tok


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _api_upload_for_review(video_path: Path) -> dict:
    """UNVERIFIED. Upload the video as a review asset; return
    {asset_id, review_link}. Frame.io's upload is a create-then-PUT-to-signed-URL
    dance; the exact v4 shape needs confirming against a live token. Kept in one
    function so swapping in the verified calls touches nothing else."""
    raise NotImplementedError(
        "Frame.io upload endpoint not yet verified. For now, upload the cndub "
        "to Frame.io by hand, share it with the reviewer, and use "
        "`review fetch --comments-json <exported comments>` once they've "
        "commented. See module docstring."
    )


def _api_fetch_comments(asset_id: str) -> list[dict]:
    """UNVERIFIED endpoint path; normalization is the stable part. Returns
    normalized [{id, text, timestamp_ms, author}]. Frame.io gives a comment a
    frame-based `timestamp`; convert with the asset fps when wiring this live."""
    resp = requests.get(f"{FRAMEIO_API_BASE}/assets/{asset_id}/comments",
                        headers=_headers(), timeout=60)
    resp.raise_for_status()
    return [_normalize_comment(c) for c in resp.json().get("data", resp.json())]


def _normalize_comment(raw: dict) -> dict:
    """Map a raw Frame.io comment to our schema. Accepts either a seconds
    `timestamp` or a `frame`+`fps`; offline exports should just provide
    timestamp_ms directly."""
    if "timestamp_ms" in raw:
        ts_ms = int(raw["timestamp_ms"])
    elif "timestamp" in raw:
        ts_ms = int(float(raw["timestamp"]) * 1000)
    elif "frame" in raw and raw.get("fps"):
        ts_ms = int(raw["frame"] / float(raw["fps"]) * 1000)
    else:
        ts_ms = 0
    return {
        "id": raw.get("id"),
        "text": raw.get("text", raw.get("body", "")),
        "timestamp_ms": ts_ms,
        "author": raw.get("author", raw.get("owner", {}).get("name") if isinstance(raw.get("owner"), dict) else None),
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
