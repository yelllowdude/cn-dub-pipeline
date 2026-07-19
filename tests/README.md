# Tests

Pure-logic unit tests for the parts of the pipeline that don't touch TTS, OCR,
ffmpeg, or the network — the parts where a bug is silent and expensive:

| File | Covers |
|---|---|
| `test_stage_gate.py` | `SKIP_OK`/`--force` staleness; per-run spend cap incl. thread-safety |
| `test_dub_timeline.py` | leading + inter-chunk silence (`dub.chunk_timeline`) — the desync regression guard |
| `test_screentext.py` | in-screen text detection: IoU, event clustering, stability flag, gap-bridging, box clamping |
| `test_frameio.py` | review loop: cue resolution, classification, and the safe per-cue auto-apply |

## Running

No pytest required — each file runs standalone:

```
python tests/test_frameio.py         # one file
for t in tests/test_*.py; do python "$t"; done   # all
```

Or with pytest if you have it: `pytest tests/`.

`tests/_bootstrap.py` stubs the heavy runtime deps only when they're not
installed, so the same tests run in a bare checkout (CI) and in the full
project venv. What these **don't** cover is end-to-end media behavior (real
audio sync, real render durations) — that's validated by hand per
`docs/VALIDATE.md`.
