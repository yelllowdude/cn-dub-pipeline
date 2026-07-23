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

## Storage modes (Drive API vs mount)

Two ways the pipeline reaches the Shared Drive, set by `storage` in
config.json (`cn_pipeline/gdrive.py` has the full design doc):

- **`gdrive` (team default):** the CLI talks to the Drive REST API and works
  against a LOCAL MIRROR that reproduces the Drive layout, so paths.py and
  every mtime-based stage gate keep local-file semantics. Bytes move only via
  `drive pull` / `drive push` (md5-diffed both ways). No Drive for Desktop,
  no per-person mount path.
- **`mount`:** the original Drive-for-Desktop path (`drive_root`). Unchanged
  behavior, kept for machines that already have the mount.

Rules that must not regress:
- **`drive pull` claims the project** (`CN/_pipeline/claim.json`, advisory);
  a claimed project refuses a second operator without `--steal`. This is the
  only guard against two machines double-paying TTS and forking the Frame.io
  version stack — never bypass it, never steal silently.
- **Shared scratch state lives at `CN/_pipeline/scratch/` on Drive** and
  syncs with `runs/{id}/` on pull/push. The paid TTS `chunks/` cache,
  `frameio_review.json`, and `api_spend.json` are in the sync set
  (losing frameio_review.json forks a NEW share link -- the exact failure
  the sync exists to prevent); huge regenerable intermediates
  (`audio_16k.wav`, `dub_master_*.wav`, align dirs) deliberately are not —
  see `gdrive.SCRATCH_EXCLUDE_*` before adding scratch files.
- Masters are pull-only: push never uploads `{id}*.mp4` from the project
  root, only `/CN/**` and `{id}_me.wav`.
- Drive auth reuses the YouTube Desktop OAuth client (GDRIVE_ vars fall back
  to YOUTUBE_ ones); the CONSENT accounts differ — Drive: any team member
  with edit access; YouTube: the channel account. Don't "fix" one into the
  other.

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
  mispronounces 腘; this exact regression came back from a native reviewer),
  and **product names stay in English** ("Pistol Squat Cheat Sheet", never
  手枪式深蹲小抄 — buyers must be able to find the SKU by name).
- **Two dub modes** (per-project `runs/{id}/project.json`, absent = `cue_locked`):
  `cue_locked` is the classic English-cue-timed path; `native` (dub_native.py +
  anchors.py) writes natural Chinese passages first, TTS at natural pace synced
  to operator-picked visual anchors (atempo hard-capped at 1.06 — overflow is
  fixed by tightening WORDING, never speed), then derives Chinese-only subtitles
  FROM the dub. Reviewer feedback "rushed / feels off" on a cue-locked cut is a
  signal to pilot native mode, not to keep patching lines. Never flip an
  existing project's mode mid-flight.
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
Chinese DB's `Review link` property and doesn't need updating per re-cut. Passphrase
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
  there (`ENsub Bilibili` / `CNdub Bilibili` in the Chinese DB).
- Queue: the Chinese DB's `Publish requested` checkbox is intent only; nothing
  polls it. Publishing happens when a human runs the `publish-chinese` skill.

## Machine state & testing

- Secrets/config live in `CLAUDE_PLUGIN_DATA` (`.env`, `config.json`, venv),
  NOT the repo checkout — survives plugin updates. `bin/cn-pipeline` resolves
  this; outside a plugin session export `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA`
  explicitly.
- Python 3.14 venv (`cn-pipeline-setup`; falls back to 3.13/3.12 with a
  warning). ffmpeg is probed at startup (brew ffmpeg-full on either
  architecture, then PATH) and must prove `--enable-libass` in its build
  config -- an ffmpeg without libass burns NO subtitles rather than erroring.
- Tests run standalone: `python tests/test_x.py` (no pytest dependency);
  `tests/_bootstrap.py` stubs heavy deps only when absent. Keep new tests pure
  (no HTTP, no media) and runnable the same way.
