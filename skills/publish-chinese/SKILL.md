---
name: publish-chinese
description: Publish approved Chinese cuts — upload the CNdub to the Chinese YouTube channel as a private draft and file the links back into the Notion Chinese database. Triggers — "publish pending Chinese videos" (scan) or "publish {project-id} to Chinese channels" (single project).
---

# Publish approved Chinese videos

This skill is the orchestration half of the publish stage: it decides WHAT to
publish by reading the Notion Chinese database, drives `cn-pipeline publish`
for the mechanical upload, and writes the resulting links back. Notion is the
team's interface — a human queues a video there, and the links land back
there. The CLI never talks to Notion; this skill never uploads bytes.

**Scope lock: this skill only operates on rows of the Chinese database**
(collection `5b3aefbc-a03a-47c9-a026-7a865f0a3d62`, under Marketing → Video →
Chinese database). If asked to publish something that has no row there, stop
and say so — don't improvise against the main YouTube database.

## Platform status

- **YouTube (Chinese channel @yellowdude_zh):** LIVE. The CNdub uploads as a
  PRIVATE draft; the link exists immediately and a human flips it public in
  YouTube Studio when ready.
- **Bilibili:** NOT WIRED — waiting on official API access. When it lands,
  both variants go there (ENsub → `ENsub Bilibili`, CNdub → `CNdub Bilibili`) and
  `cn-pipeline publish bilibili` replaces its current not-implemented stub.
  Until then this skill uploads YouTube only and says so in its report.

## Queue semantics

A row is queued when the **`Publish requested` checkbox is checked**. The
checkbox only records intent — nothing watches it; publishing happens when a
human runs this skill (that's deliberate: no status-change side effects).

## Steps

1. **Find the queue.** Query the Chinese database for rows with
   `Publish requested` checked. If invoked for one specific project-id, use
   that row regardless of the checkbox, but say whether it was checked.

2. **Pre-check each row (mandatory, prevents double-publishing):**
   - `CNdub YouTube` already filled → SKIP the row and report it as already
     published; do not upload a duplicate. Leave the checkbox for a human to
     clear (they may have re-checked it deliberately and should see the skip).
   - `Status` is not `Ready to publish` → flag it loudly in the report and
     skip unless the user explicitly says to publish anyway.
   - Confirm the deliverable exists in Drive `/CN/`:
     `{id}_cndub.mp4`, or the highest `_v{N}` revision if review produced
     re-cuts — **always publish the highest version present.** On a machine
     in gdrive storage mode, run `cn-pipeline drive pull --project-id {id}
     --no-claim` first so the local mirror has the latest cut and
     `{id}_cover.jpg` (publishing doesn't modify the project, so no claim
     is needed).

3. **Gather metadata — from the Notion row only, never invented:**
   - Title: the row page's `# CN title → V2 · 中配` line, exactly as written
     (it already carries the `【中配】` suffix per the glossary convention).
   - Description: the row page's `# CN description` code fence, verbatim.
   - Tags: the row page's `# CN tags` line, comma-split.
   If any of the three is missing from the page, stop for that row and report
   it — a publish with fabricated metadata is worse than a skipped one.

4. **Upload** (mechanical, one call per row). **Immediately before this call,
   re-read the row's `CNdub YouTube` property one more time** — not the value
   you fetched in step 2. Metadata-gathering can take minutes, and a teammate
   publishing in parallel lands exactly in that gap; a duplicate draft cannot
   be deleted by the pipeline (upload-only scope), so the last look wins:
   ```
   cn-pipeline publish youtube --project-id {id} [--version vN] \
       --title '{V2 title}' --description-file {tmp} --tags '{tags}'
   ```
   The command prints `{video_id, link, privacy, thumbnail}`. privacy must come
   back `private` — anything else is a stop-and-report. The CN thumbnail
   (`{id}_cover.jpg` from `/CN/`) is set automatically; if the `thumbnail`
   field reports a failure, flag it — the human sets it in Studio.

5. **Write back to the Notion row — the `CNdub YouTube` link FIRST, before
   any other write or the next row's upload.** That property is the
   double-publish guard for everyone else; every second it stays empty after
   a successful upload is a second another operator can duplicate the draft.
   - `CNdub YouTube` ← the returned link
   - `Publish requested` ← unchecked
   - In the page's publish-status reminder block (the callout at the top —
     see localize-chinese step 10): check the `CNdub YouTube` to-do and
     strike it through. Leave the Bilibili to-dos alone.
   - Append one line to the page's `# 📋 Run log`: date, "CNdub uploaded to
     YouTube as private draft", the link, and which file version went up.
   - Leave `Status` alone: `Published` means live-to-viewers, and these are
     drafts. When Bilibili is wired and all links are in, the human flipping
     things live owns that status change.

6. **Report.** One line per row: published (with link) / skipped-already-done /
   skipped-missing-metadata / flagged-wrong-status. Plus the standing note
   that Bilibili is pending API access.

## If something fails partway

An upload that died mid-transfer leaves NO draft (YouTube only materializes
the video when the last byte lands) — safe to re-run. If the CLI errored
after printing a video id but before Notion was updated, write the link back
by hand rather than re-uploading — a second run would create a duplicate
draft on the channel that upload-scope credentials cannot delete.
