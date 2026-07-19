"""
Per-run paid-API spend guard.

Not a cost estimator -- a dumb counter with a hard stop. The docs have always
said "don't retry a paid stage blindly" (SKILL.md, README, cn_workflow.html),
but until now that rule lived only in prose, enforceable only by careful
readers. This makes it mechanical: every paid call (ElevenLabs TTS, KIE
thumbnail clean) increments a counter in runs/{id}/api_spend.json, and a call
past the per-run cap raises instead of spending.

Caps live in config.json (max_tts_calls_per_run / max_kie_calls_per_run) with
defaults sized generously for a normal run -- a typical video is ~10-15 TTS
chunks plus a handful of re-split sub-chunks, and exactly one KIE clean --
so the cap should only ever trip on a runaway loop or a rerun that should
have been served from cache.
"""

import json
import threading
from pathlib import Path

SPEND_FILE = "api_spend.json"

# generate() fires TTS calls from a thread pool; the read-increment-write on
# the counter file must not interleave.
_lock = threading.Lock()


class SpendCapError(RuntimeError):
    pass


def record_call(scratch_dir: Path, service: str, cap: int) -> int:
    """Record one paid call against this run's counter. Raises SpendCapError
    (before any spend) if the call would exceed the cap."""
    path = Path(scratch_dir) / SPEND_FILE
    with _lock:
        counts = json.loads(path.read_text()) if path.exists() else {}
        n = counts.get(service, 0) + 1
        if n > cap:
            raise SpendCapError(
                f"{service} call #{n} this run would exceed the per-run cap of {cap} "
                f"(counter: {path}). This usually means a retry loop, or a rerun that "
                "should have been served from cache. Flag what happened before spending "
                "more (see SKILL.md's failure section); if the spend is genuinely "
                f"intended, raise max_{service}_calls_per_run in config.json or delete "
                "the counter file."
            )
        counts[service] = n
        path.write_text(json.dumps(counts, indent=2))
    return n
