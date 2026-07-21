# cn-dub-pipeline

Turns a finished Gravgear/Yellow Dude YouTube video into a Chinese-dubbed
version — translated, voiced, subtitled, rendered, sent for native-speaker
review, and published — with Claude doing the machine work and humans making
the judgment calls.

**If you just want to use it, read the first half of this page and stop.**
The machine internals live in the second half and in `docs/`.

---

## Part 1 · The human guide

### How a video gets localized

You talk to Claude in plain language. There are only three phrases to know:

1. **"localize {project-id} for Chinese"** — Claude transcribes the English,
   writes a natural Chinese script, generates the voiceover, adds subtitles,
   renders the video, uploads it to Frame.io for review, and files the review
   link into the Notion Chinese database. Takes roughly an hour; you can walk
   away.
2. **"apply the review feedback for {project-id}"** — after the native
   reviewer has watched the cut on Frame.io and left time-coded comments
   (Notion page comments work too), Claude reads every comment, makes the
   fixes, and puts a new version on the **same** review link so the reviewer
   can compare old vs new with one dropdown.
3. **"publish {project-id} to Chinese channels"** — once the reviewer says
   it's good, Claude uploads the video to the Chinese YouTube channel as a
   **private draft**, sets the thumbnail, and files the link back into
   Notion. Nothing goes live by itself.

### What only humans do

- **Review the video.** A native speaker watches the Frame.io link and leaves
  comments at the exact moments something feels off. This is the quality
  gate — the pipeline never skips it.
- **Approve publishing.** A "good to publish" comment from the reviewer is
  what unlocks step 3 above.
- **Flip the video public.** Claude uploads drafts only. A human presses the
  actual publish button in YouTube Studio.
- **Bilibili uploads** are still manual (waiting on API access) — see
  `docs/cn_staff_handoff.html` for that checklist.
- **Hold the keys.** The two paid API keys come from Wayne via password
  manager — never Slack or email.

That's the whole job. Everything else — timing, syncing the voice to the
picture, subtitle sizing, not double-publishing, not overspending on the paid
voice API — is enforced by the machinery, not by you remembering things.

---

## Part 2 · Getting set up

You need a Mac, about 15 minutes, and three things before you start:

- the **Claude desktop app** (or Claude Code in a terminal), signed in with
  your Gravgear team account
- **Google Drive for desktop**, signed in, with the `General` shared drive
  available
- your **two API keys** from Wayne (ElevenLabs + KIE, via password manager)

Then pick ONE of the two paths below.

### Path A — Claude desktop app (for everyone)

No terminal. You paste two messages and fill in two blanks; Claude does the
actual installing.

**Step 1.** Open the Claude app, start a new conversation, and paste this
whole block:

> Set up the cn-dub-pipeline plugin on this Mac for me:
> 1. Make sure Homebrew is installed, then `brew install ffmpeg-full python@3.14`.
> 2. In `~/.claude/settings.json`, add the marketplace and enable the plugin:
>    `extraKnownMarketplaces: {"gravgear-tools": {"source": {"source": "github", "repo": "yelllowdude/cn-dub-pipeline"}}}`
>    and `enabledPlugins: {"cn-dub-pipeline@gravgear-tools": true}` (merge with
>    whatever is already there, don't overwrite other settings).
> 3. Tell me when to restart the app.

Approve what it asks to run, and restart the app when it says so.

**Step 2.** In a fresh conversation, paste:

> run cn-pipeline-setup

Claude creates the working folders and then shows you exactly where to paste
your two API keys and confirms your Google Drive path. Paste the keys where
it points you (you type the keys, not Claude — they're secrets).

**Step 3.** Check it worked. Ask:

> what skills are available?

If `localize-chinese` is in the list, you're done. Ready for
`localize {project-id} for Chinese`.

### Path B — terminal (Claude Code CLI)

Same result, four commands:

```
brew install ffmpeg-full python@3.14      # Homebrew first if needed: https://brew.sh
claude                                     # then inside the session:
/plugin marketplace add yelllowdude/cn-dub-pipeline
/plugin install cn-dub-pipeline@gravgear-tools
```

Then say `run cn-pipeline-setup`, add your two keys and Drive path to the
`.env` / `config.json` it creates (it prints the exact location — under
`~/.claude/plugins/data/`, safe from plugin updates), and confirm
`localize-chinese` shows up under "what skills are available?".

### Before the first real run

Do one dry run against a project that's already been localized (ask Wayne
which — there's a known-good baseline) and check the output durations match
the published files in that project's Drive `/CN/` folder.
`docs/VALIDATE.md` is the copy-paste runbook.

---

## Part 3 · The machine part

Everything below is reference for people working on the pipeline itself.
`docs/cn_workflow.html` is the source of truth for stage rules, thresholds
and exceptions; `CLAUDE.md` covers the working conventions for changing this
code.

### What actually happens during "localize"

The pipeline's default is **native dub mode** (dub-first): instead of forcing
Chinese speech into the English cue timing (which sounds rushed), the Chinese
is written as natural spoken passages, voiced at natural pace, and synced to
the picture through **beats** — each 1–2 sentences pinned to the English
segment whose meaning they carry, because the animation was timed to the
English narration. Subtitles are then derived *from* the finished dub, so cue
breaks land at natural Chinese sentence boundaries.

Stage by stage:

1. **Transcribe** — Whisper transcription of the English master → cue-level
   segments (`align extract-audio` / `transcribe`, `subtitles split-cues`).
2. **Anchors** — scene cuts + speech gaps propose visual sync points
   automatically; the operator reviews them (`anchors detect` / `validate`).
3. **Native script** — the operator (Claude, live judgment) writes
   beat-tagged natural Chinese, glossary-checked (`glossary/cn_glossary.md`
   is law: product names stay English). Ingested via
   `subtitles ingest-script`.
4. **Dub** — one ElevenLabs take per passage at natural pace
   (`dub generate`). A passage that doesn't fit its window is fixed by
   **tightening the wording, never by speeding up speech** (atempo hard cap
   1.06). `dub finalize` cuts takes at inter-sentence silences and places
   each beat on its visual timestamp; `dub tighten` + `dub mix-me` add the
   Demucs-separated M&E bed.
5. **Subtitles from the dub** — `subtitles split-zh-cues` (hard 20-char cap)
   + `align align-dub` (forced alignment to the actual speech).
6. **Render + gates** — `render cndub`, then two close-out gates:
   `dub verify-anchors` (every beat's speech onset measured from the real
   audio, ±500ms) and `render verify` (duration).
7. **Review loop** — `review submit` uploads to Frame.io as a version stack
   (stable share link across re-cuts); `review fetch` / `apply` pull
   time-coded comments back and apply the mechanical fixes.
8. **Publish** — `publish youtube` uploads the highest `_vN` as a private
   draft + sets the CN thumbnail.

The older cue-locked mode still exists per-project
(`runs/{id}/project.json`); existing projects are untouched by the native
default.

### Guard rails

- **Re-running is safe.** Every stage prints `SKIP_OK` when its outputs are
  current; `--force` redoes a stage and downstream stages rerun
  automatically. Paid TTS takes are cached against their exact text — editing
  one passage re-buys only that passage.
- **Paid calls are capped per run** (`max_*_calls_per_run` in `config.json`,
  counter in `runs/{id}/api_spend.json`). Hitting a cap is a
  flag-it-to-Wayne moment, not a prompt to raise the cap.
- **Versioned deliverables.** A re-cut renders as `{id}_cndub_v2.mp4` next to
  v1, never over it. Publishing takes the highest version present, and the
  publish skill refuses to double-publish a row that already has a link.

### Repo layout

```
.claude-plugin/        plugin.json + marketplace.json — this repo is both
skills/                the Claude skills that drive the CLI (localize-chinese, publish-chinese)
.claude/skills/        symlink to the same files for local-dev auto-discovery
bin/                   cn-pipeline (CLI wrapper) + cn-pipeline-setup (one-time env setup)
cn_pipeline/           the package — one module per pipeline stage concept
docs/                  cn_workflow.html (rules) · cn_staff_handoff.html (upload steps) · VALIDATE.md · frameio_review.md
glossary/              locked terms + formatting conventions, checked on every translation
runs/{project-id}/     per-run working data (gitignored)
tests/                 pure-logic tests, run as plain python (no pytest, no media)
```

Secrets and per-machine config (`.env`, `config.json`, the Python venv) live
under `~/.claude/plugins/data/<plugin-id>/` when installed as a plugin — a
plugin update never touches them. Nothing in `runs/` or any media file is
ever committed.

### Local dev clone (only for changing the pipeline's code)

```
git clone https://github.com/yelllowdude/cn-dub-pipeline
cd cn-dub-pipeline
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # edit drive_root
cp .env.example .env                 # edit API keys
```

Launch Claude Code from inside the directory — `.claude/skills/` is
auto-discovered. Run the tests with `python tests/test_<name>.py`.

### If something's broken

Check `runs/{project-id}/*.log` and `*_log.json` first. Don't blind-retry a
paid stage — TTS and thumbnail cleaning cost real money per call. If the
cause isn't obvious from the log, flag it to Wayne.
