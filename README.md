# cn-dub-pipeline

Chinese dub localization pipeline for Gravgear/Yellow Dude YouTube→Bilibili
videos. One command (`localize {project-id} for Chinese`, via Claude Code)
runs transcription, translation, dubbing, alignment, rendering, and
thumbnail generation.

`docs/cn_workflow.html` is the source of truth for how the pipeline actually
behaves (stage rules, thresholds, exceptions) — read that, not just this
README, if you want to understand *why* something works the way it does.
`docs/cn_staff_handoff.html` covers the human upload/schedule steps after a
run finishes.

## First-time setup

1. **Homebrew**, if you don't have it: https://brew.sh
2. **ffmpeg-full** (not plain `ffmpeg` — this specific formula has the
   libass support subtitle burn-in needs, and the videotoolbox hardware
   encoder):
   ```
   brew install ffmpeg-full
   ```
3. **Python 3.14**:
   ```
   brew install python@3.14
   ```
4. **Clone this repo**, then from inside it:
   ```
   git clone <repo-url> cn-dub-pipeline
   cd cn-dub-pipeline
   python3.14 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
5. **Google Drive Desktop**: confirm it's installed and the `General` Shared
   Drive is synced. Find the exact path — it'll look like:
   ```
   /Users/<your-mac-username>/Library/CloudStorage/GoogleDrive-wayne@thegravgear.com/Shared drives/General
   ```
6. **Config file**:
   ```
   cp config.example.json config.json
   ```
   Edit `config.json`, set `drive_root` to the path from step 5 (yours will
   differ from anyone else's only by macOS username).
7. **API keys**: get `ELEVENLABS_API_KEY` and `KIE_API_KEY` from Wayne via a
   secure channel (password manager share — not Slack/email in plaintext).
   ```
   cp .env.example .env
   ```
   Paste the values into `.env`.
8. **Claude Code**: install it if you don't have it, log in with your own
   account.
9. From inside this repo's directory, launch Claude Code and confirm it
   picks up the skill:
   > "what skills are available?"

   You should see `localize-chinese` listed.

## First run — validate before trusting it on a new project

Before running this on a video that hasn't been localized yet, do a dry run
against an already-completed project (ask Wayne which one, `100-body-squats_2026-04-11`
is a good candidate) and compare your output's file durations against the
already-published `{id}_ensub.mp4` / `{id}_cndub.mp4` in that project's Drive
`/CN/` folder. They should match closely. This confirms your environment
produces the same result before you run it live on something new.

## Running it

In Claude Code, from inside this repo:
```
localize {project-id} for Chinese
```
See `.claude/skills/localize-chinese/SKILL.md` for exactly what that does —
worth a read once so you know which parts are mechanical (always
reproducible) versus where Claude is applying live judgment (translation
quality, title choice, thumbnail wording) that's worth double-checking.

## Repo layout

```
cn_pipeline/          the actual package — one module per pipeline stage concept
docs/                  cn_workflow.html (rules/thresholds) + cn_staff_handoff.html (upload/schedule)
glossary/              locked terms + formatting conventions, checked against every translation
.claude/skills/        the Claude Code skill that drives cn_pipeline's CLI
runs/{project-id}/     per-run working data (gitignored) — segments.json, zh.json, logs, configs
```

Nothing in `runs/` or any media file is committed to git — those are
per-video data and stay on Drive. Only code, docs, and templates live here.

## If something's broken

Check `runs/{project-id}/*.log` and `*_log.json` first. If a stage fails
partway, don't just retry blindly — TTS generation and the thumbnail
cleaning step both cost real API spend per call. Flag it to Wayne if the
cause isn't obvious from the log.
