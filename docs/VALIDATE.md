# Validation runbook — `max-strength_2026-03-12`

Run this on a **real machine** (a team Mac with the environment set up), not in
a cloud/web session — it needs Google Drive Desktop, `ffmpeg-full`, the Python
venv, and the API keys. Its job is to confirm the pipeline still produces
known-good output *and* that the recent fixes actually landed, before trusting
any of it on a new video.

Why this project: it already has a completed run and shipped output in its
Drive `/CN/` folder, so you have something to diff against. It's also the run
whose log surfaced the inter-chunk-silence timing bug — so it's the exact case
the fix needs to prove out.

---

## 0. Prerequisites (one-time)

- [ ] `ffmpeg-full` installed (`brew install ffmpeg-full` — plain `ffmpeg` lacks libass and silently skips subtitle burn-in)
- [ ] Python 3.14 + venv created via `cn-pipeline-setup`
- [ ] `.env` filled: `ELEVENLABS_API_KEY`, `KIE_API_KEY` (optional `FRAMEIO_TOKEN`)
- [ ] `config.json` filled: `drive_root` points at the synced `General` Shared Drive
- [ ] Google Drive Desktop running and the project folder actually synced locally (open the folder in Finder and confirm the `.mp4` is a real file, not a cloud placeholder)

Sanity-check the environment resolves before spending any API calls:

```
cn-pipeline preflight --project-id max-strength_2026-03-12
```

Expect it to print the project dir, the master video, `me.wav present: …`, the
ffmpeg path, and the list of already-existing `/CN/` deliverables (which will
`SKIP_OK` unless you force them). If it errors on a missing venv/`.env`/config,
stop and fix setup — don't work around it.

---

## 1. The headline check — the timeline fix

This is the single most important number. The previous run padded **4009ms** of
trailing silence because inter-chunk gaps were being dropped and dumped at the
end. After the fix, the pad should be **small** (the real trailing dead air was
~130ms).

Run the dub chain fresh (force it, so you're testing the new code, not a cached
result):

```
cn-pipeline align extract-audio --project-id max-strength_2026-03-12
cn-pipeline align transcribe   --project-id max-strength_2026-03-12
cn-pipeline subtitles split-cues --project-id max-strength_2026-03-12
# translate (live) -> ingest -> build-srt, per SKILL.md step 3
cn-pipeline dub generate --project-id max-strength_2026-03-12 --force
cn-pipeline dub fix      --project-id max-strength_2026-03-12   # only if generate reports capped chunks
cn-pipeline dub finalize --project-id max-strength_2026-03-12
cn-pipeline dub tighten  --project-id max-strength_2026-03-12
```

**PASS / FAIL:**
- [ ] `dub tighten` reports `pad_ms` in the low hundreds or less — **not** thousands. A pad back up near 4000ms means the fix did not take effect.
- [ ] `dub finalize`'s log (`runs/max-strength_2026-03-12/finalize_log.json`) shows a non-zero `inter_chunk_gap_ms` and `lead_ms` — proof the gaps are now being placed inline instead of lost.
- [ ] No `SpendCapError`. If you hit one, that's the spend guard doing its job — investigate what caused repeated paid calls before raising the cap.

---

## 2. Alignment + render, then the duration gate

```
cn-pipeline dub mix-me     --project-id max-strength_2026-03-12   # no-ops if no me.wav
cn-pipeline align align-dub --project-id max-strength_2026-03-12
cn-pipeline render ensub   --project-id max-strength_2026-03-12
cn-pipeline render cndub   --project-id max-strength_2026-03-12
cn-pipeline render verify  --project-id max-strength_2026-03-12
```

**PASS / FAIL:**
- [ ] `align align-dub` reports **0–2** monotonic-clamp overlaps. Many more = something upstream is off.
- [ ] `render verify` prints **PASS** (both outputs within ~0.1s of the source duration). The prior run measured master 572.033s, ensub +0.02s, cndub −0.0013s — expect the same ballpark.
- [ ] **Watch the dubbed video with the picture, second half especially.** The whole point of the fix is that the voice tracks the on-screen cuts. Compared against the previously-shipped `max-strength_2026-03-12_cndub.mp4`, the second-half drift the old run flagged should be gone. This is an eyes-and-ears check — the duration gate alone won't catch drift.

---

## 3. Idempotency + spend guards (fast, free)

```
cn-pipeline render cndub --project-id max-strength_2026-03-12          # expect: SKIP_OK
cn-pipeline render cndub --project-id max-strength_2026-03-12 --force  # expect: re-renders
```

- [ ] First call prints `SKIP_OK` and does nothing; `--force` re-runs it.
- [ ] Editing one line of the translation and re-running `dub generate` re-buys only the affected chunk's TTS (check `runs/.../api_spend.json` barely moves), never the whole video.

---

## 4. In-screen text localization — EXPERIMENTAL (optional)

Only if you want to try it. It's off by default.

```
# set  "screentext_enabled": true  in config.json first
cn-pipeline screentext detect --project-id max-strength_2026-03-12
# review runs/.../screentext/screentext_events.json, translate each event's text,
# write screentext_zh.json (same order/length), then:
cn-pipeline screentext ingest-translation --project-id max-strength_2026-03-12 --zh-json <path>
cn-pipeline screentext localize --project-id max-strength_2026-03-12
# then re-run render ensub/cndub/verify -- they auto-pick up the localized master
```

**What to check:**
- [ ] `detect` reports a sane event count and how many are unstable (moving) — those are skipped, listed, and left for you.
- [ ] On the localized render, the fixed-position on-screen text reads in Chinese and the patches don't obviously ghost or misalign. **If it looks wrong, set `screentext_enabled` back to `false` and the pipeline reverts with zero other changes** — that's the whole point of the flag.

---

## 5. Frame.io review loop (optional, offline-capable)

The live Frame.io upload/fetch API is not yet verified, so run the loop from an
exported comments file to exercise everything downstream of the API:

```
# have a native speaker comment on the cndub in Frame.io, export comments to JSON
cn-pipeline review fetch --project-id max-strength_2026-03-12 --comments-json <export.json>
cn-pipeline review apply --project-id max-strength_2026-03-12
```

- [ ] `review fetch` writes `review_report.md` splitting comments into auto-fixable vs needs-a-human, each resolved to a cue.
- [ ] `review apply` edits only the flagged lines in `zh.json` (per-cue, never a blanket swap), backs up the old translation to `zh.pre_review.json`, and tells you which stages to re-run. Work the needs-a-human queue yourself.

---

## If a check fails

Check `runs/max-strength_2026-03-12/*.log` and `*_log.json` for the failing
stage first. Don't blind-retry a paid stage (TTS, KIE) — flag what happened.
Most failures trace to one upstream cause worth fixing once, not per-video.
