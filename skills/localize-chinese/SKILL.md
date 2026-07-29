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

**Claim the project first, in both storage modes.** Two operators on the same
video double-spend the TTS budget and fork the Frame.io version stack into two
links nobody can reconcile.

```
cn-pipeline drive pull --project-id {id}
```

In `gdrive` mode this downloads the master, `{id}_me.wav`, everything in
`/CN/`, and the shared scratch state (translations, paid TTS cache, Frame.io
review state, spend counter). In `mount` mode the media is already on disk, so
it restores the shared scratch and claims. Either way it **claims the
project**, and every later command refreshes the claim automatically. If the
claim is refused because someone else holds it, **stop and tell the user who
has it** — never `--steal` on your own initiative.

`cn-pipeline drive status --project-id {id}` says who holds it. A claim idle
for 12h+ is stale and is taken over automatically; if that happens, say so,
because it means someone's run died partway.

Release at hand-off (step 10) — an unreleased claim blocks your teammates.

### Announce the claim in Notion

Create the Chinese-DB row **now, at claim time**, not at the end of the run. An
empty row that exists is how a teammate discovers the video is being worked;
a perfect row that appears three hours later tells them nothing.

The row needs only its title and the project link at this point. Then post
**one comment** on it:

> 🤖 *Claude posted this, not a human.*
> Claimed for Chinese localization — {operator}@{machine}, {timestamp}.
> Frame.io link and deliverables land on this row when the run finishes.

Use `cn-pipeline drive status --project-id {id}` for the operator/machine
values so the comment and the lock file agree.

**Do not add Notion properties for this.** The claim lives in
`CN/_pipeline/claim.json` (that's what actually enforces it) and the comments
are the human-readable trail. Two comments per row, total: this one, and the
done comment in step 10.

Run `cn-pipeline preflight --project-id {id}`. If it errors on a missing
Python environment, `.env`, or `config.json`, stop and tell the user to run
`cn-pipeline-setup` once first (see README) — don't try to work around a
missing environment.

Pre-flight also prints the Stage 1 checks it *can't* do mechanically —
winning title from the Notion source row, thumbnail source, sponsor
detection. Those are yours; the sponsor check is scheduled explicitly in
stage 2 below.

**Mode — new videos run in native dub mode.** Set it before stage 3:
`cn-pipeline mode set --project-id {id} --dub-mode native`. The native
sequence lives at the end of this file and replaces steps 3, 6 and 7 of the
sequence below; everything else is shared. Cue-locked (steps 1–10 exactly as
written) is legacy — kept only for existing cue-locked projects, not chosen
for new work. The mechanical default for an unset project is still
`cue_locked` (so old runs are never silently changed), which is why setting
native is an explicit one-liner rather than automatic.

**Re-runs are safe by default.** Every mechanical command prints `SKIP_OK`
and does nothing when its outputs are already up to date; pass `--force` to
redo a stage, and downstream stages then rerun automatically (their inputs
are now newer). Paid TTS chunks are cached against the exact text that
generated them, so a rerun after a translation edit only re-spends on the
chunks whose lines actually changed — never trust or hand-edit the files in
`runs/{id}/chunks/` to game this.

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

   **Then the sponsor check — live, and it happens here, not at upload.**
   Scan `segments.json` for sponsor mentions and promo-code patterns, and
   check the source Notion row's sponsor field. Record the verdict now: it
   drives the `Contains ads?` checkbox, the sponsor CTA's placement in the
   description, and the `# CN ad disclosure` section in step 9 (rules in
   `cn_workflow.html` Stage 2 and the glossary's ad-disclosure boilerplate).
   This check previously lived only in the rulebook's Stage 1 with no owner
   in this sequence — a missed check here is a compliance problem, not a
   formatting one.

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

   **In-screen text localization — OPTIONAL, experimental, off by default.**
   Only if `config.json` has `"screentext_enabled": true`. This translates
   baked-in text *inside the video frames* (lower-thirds, labels, on-screen
   graphics) into Chinese, so the picture itself is localized — not just the
   subtitles. It handles fixed-position overlay text and *flags* moving text
   for a human rather than smearing a stale patch across it (see
   `cn_pipeline/screentext.py`'s docstring for the boundary). Run:
   ```
   cn-pipeline screentext detect --project-id {id}
   ```
   Read `runs/{id}/screentext/screentext_events.json`, translate each event's
   `text` to Chinese (live, glossary-checked, same discipline as the
   subtitles), write a JSON array of strings in the same order, then:
   ```
   cn-pipeline screentext ingest-translation --project-id {id} --zh-json path/to/screentext_zh.json
   cn-pipeline screentext localize --project-id {id}
   ```
   `localize` composites Chinese patches onto the master → a localized master
   the render stage (step 8) picks up automatically. It prints any unstable
   (moving) events it skipped — decide those by hand. If the flag is off, skip
   this entirely; renders use the raw master. **This is a "see how it works"
   feature: if the output looks wrong, set the flag back to false and the
   pipeline reverts with zero other changes.**

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
   (music + effects, no VO).

   **If you generated the bed yourself, say so:** `--me-source generated`. A
   staff-prepped bed is mixed at full level and gets pulled down under the
   voice; a Demucs-separated bed is thinner and gets a boost instead. One gain
   can't serve both, and the wrong one either buries the bed or lets it fight
   the dub. It's recorded in `project.json`, so pass it once — and changing it
   re-mixes rather than skipping past a bed at the wrong level. Default is
   `provided`, which is correct whenever staff prepped the file.

   You cannot infer this from the file: measured across real projects, the
   separated and staff-prepped beds' overall levels overlap completely. Declare
   it from how the file was actually produced.

   If `{id}_me.wav` doesn't exist at the project
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

8. **Render + verify** (mechanical):
   ```
   cn-pipeline render ensub --project-id {id}
   cn-pipeline render cndub --project-id {id}
   cn-pipeline render verify --project-id {id}
   ```
   `render verify` is the close-out gate: it fails loudly if either output's
   duration is off the source by more than ~0.1s. A failure means something
   upstream broke — diagnose it, don't re-render-and-hope past it. The run
   isn't done until it prints PASS.

9. **Native-speaker review — Frame.io.** Submit the finished dub for a native
   speaker to review with time-coded comments, then fold their feedback back
   in. Submit:
   ```
   cn-pipeline review submit --project-id {id}
   ```
   This uploads a **1080p proxy**, not the 4K deliverable — a 10-minute video
   is 1.4 GB at 4K and ~310 MB as a proxy, and the reviewer is judging
   translation, register and sync, none of which 4K carries. Burned subtitles
   stay exactly as legible (they're sized as a fraction of frame height) and
   comment framestamps map to the same cues. Pass `--full` only when someone is
   specifically checking image quality.

   Paste the returned link into the Chinese DB's `Frame.io link` field and set
   `Status: In review`. (Until the Frame.io upload API is verified, `submit`
   will tell you to upload the `{id}_cndub.mp4` by hand and share it — the rest
   of the loop works regardless.) When the reviewer is done, pull and apply:
   ```
   cn-pipeline review fetch --project-id {id} --asset-id <id>      # or --comments-json <exported.json>
   cn-pipeline review apply --project-id {id}
   ```
   `fetch` resolves every comment to its exact cue and splits them into
   auto-fixable (a term/typo with a concrete replacement) vs needs-a-human
   (pacing, sync, anything vague) — written to `review_report.md`. `apply`
   makes only the mechanical fixes to `zh.json`, per-cue, never a blanket
   swap, and tells you which stages to re-run (the SKIP_OK gates redo only
   what's downstream of the edited translation). **Work the needs-a-human
   queue yourself** — those are judgment calls the tool deliberately won't
   guess. Re-render, re-submit if the changes were substantial, and only move
   on once the reviewer signs off.

   **gdrive mode: push after every deliverable-producing stage boundary** —
   at minimum after step 8's renders pass verify, and again after any
   review re-cut — with:
   ```
   cn-pipeline drive push --project-id {id}
   ```
   Uploads are md5-diffed (unchanged files cost nothing), and the push also
   syncs the paid TTS cache + review state to `/CN/_pipeline/scratch/` so
   any teammate can resume the project without re-spending. Keep the claim
   while the review loop is open; release it at hand-off (step 10).

10. **Hand off.** Everything from here — uploading to Bilibili, scheduling,
   what "good" output looks like on review — is `docs/cn_staff_handoff.html`'s
   job, not this skill's.

   **Fill in the row you created at claim time.** Author the creative fields
   into `runs/{id}/notion_row.json`, then let the CLI validate and lay it out:

   ```
   cn-pipeline notion row --project-id {id} --init    # first time only
   cn-pipeline notion row --project-id {id}           # validate + render
   ```

   You write `title_zh`, `title_options_zh` (all 3 candidates from step 4),
   `description_zh`, `tags_zh`, `contains_ads` and — if there is a sponsor —
   `ad_disclosure_zh`. The command fills the mechanical fields from what
   actually shipped, refuses to render until the row is complete, and prints
   the page body from one fixed template so every operator's row has the same
   shape. Post that output; don't re-improvise the layout.

   `contains_ads` has **no default** — it's a legal disclosure and can't be
   derived from anything on disk, so validation fails until you state it. Check
   the source Notion row's sponsor field and the English audio; if the English
   video never says a code, the code does not go in the dub (description only).

   Set `Status: Review draft`, then post the **second and final comment**:

   > 🤖 *Claude posted this, not a human.*
   > Localization run finished — released, {timestamp}.
   > Frame.io: {review link} · deliverables in `/CN/` · dub mode: {mode}
   > Needs a human: {anything from the review queue you couldn't resolve, or "nothing"}

   That "needs a human" line is the point of the comment — it's the one place
   an unresolved judgment call gets recorded where the next person will see it.

   **Product names stay in English everywhere** (titles, descriptions, subs,
   dub): "Pistol Squat Cheat Sheet", "Playbook", etc. — see the glossary's
   locked-terms table. Translate around the name, never the name.

   **Final push + release — both storage modes.** The hand-off isn't done until
   the shared scratch state is on Drive and the claim is returned. Leaving a
   claim held blocks every teammate for 12 hours until it goes stale:
   ```
   cn-pipeline drive push --project-id {id} --release
   ```

   **Publish-status reminder block.** `notion row` already renders this at the
   TOP of the page body — don't hand-write a second copy, or the two drift.
   It's a 💡 callout, temporary while Bilibili API access is pending, and the
   whole block gets deleted once it's live:

   > Delete this reminder once Bilibili API access is live.
   > Link in a publish property = published.
   > Publish statuses:
   > - [ ] ENsub Bilibili
   > - [ ] CNdub Bilibili
   > - [ ] CNdub YouTube

   The three to-dos mirror the three URL properties (`ENsub Bilibili`,
   `CNdub Bilibili`, `CNdub YouTube`). Whoever fills a link property checks
   (and strikes through) the matching to-do — the publish skill does this for
   its own uploads. The wording lives in `cn_pipeline/notion_row.py`
   (`REMINDER_BLOCK`); change it there, not here.

## If something fails partway

Check `runs/{id}/*.log` and `runs/{id}/*_log.json` for the stage that failed.
Don't silently retry a paid stage (TTS generation, the KIE thumbnail call)
with different parameters hoping it works — flag what happened and why
before spending more API calls on it.

That rule is now also enforced mechanically: paid calls count against
per-run caps (`runs/{id}/api_spend.json`, caps in `config.json`), and a
call past the cap raises instead of spending. If you hit a `SpendCapError`,
that *is* the flag — report what burned the budget; don't raise the cap or
delete the counter to push through without the user's say-so.

## Native dub mode (dub-first, anchor-synced) — the default for new videos

The cue-locked sequence above tempo-forces Chinese speech into the English cue
grid; native reviewers consistently read that as "rushed / feels off". **Native
mode is the default for every new video** and inverts the priority: **write
natural Chinese first, dub it at natural pace synced to visual anchors, derive
the subtitles from the finished dub.** Set it explicitly per project (the
mechanical default for an unset project stays cue_locked so existing runs are
untouched):

```
cn-pipeline mode set --project-id {id} --dub-mode native
```

Native-mode sequence (replaces steps 3, 6, 7 above; everything else — 
pre-flight, transcribe, sponsor check, title, thumbnail, M&E, render, review,
publish — is unchanged):

1. `cn-pipeline anchors detect --project-id {id}` → `anchor_candidates.json`
   AND an auto-proposed `anchors.json` (scene cuts that land in English speech
   gaps, 30–75s spacing). **Anchors are automatic most of the time — you (the
   operator) review the proposal against the picture and adjust only where a
   must-sync moment is missing or misplaced (product shots, on-screen text,
   chapter turns). Never ask the user to pick anchors.** Then `anchors validate`.
2. **Write the native script — live, BEAT-TAGGED.** One natural
   spoken-Chinese paragraph per anchor window in `zh_script.json`, written as
   beats: `{"passages": [{"anchor_id", "beats": [{"text", "en_seg"}]}]}` —
   each beat is 1–2 sentences tagged with the English segment whose meaning it
   carries. The English cue grid is the visual beat map, so finalize places
   each beat on its segment's timestamp: pauses land where the original
   narrator breathed instead of pooling as dead air before the next anchor
   (reviewer-confirmed failure of untagged passages, 2026-07-21). Ground each
   beat in the English VO's essential meaning, facts, numbers and jokes — but
   never translate word-by-word. Write the Chinese as if the video never had
   an English voiceover. Glossary still law; product names stay English. Aim
   for ~90-95% of each window at natural pace.
   Then: `cn-pipeline subtitles ingest-script --project-id {id} --script-json …`
3. `cn-pipeline dub generate --project-id {id}` — TTS per passage, natural
   pace. Overflowing passages are flagged with a target char count: **tighten
   the WORDING** (never the tempo), re-ingest, re-run — the cache only re-buys
   edited passages. When clean: `dub finalize`, then `dub mix-me` as usual.
4. `cn-pipeline subtitles split-zh-cues --project-id {id}` → Chinese cue lines
   at natural sentence breaks, then `align align-dub` (subtitles come FROM the
   dub; **Chinese-only** on the CN dub — the ENsub variant stays bilingual and
   still needs the per-cue `zh.json` translation from step 3 of the classic
   sequence).
5. Close-out gates: `cn-pipeline dub verify-anchors` (every passage's speech
   onset within ±500ms of its anchor, no bleed) AND `render verify`, then the
   normal Frame.io review.
