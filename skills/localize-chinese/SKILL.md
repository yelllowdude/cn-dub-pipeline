---
name: localize-chinese
description: Run the Chinese dub localization pipeline for a Gravgear/Yellow Dude video project — transcribe, translate, dub, align, render, thumbnail. Trigger phrase — "localize {project-id} for Chinese".
---

# Localize a video for Chinese

This skill orchestrates `cn_pipeline`'s CLI via the `cn-pipeline` command (a
wrapper that resolves the plugin's own Python environment, so it works
regardless of your current working directory) through the mechanical stages,
and tells you where to apply live judgment yourself — translation quality,
which title to pick, what the thumbnail headline says. **Those three are
never scripted.** That's deliberate: the pipeline's value is doing the
mechanical parts (timing, alignment, rendering) identically every time; the
creative calls still need a person (or you) actually reading and deciding,
not a script guessing.

**The rules and thresholds below live in `docs/cn_workflow.html`, not here.**
This file is the *sequence*, not the *rulebook* — before applying any gate,
threshold, or naming convention, re-read the relevant section of that doc
(find it at `${CLAUDE_PLUGIN_ROOT}/docs/cn_workflow.html`). Don't rely on
memory of a previous run; the doc can be updated independently of this
skill, and if it has been, the doc wins.

## Before starting

Run `cn-pipeline preflight --project-id {id}`. If it errors on a missing
Python environment, `.env`, or `config.json`, stop and tell the user to run
`cn-pipeline-setup` once first (see README) — don't try to work around a
missing environment.

## Stage sequence

1. **Pre-flight** (mechanical) — already run above. Confirms the master
   video and ffmpeg resolve; reports whether `{id}_me.wav` exists.

2. **Transcribe** (mechanical):
   ```
   cn-pipeline align extract-audio --project-id {id}
   cn-pipeline align transcribe --project-id {id}
   cn-pipeline subtitles split-cues --project-id {id}
   ```
   This writes `runs/{id}/segments.json`.

3. **Translation — live, not scripted.** Read `runs/{id}/segments.json`,
   translate the whole transcript to Chinese *in context as one document*
   (not line-by-line — a term drifting across cues is the exact failure this
   guards against), checked against
   `${CLAUDE_PLUGIN_ROOT}/glossary/cn_glossary.md`. Write the result as a
   JSON array of strings, same order, same length as `segments.json`. Then:
   ```
   cn-pipeline subtitles ingest-translation --project-id {id} --zh-json path/to/your/translation.json
   cn-pipeline subtitles build-srt --project-id {id}
   ```

4. **Title pick — live.** Generate 3 CN title options per `cn_workflow.html`'s
   meta-pack rules, pick one (present to the user if they're around, otherwise
   apply the same judgment yourself), format the winner into the V1/V2
   ready-to-paste strings per the title-suffix convention in
   `glossary/cn_glossary.md`. This becomes part of the Notion page content,
   not a CLI step — no script call here.

5. **Thumbnail — live text, mechanical render.** Decide the Chinese headline
   text (and sub-line, if the source thumbnail has one) per
   `cn_workflow.html` §2's tone. Write `runs/{id}/thumb_config.json`
   (schema documented in `cn_pipeline/thumbnail.py`'s module docstring). Then:
   ```
   cn-pipeline thumbnail clean --project-id {id}
   cn-pipeline thumbnail render --project-id {id}
   ```

6. **Dub generation + the mandatory fit-gate** (mechanical, but the gate
   *rules* must be re-read from `cn_workflow.html` Stage 4 before acting —
   don't apply cached thresholds from memory):
   ```
   cn-pipeline dub generate --project-id {id}
   ```
   If it reports capped chunks:
   ```
   cn-pipeline dub fix --project-id {id}
   ```
   Always, unconditionally, whether or not any chunk was capped:
   ```
   cn-pipeline dub finalize --project-id {id}
   cn-pipeline dub tighten --project-id {id}
   ```
   `dub tighten` compares the assembled dub track's length against the
   source video's actual duration and pads trailing silence if it undershoots.
   If it reports padding more than a couple of seconds, **check the source
   video's tail actually is a silent/non-verbal outro** before trusting it —
   don't assume every undershoot is the documented exception.

   Then, always, whether or not `{id}_me.wav` exists (it no-ops cleanly if not):
   ```
   cn-pipeline dub mix-me --project-id {id}
   ```
   Mixes the tightened VO with the project's `{id}_me.wav` background bed
   (music + effects, no VO). If `{id}_me.wav` doesn't exist at the project
   root and one should be generated (staff hasn't prepped one, but the
   source video clearly has a music/effects bed worth keeping under the
   dub), separate it from the master's full audio track with Demucs
   (`--two-stems=vocals`, use the `no_vocals` stem) rather than skipping —
   verify the separation is clean before trusting it (transcribe the
   `no_vocals` stem with Whisper; it should come back empty/near-empty) and
   save it to the project root as `{id}_me.wav` before continuing. `render
   cndub` prefers `dub_master_mixed.wav` when `dub mix-me` produced one,
   falling back to the VO-only track otherwise.

7. **Forced alignment** (mechanical):
   ```
   cn-pipeline align align-dub --project-id {id}
   ```
   Writes `{id}_bilingual_cndub.srt`. If it reports more than 1-2 monotonic-clamp
   overlaps fixed, that's worth a second look, not just a pass-through.

8. **Render** (mechanical):
   ```
   cn-pipeline render ensub --project-id {id}
   cn-pipeline render cndub --project-id {id}
   ```
   Confirm both output durations match the source video closely (within ~0.1s)
   before treating the run as done — a bigger mismatch means something
   upstream broke, not something to re-render-and-hope past.

9. **Hand off.** Everything from here — uploading to Bilibili, scheduling,
   what "good" output looks like on review — is `docs/cn_staff_handoff.html`'s
   job, not this skill's. Write the Notion Chinese DB row (title, description,
   tags, ad-disclosure section if `Contains ads?`) per `cn_workflow.html`'s
   Data Model section, set `Status: Review draft`, and stop.

## If something fails partway

Check `runs/{id}/*.log` and `runs/{id}/*_log.json` for the stage that failed.
Don't silently retry a paid stage (TTS generation, the KIE thumbnail call)
with different parameters hoping it works — flag what happened and why
before spending more API calls on it.
