"""
In-screen text localization: translate baked-in English text inside the video
frames (lower-thirds, labels, tier/grade callouts, exercise-name cards) into
Chinese, so the finished video's on-screen graphics read in the target
language -- not just the burned-in subtitles.

SCOPE / the one limitation to understand before trusting this
----------------------------------------------------------------
This localizes text that sits at a FIXED screen position for its whole
on-screen life -- i.e. overlay graphics the original edit composited on top of
the footage. That's the dominant case for this channel. The mechanism mirrors
the thumbnail stage exactly: an image model erases the English text off ONE
representative frame and rebuilds the background behind it, the Chinese text is
then rendered deterministically with the brand font onto that cleaned patch,
and the patch is composited back over the video for the text's time span.

Text that MOVES within the frame across its lifetime (baked into moving
footage, sliding transitions) can't be covered by a single static patch. The
detector measures each event's positional drift and marks unstable ones
`stable=false`; `build_localized_master` skips them and lists them so a human
can decide, rather than smearing a stale patch across moving video. This is a
loud, documented boundary -- not a silent best-effort.

Like the subtitle path, DETECTION and COMPOSITING are mechanical (identical
every run); the TRANSLATION of the detected strings is a live judgment call
made between `detect` and `localize`, glossary-checked, same as Stage 3.

Per-run data (sampled frames, detected events, cleaned patches, overlays) lives
under runs/{id}/screentext/ -- gitignored, regenerated on demand.
"""

import json
import subprocess
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from cn_pipeline.config import get_config
from cn_pipeline.spend import record_call
from cn_pipeline.thumbnail import (
    DEFAULT_FONT,
    KIE_CREATE_TASK_URL,
    KIE_RECORD_INFO_URL,
    KIE_UPLOAD_URL,
    _poll_kie_task,
)

# Detection defaults (overridable via thumb-style config or CLI later).
SAMPLE_FPS = 2.0            # OCR two frames a second -- enough to bound a text event's span
MIN_CONFIDENCE = 0.45       # drop low-confidence OCR noise
MIN_EVENT_MS = 500          # ignore a flash of text too brief to bother localizing
MIN_TEXT_LEN = 2            # single stray characters are almost always OCR noise
IOU_MATCH = 0.35            # boxes overlapping this much across frames = the same event
GAP_TOLERANCE_MS = 1200     # bridge a single OCR dropout (>1 sample interval at SAMPLE_FPS) without splitting one event in two
STABLE_DRIFT_FRAC = 0.03    # center may drift this fraction of the diagonal and still be "fixed position"
PATCH_PAD_FRAC = 0.06       # pad the cleaned crop this much beyond the OCR box, so we erase the whole glyph


# --- frame sampling + OCR ----------------------------------------------------

_reader = None


def _get_reader():
    """Lazy EasyOCR reader. Imported lazily so the rest of the pipeline (and
    the unit tests) don't pay the torch/model-load cost unless in-screen text
    localization is actually requested."""
    global _reader
    if _reader is None:
        import easyocr  # heavy (torch); intentionally imported only on demand
        _reader = easyocr.Reader(["en"], gpu=False)
    return _reader


def _probe_frame_size(ffmpeg_path: str, video: Path) -> tuple[int, int]:
    ffprobe = str(Path(ffmpeg_path).with_name("ffprobe"))
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=s=x:p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def _extract_frame(ffmpeg_path: str, video: Path, t_ms: int, out_png: Path) -> Path:
    subprocess.run(
        [ffmpeg_path, "-y", "-ss", f"{t_ms / 1000:.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(out_png)],
        capture_output=True, check=True,
    )
    return out_png


def _sample_timestamps(ffmpeg_path: str, video: Path, sample_fps: float) -> list[int]:
    from cn_pipeline.render import probe_duration_ms
    dur_ms = probe_duration_ms(ffmpeg_path, video)
    step_ms = int(1000 / sample_fps)
    return list(range(0, int(dur_ms), step_ms))


def _corners_to_box(corners: list) -> tuple[int, int, int, int]:
    """EasyOCR returns 4 corner points; reduce to an axis-aligned x,y,w,h."""
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    x, y = int(min(xs)), int(min(ys))
    return x, y, int(max(xs) - x), int(max(ys) - y)


def _ocr_frame(frame_png: Path, min_conf: float) -> list[dict]:
    reader = _get_reader()
    dets = []
    for corners, text, conf in reader.readtext(str(frame_png)):
        text = " ".join(text.split()).strip()
        if conf < min_conf or len(text) < MIN_TEXT_LEN:
            continue
        x, y, w, h = _corners_to_box(corners)
        dets.append({"text": text, "box": [x, y, w, h], "conf": float(conf)})
    return dets


# --- event clustering (pure logic, unit-tested) ------------------------------

def _iou(a: list, b: list) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def cluster_events(frames: list[dict], frame_w: int, frame_h: int,
                   iou_match: float = IOU_MATCH, gap_ms: int = GAP_TOLERANCE_MS,
                   stable_drift_frac: float = STABLE_DRIFT_FRAC) -> list[dict]:
    """Group per-frame detections into text events spanning time.

    frames: [{"t_ms": int, "dets": [{"text","box","conf"}, ...]}, ...] in time order.
    Returns events: [{text, box, start_ms, end_ms, rep_ms, stable, drift_frac, conf}].

    Pure function -- no OCR, no ffmpeg -- so the clustering logic is testable
    without a video. An open event matches a new detection when the boxes
    overlap (IoU) and the normalized text agrees; unmatched detections open new
    events; open events unseen for longer than gap_ms are closed.
    """
    diag = (frame_w ** 2 + frame_h ** 2) ** 0.5
    open_events: list[dict] = []
    closed: list[dict] = []

    def close(ev):
        cx = [b[0] + b[2] / 2 for b in ev["_boxes"]]
        cy = [b[1] + b[3] / 2 for b in ev["_boxes"]]
        drift = ((max(cx) - min(cx)) ** 2 + (max(cy) - min(cy)) ** 2) ** 0.5
        ev["drift_frac"] = round(drift / diag, 4) if diag else 0.0
        ev["stable"] = ev["drift_frac"] <= stable_drift_frac
        # representative box/frame = the widest detection (most legible glyphs)
        best = max(range(len(ev["_boxes"])), key=lambda i: ev["_boxes"][i][2] * ev["_boxes"][i][3])
        ev["box"] = ev["_boxes"][best]
        ev["rep_ms"] = ev["_times"][best]
        ev["conf"] = round(max(ev["_confs"]), 3)
        del ev["_boxes"], ev["_times"], ev["_confs"], ev["_last_ms"]
        closed.append(ev)

    for fr in frames:
        t = fr["t_ms"]
        still_open = []
        for ev in open_events:
            if t - ev["_last_ms"] > gap_ms:
                close(ev)
            else:
                still_open.append(ev)
        open_events = still_open

        for det in fr["dets"]:
            match = None
            for ev in open_events:
                if _norm(det["text"]) == _norm(ev["text"]) and _iou(det["box"], ev["box"]) >= iou_match:
                    match = ev
                    break
            if match:
                match["end_ms"] = t
                match["_last_ms"] = t
                match["box"] = det["box"]
                match["_boxes"].append(det["box"])
                match["_times"].append(t)
                match["_confs"].append(det["conf"])
            else:
                open_events.append({
                    "text": det["text"], "box": det["box"],
                    "start_ms": t, "end_ms": t, "_last_ms": t,
                    "_boxes": [det["box"]], "_times": [t], "_confs": [det["conf"]],
                })

    for ev in open_events:
        close(ev)

    closed.sort(key=lambda e: (e["start_ms"], e["box"][1]))
    for i, ev in enumerate(closed, 1):
        ev["idx"] = i
    return closed


def filter_events(events: list[dict], min_event_ms: int = MIN_EVENT_MS) -> list[dict]:
    """Drop events too brief to be worth localizing (OCR flicker, one-frame hits)."""
    kept = [e for e in events if e["end_ms"] - e["start_ms"] >= min_event_ms]
    for i, ev in enumerate(kept, 1):
        ev["idx"] = i
    return kept


# --- detection driver --------------------------------------------------------

def detect_text_events(video: Path, scratch_dir: Path, sample_fps: float = SAMPLE_FPS,
                       min_conf: float = MIN_CONFIDENCE, min_event_ms: int = MIN_EVENT_MS) -> dict:
    """Sample the video, OCR each frame, cluster into events, and write
    screentext_events.json (+ a representative frame per event). Returns the
    events dict. Mechanical -- identical output for identical input."""
    cfg = get_config()
    st_dir = scratch_dir / "screentext"
    frames_dir = st_dir / "frames"
    reps_dir = st_dir / "reps"
    for d in (frames_dir, reps_dir):
        d.mkdir(parents=True, exist_ok=True)

    frame_w, frame_h = _probe_frame_size(cfg.ffmpeg_path, video)
    timestamps = _sample_timestamps(cfg.ffmpeg_path, video, sample_fps)

    frames = []
    for t in timestamps:
        fp = frames_dir / f"f_{t:08d}.jpg"
        _extract_frame(cfg.ffmpeg_path, video, t, fp)
        frames.append({"t_ms": t, "dets": _ocr_frame(fp, min_conf)})

    events = filter_events(
        cluster_events(frames, frame_w, frame_h), min_event_ms
    )

    # save one representative frame per event, for the clean step + human review
    for ev in events:
        rep = reps_dir / f"event_{ev['idx']:03d}.jpg"
        _extract_frame(cfg.ffmpeg_path, video, ev["rep_ms"], rep)
        ev["rep_frame"] = str(rep)

    result = {
        "frame_w": frame_w, "frame_h": frame_h,
        "sample_fps": sample_fps, "event_count": len(events),
        "unstable_count": sum(1 for e in events if not e["stable"]),
        "events": events,
    }
    (st_dir / "screentext_events.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def load_events(scratch_dir: Path) -> dict:
    return json.loads((scratch_dir / "screentext" / "screentext_events.json").read_text(encoding="utf-8"))


# --- clean + overlay render + composite --------------------------------------

def _upload_and_clean(crop_png: Path, remove_desc: str, scene_desc: str,
                      out_png: Path, scratch_dir: Path) -> Path:
    """Erase English text from a single cropped region and rebuild the
    background, via the same KIE nano-banana-edit model the thumbnail stage
    uses. Counts against a screentext-specific spend cap (many events per
    video, vs. the thumbnail's single clean)."""
    cfg = get_config()
    import base64
    b64 = base64.b64encode(crop_png.read_bytes()).decode()
    up = requests.post(
        KIE_UPLOAD_URL,
        headers={"Authorization": f"Bearer {cfg.kie_api_key}", "Content-Type": "application/json"},
        json={"base64Data": f"data:image/png;base64,{b64}", "uploadPath": "images/cn-dub-pipeline",
              "fileName": f"{crop_png.stem}.png"},
        timeout=90,
    )
    up.raise_for_status()
    url = up.json()["data"]["downloadUrl"]

    prompt = (
        f"Remove {remove_desc} and seamlessly rebuild the background behind it "
        f"so no text or text-shaped artifact remains. Do NOT change anything "
        f"else: {scene_desc}."
    )
    record_call(scratch_dir, "kie_screentext", cfg.max_screentext_clean_calls_per_run)
    task = requests.post(
        KIE_CREATE_TASK_URL,
        headers={"Authorization": f"Bearer {cfg.kie_api_key}", "Content-Type": "application/json"},
        json={"model": "google/nano-banana-edit",
              "input": {"prompt": prompt, "image_urls": [url], "output_format": "png"}},
        timeout=90,
    )
    task.raise_for_status()
    task_id = task.json()["data"]["taskId"]
    img_url = _poll_kie_task(cfg.kie_api_key, task_id)
    img = requests.get(img_url, timeout=60)
    img.raise_for_status()
    out_png.write_bytes(img.content)
    return out_png


def _padded_box(box: list, frame_w: int, frame_h: int, pad_frac: float = PATCH_PAD_FRAC) -> tuple:
    x, y, w, h = box
    px, py = int(w * pad_frac) + 4, int(h * pad_frac) + 4
    x0, y0 = max(0, x - px), max(0, y - py)
    x1, y1 = min(frame_w, x + w + px), min(frame_h, y + h + py)
    return x0, y0, x1 - x0, y1 - y0


def _render_patch(cleaned_patch: Path, zh_text: str, out_png: Path,
                  font_path: str = DEFAULT_FONT, font_index: int = 2) -> Path:
    """Draw the Chinese translation centered on the cleaned (text-free) patch,
    auto-sizing the font down until it fits the patch width. Deterministic --
    no model draws the CJK glyphs (they're unreliable at it), same as the
    thumbnail stage."""
    img = Image.open(cleaned_patch).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    size = max(12, int(H * 0.7))
    while size > 8:
        font = ImageFont.truetype(font_path, size, index=font_index)
        bbox = draw.textbbox((0, 0), zh_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W * 0.94 and th <= H * 0.92:
            break
        size -= 2
    x = (W - tw) // 2 - bbox[0]
    y = (H - th) // 2 - bbox[1]
    stroke = max(2, size // 18)
    draw.text((x, y), zh_text, font=font, fill="#FFFFFF",
              stroke_width=stroke, stroke_fill="#0A142D")
    img.save(out_png)
    return out_png


def build_localized_master(video: Path, events: list[dict], zh_texts: list[str],
                           frame_w: int, frame_h: int, scratch_dir: Path,
                           out_path: Path, scene_desc: str = "") -> dict:
    """For every STABLE event, clean its region and composite a Chinese patch
    over the video for the event's time span; unstable (moving) events are
    skipped and reported. Produces {id}_master_localized.mp4, which the render
    stage consumes in place of the raw master when it exists.

    Returns a log dict, incl. the skipped-unstable list a human must resolve."""
    cfg = get_config()
    st_dir = scratch_dir / "screentext"
    patch_dir = st_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)

    inputs, filters, skipped = [], [], []
    stage = "0:v"
    ov_idx = 0
    for ev, zh in zip(events, zh_texts):
        if not ev["stable"]:
            skipped.append({"idx": ev["idx"], "text": ev["text"], "zh": zh,
                            "drift_frac": ev["drift_frac"], "start_ms": ev["start_ms"]})
            continue

        rep = Path(ev["rep_frame"])
        px, py, pw, ph = _padded_box(ev["box"], frame_w, frame_h)
        crop = patch_dir / f"crop_{ev['idx']:03d}.png"
        Image.open(rep).convert("RGB").crop((px, py, px + pw, py + ph)).save(crop)

        cleaned = patch_dir / f"clean_{ev['idx']:03d}.png"
        _upload_and_clean(
            crop, remove_desc=f"the text reading '{ev['text']}'",
            scene_desc=scene_desc or "the surrounding image, all colors and textures",
            out_png=cleaned, scratch_dir=scratch_dir,
        )

        patch = patch_dir / f"patch_{ev['idx']:03d}.png"
        _render_patch(cleaned, zh, patch)

        inputs += ["-i", str(patch)]
        ov_idx += 1
        s, e = ev["start_ms"] / 1000, ev["end_ms"] / 1000
        out_label = f"v{ov_idx}"
        filters.append(
            f"[{stage}][{ov_idx}:v]overlay={px}:{py}:enable='between(t,{s:.3f},{e:.3f})'[{out_label}]"
        )
        stage = out_label

    if ov_idx == 0:
        # nothing stable to localize -- don't produce a redundant re-encode
        return {"localized": False, "composited": 0, "skipped_unstable": skipped,
                "reason": "no stable text events"}

    cmd = [cfg.ffmpeg_path, "-y", "-i", str(video)] + inputs + [
        "-filter_complex", ";".join(filters),
        "-map", f"[{stage}]", "-map", "0:a?",
        "-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "copy",
        str(out_path),
    ]
    log_path = st_dir / "localize_render.log"
    with open(log_path, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"localized-master render failed (see {log_path})")

    return {"localized": True, "composited": ov_idx, "skipped_unstable": skipped,
            "out": str(out_path)}
