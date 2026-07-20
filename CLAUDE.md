# CLAUDE.md — cn-dub-pipeline

Chinese localization pipeline for Gravgear/Yellow Dude videos. Read this before
changing anything; it encodes decisions that were paid for in API spend and
review cycles. `docs/cn_workflow.html` is the rulebook for pipeline *behavior*
(stages, thresholds); this file is for *working on the code and its flows*.

## Division of labor (the load-bearing design rule)

- **CLI (`cn_pipeline/`, driven via `bin/cn-pipeline`) = mechanical.** Timing,
  alignment, rendering, uploads. Deterministic, re-runnable, SKIP_OK-gated.
- **Skills (`skills/*/SKILL.md`) = judgment + orchestration.** Translation,
  title picks, thumbnail wording, reading/writing Notion, deciding what to
  publish. The CLI never talks to Notion; skills never move bytes.

Keep new work on the right side of that line. When a reviewer's feedback needs
both (e.g. a term fix that triggers a re-dub), the skill edits the translation
and then drives the CLI.

## Conventions that must not regress

- **Versioned deliverables:** `deliverable_paths(project_dir, version)` — a
  review re-cut renders as `{id}_cndub_v2.mp4` NEXT TO v1, never over it. Every
  deliverable-writing CLI command takes `--version`. Publishing always takes
  the highest `_vN` present.
- **In the Notion Chinese DB, "ENsub/CNdub" are the two published VARIANTS**
  (英配中字 / 中配) — do not confuse them with review-cut v1/v2, which exist
  only in filenames and the Frame.io version stack.
- **Glossary is law** (`glossary/cn_glossary.md`): "Go Bananas" stays English,
  "Yellow Dude" → 光头黄, hamstrings → 大腿后侧 (never 腘绳肌 — the TTS voice
  mispronounces 腘; this exact regression came back from a native reviewer).
- **CN dub subtitles burn from a generated .ass, not SRT** (`build_cndub_ass`):
  ffmpeg's SRT reader strips inline `{\fs}` overrides, so SRT+force_style
  cannot size the Chinese and English lines differently. The .ass gives one
  line per language, English at ~66%, block low — a native-reviewer ask.
  `render ensub` still uses plain SRT deliberately.
- **M&E bed:** `{id}_me.wav` at the project root mixes under the VO at
  `me_gain_db` (config.json; +6dB ≈ 2× louder — reviewer-tuned to +2.0 for
  100-body-squats). If no me.wav exists, separate one with Demucs
  (`--two-stems=vocals`, use `no_vocals`), and have a human EAR-check it —
  Whisper "verification" hallucinates gibberish on music and cannot clear it.
- **Paid calls are capped** (`max_*_calls_per_run`, `runs/{id}/api_spend.json`).
  A `SpendCapError` is a flag to a human, not a prompt to raise the cap. TTS
  chunks are cached against exact text: after a translation edit, only changed
  lines re-spend.
- **Re-runs are safe**; every stage SKIP_OKs when outputs are current and
  `--force` cascades downstream. Prefer re-running a stage over hand-patching
  its outputs.

## Review loop (Frame.io) — how it's meant to be used

`review submit` uploads the cut, folds it into a **version stack** with the
previous cut, and shares the STACK: the reviewer gets Frame.io's version
dropdown + Compare and checks old comments against the new cut. The share
short-url (f.io/…) is **stable across versions** — it's written once into the
Chinese DB's `Frame.io link` and doesn't need updating per re-cut. Passphrase
comes from `FRAMEIO_SHARE_PASSPHRASE`. State lives in
`runs/{id}/frameio_review.json` (versions, stack_id, share_id).

`review fetch` converts V4 comment **framestamps** to ms using fps probed from
the local cndub (Frame.io's file object exposes no fps). Offline exports
(`--comments-json`) still read seconds — don't "fix" one path into the other.

All verified V4 API specifics (auth flow, exact request bodies, the traps) live
in `docs/frameio_review.md`. Do not re-derive them; several are undocumented
upstream and were probed against the live account.

## Publish stage

- YouTube (Chinese channel @yellowdude_zh, its own Google account): CNdub only,
  uploaded as a **private draft** — the link exists immediately for Notion;
  a human flips it public in Studio. OAuth is upload-only scope, which means
  the pipeline **cannot delete** videos — duplicates are a human cleanup, so
  the skill's no-double-publish pre-checks matter.
- Google OAuth app must be **published to production** in the console;
  test-mode refresh tokens die every 7 days.
- Bilibili: stubbed, waiting on official API access. Both variants will go
  there (ENsub link / CNdub link in the Chinese DB).
- Queue: the Chinese DB's `Publish requested` checkbox is intent only; nothing
  polls it. Publishing happens when a human runs the `publish-chinese` skill.

## Machine state & testing

- Secrets/config live in `CLAUDE_PLUGIN_DATA` (`.env`, `config.json`, venv),
  NOT the repo checkout — survives plugin updates. `bin/cn-pipeline` resolves
  this; outside a plugin session export `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA`
  explicitly.
- Python 3.14 venv (`cn-pipeline-setup`); ffmpeg-full (libass + videotoolbox).
- Tests run standalone: `python tests/test_x.py` (no pytest dependency);
  `tests/_bootstrap.py` stubs heavy deps only when absent. Keep new tests pure
  (no HTTP, no media) and runnable the same way.
