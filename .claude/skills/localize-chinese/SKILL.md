---
name: localize-chinese
description: Run the Chinese dub localization pipeline for a Gravgear/Yellow Dude video project — transcribe, translate, dub, align, render, thumbnail. Trigger phrase — "localize {project-id} for Chinese".
---

# Localize a video for Chinese

This skill orchestrates `cn_pipeline`'s CLI (`python -m cn_pipeline.cli ...`,
run with the repo's `.venv` active) through the mechanical stages, and tells
you where to apply live judgment yourself — translation quality, which title
to pick, what the thumbnail headline says. **Those three are never scripted.**
That's deliberate: the pipeline's value is doing the mechanical parts
(timing, alignment, rendering) identically every time; the creative calls
still need a person (or you) actually reading and deciding, not a script
guessing.

**The rules and thresholds below live in `docs/cn_workflow.html`, not here.**
This file is the *sequence*, not the *rulebook* — before applying any gate,
threshold, or naming convention, re-read the relevant section of that doc.
Don't rely on memory of a previous run; the doc can be updated independently
of this skill, and if it has been, the doc wins.

## Before starting

Run `python -m cn_pipeline.cli preflight --project-id {id}`. If it errors on
missing `.env`/`config.json`, stop and point the user at `README.md` — don't
try to work around a missing environment.

## Stage sequence

1. **Pre-flight** (mechanical) — already run above. Confirms the master
   video and ffmpeg resolve; reports whether `{id}_me.wav` exists.

2. **Transcribe** (mechanical):
   ```
   python -m cn_pipeline.cli align extract-audio --project-id {id}
   python -m cn_pipeline.cli align transcribe --project-id {id}
   python -m cn_pipeline.cli subtitles split-cues --project-id {id}
   ```
   This writes `runs/{id}/segments.json`.

3. **Translation — live, not scripted.** Read `runs/{id}/segments.json`,
   translate the whole transcript to Chinese *in context as one document*
   (not line-by-line — a term drifting across cues is the exact failure this
   guards against), checked against `glossary/cn_glossary.md`. Write the
   result as a JSON array of strings, same order, same length as
   `segments.json`. Then:
   ```
   python -m cn_pipeline.cli subtitles ingest-translation --project-id {id} --zh-json path/to/your/translation.json
   python -m cn_pipeline.cli subtitles build-srt --project-id {id}
   ```

4. **Title pick — live.** Generate 3 CN title options per `docs/cn_workflow.html`'s
   meta-pack rules, pick one (present to the user if they're around, otherwise
   apply the same judgment yourself), format the winner into the V1/V2
   ready-to-paste strings per the title-suffix convention in
   `glossary/cn_glossary.md`. This becomes part of the Notion page content,
   not a CLI step — no script call here.

5. **Thumbnail — live text, mechanical render.** Decide the Chinese headline
   text (and sub-line, if the source thumbnail has one) per
   `docs/cn_workflow.html` §2's tone. Write `runs/{id}/thumb_config.json`
   (schema documented in `cn_pipeline/thumbnail.py`'s module docstring). Then:
   ```
   python -m cn_pipeline.cli thumbnail clean --project-id {id}
   python -m cn_pipeline.cli thumbnail render --project-id {id}
   ```

6. **Dub generation + the mandatory fit-gate** (mechanical, but the gate
   *rules* must be re-read from `docs/cn_workflow.html` Stage 4 before acting —
   don't apply cached thresholds from memory):
   ```
   python -m cn_pipeline.cli dub generate --project-id {id}
   ```
   If it reports capped chunks:
   ```
   python -m cn_pipeline.cli dub fix --project-id {id}
   ```
   Always, unconditionally, whether or not any chunk was capped:
   ```
   python -m cn_pipeline.cli dub finalize --project-id {id}
   python -m cn_pipeline.cli dub tighten --project-id {id}
   ```
   `dub tighten` compares the assembled dub track's length against the
   source video's actual duration and pads trailing silence if it undershoots.
   If it reports padding more than a couple of seconds, **check the source
   video's tail actually is a silent/non-verbal outro** before trusting it —
   don't assume every undershoot is the documented exception.

7. **Forced alignment** (mechanical):
   ```
   python -m cn_pipeline.cli align align-dub --project-id {id}
   ```
   Writes `{id}_bilingual_cndub.srt`. If it reports more than 1-2 monotonic-clamp
   overlaps fixed, that's worth a second look, not just a pass-through.

8. **Render** (mechanical):
   ```
   python -m cn_pipeline.cli render ensub --project-id {id}
   python -m cn_pipeline.cli render cndub --project-id {id}
   ```
   Confirm both output durations match the source video closely (within ~0.1s)
   before treating the run as done — a bigger mismatch means something
   upstream broke, not something to re-render-and-hope past.

9. **Hand off.** Everything from here — uploading to Bilibili, scheduling,
   what "good" output looks like on review — is `docs/cn_staff_handoff.html`'s
   job, not this skill's. Write the Notion Chinese DB row (title, description,
   tags, ad-disclosure section if `Contains ads?`) per `docs/cn_workflow.html`'s
   Data Model section, set `Status: Reviewer: review draft`, and stop.

## If something fails partway

Check `runs/{id}/*.log` and `runs/{id}/*_log.json` for the stage that failed.
Don't silently retry a paid stage (TTS generation, the KIE thumbnail call)
with different parameters hoping it works — flag what happened and why
before spending more API calls on it.
