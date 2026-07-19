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

## First-time setup (team plugin install — recommended)

If your team is on the Gravgear Claude Team plan, this is a real installed
plugin — you don't clone anything or `cd` into a folder to use it.

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
4. **Google Drive Desktop**: confirm it's installed and the `General` Shared
   Drive is synced.
5. **Install the plugin**, in any Claude Code session:
   ```
   /plugin marketplace add yelllowdude/cn-dub-pipeline
   /plugin install cn-dub-pipeline@gravgear-tools
   ```
6. **Run the one-time environment setup** (creates a Python venv + starter
   `.env`/`config.json` under this plugin's own data directory, so a later
   plugin update won't wipe them):
   > "run cn-pipeline-setup"

   Then edit the `.env` and `config.json` it just created (Claude Code will
   tell you the exact path — it's under `~/.claude/plugins/data/...`, not
   inside the plugin's synced files):
   - `.env`: `ELEVENLABS_API_KEY` and `KIE_API_KEY` — get these from Wayne
     via a secure channel (password manager share — not Slack/email
     plaintext).
   - `config.json`: set `drive_root` to your Google Drive Desktop path, e.g.
     `/Users/<your-mac-username>/Library/CloudStorage/GoogleDrive-wayne@thegravgear.com/Shared drives/General`
     (differs from anyone else's only by macOS username).
7. Confirm the skill is live:
   > "what skills are available?"

   You should see `localize-chinese` listed.

## First-time setup (local dev clone — for editing the pipeline itself)

Only needed if you're changing `cn_pipeline`'s code, not just running it.

1. Steps 1-4 above (Homebrew, ffmpeg-full, Python 3.14, Drive Desktop).
2. Clone and set up a venv at the repo root (the older, pre-plugin layout —
   `bin/cn-pipeline` auto-detects this and uses it if
   `CLAUDE_PLUGIN_DATA/venv` doesn't exist):
   ```
   git clone <repo-url> cn-dub-pipeline
   cd cn-dub-pipeline
   python3.14 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp config.example.json config.json   # edit drive_root
   cp .env.example .env                 # edit API keys
   ```
3. Launch Claude Code from inside this directory — `.claude/skills/` is
   auto-discovered the same way `skills/` is when installed as a plugin.

## First run — validate before trusting it on a new project

Before running this on a video that hasn't been localized yet, do a dry run
against an already-completed project (ask Wayne which one, `100-body-squats_2026-04-11`
is a good candidate) and compare your output's file durations against the
already-published `{id}_ensub.mp4` / `{id}_cndub.mp4` in that project's Drive
`/CN/` folder. They should match closely. This confirms your environment
produces the same result before you run it live on something new.

## Running it

In any Claude Code session (no `cd` needed once installed as a plugin):
```
localize {project-id} for Chinese
```
See `skills/localize-chinese/SKILL.md` for exactly what that does — worth a
read once so you know which parts are mechanical (always reproducible)
versus where Claude is applying live judgment (translation quality, title
choice, thumbnail wording) that's worth double-checking.

## Repo layout

```
.claude-plugin/        plugin.json + marketplace.json — this repo is both
skills/                 the Claude Code skill that drives cn_pipeline's CLI (plugin-standard location)
.claude/skills/         same skill, duplicated here for local-dev auto-discovery when you cd into this repo directly
bin/                    cn-pipeline (CLI wrapper) + cn-pipeline-setup (one-time env setup), both added to PATH when installed as a plugin
cn_pipeline/            the actual package — one module per pipeline stage concept
docs/                   cn_workflow.html (rules/thresholds) + cn_staff_handoff.html (upload/schedule)
glossary/               locked terms + formatting conventions, checked against every translation
runs/{project-id}/      per-run working data (gitignored) — segments.json, zh.json, logs, configs
```

Nothing in `runs/` or any media file is committed to git — those are
per-video data and stay on Drive. Only code, docs, and templates live here.

**Where your `.env`/`config.json`/venv actually live:** if you installed via
the plugin path, they're under `~/.claude/plugins/data/<plugin-id>/` (run
`cn-pipeline-setup` — it'll print the exact path), *not* inside this repo's
synced copy — that's deliberate, so `/plugin marketplace update` never wipes
your keys or Drive path. If you're on the local-dev clone, they're just at
the repo root as before.

## If something's broken

Check `runs/{project-id}/*.log` and `*_log.json` first. If a stage fails
partway, don't just retry blindly — TTS generation and the thumbnail
cleaning step both cost real API spend per call. Flag it to Wayne if the
cause isn't obvious from the log.
