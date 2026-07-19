"""
Thumbnail: clean baked-in English text off the winning source thumbnail via
KIE.ai's nano-banana-edit model, then render Chinese headline text on top.

The headline TEXT is a live creative call each run (see docs/cn_workflow.html
Stage 2 and the Skill) -- this module only does the mechanical parts: the
clean-up API call, and placing given text at a given style/position. It does
not invent copy.

thumb_config.json schema (one per project, lives in the run scratch dir):
{
  "source_image": "path/to/raw/thumbnail.png",
  "clean_first": true,
  "font_path": "/System/Library/Fonts/Hiragino Sans GB.ttc",
  "headlines": [
    {"text": "...", "font_size": 118, "color": "#FFFFFF", "stroke_color": "#0A142D",
     "stroke_width": 6, "anchor": "top-center", "position": {"x": null, "y": 20}}
  ]
}
`anchor` is one of: top-center, top-left, top-right, bottom-center, bottom-left,
bottom-right, center. `position` gives pixel offsets from that anchor (nulls
mean "auto-center on that axis").
"""

import base64
import json
import subprocess
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from cn_pipeline.config import get_config
from cn_pipeline.spend import record_call

KIE_UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"
KIE_CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
KIE_RECORD_INFO_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

DEFAULT_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def clean_source_thumbnail(
    source_image: Path,
    remove_text_description: str,
    scene_description: str,
    out_path: Path,
    poll_timeout_s: int = 180,
) -> Path:
    """Remove baked-in English text from `source_image` via KIE's
    nano-banana-edit model and rebuild the background behind it.

    remove_text_description: what the English text says / looks like, e.g.
        "the bold white headline text 'DEATH BY SQUATS' at the top"
    scene_description: everything else that must stay pixel-identical, e.g.
        "the glowing yellow squatting figure, the two red muscle characters,
         the cracked rock platform, all colors"
    """
    cfg = get_config()
    small = out_path.parent / f"{out_path.stem}_upload_src.jpg"
    subprocess.run(
        [cfg.ffmpeg_path, "-y", "-i", str(source_image), "-vf", "scale=1920:1080", str(small)],
        capture_output=True, check=True,
    )

    b64 = base64.b64encode(small.read_bytes()).decode()
    up = requests.post(
        KIE_UPLOAD_URL,
        headers={"Authorization": f"Bearer {cfg.kie_api_key}", "Content-Type": "application/json"},
        json={"base64Data": f"data:image/jpeg;base64,{b64}", "uploadPath": "images/cn-dub-pipeline",
              "fileName": f"{out_path.stem}_source.jpg"},
        timeout=90,
    )
    up.raise_for_status()
    url = up.json()["data"]["downloadUrl"]

    prompt = (
        f"Remove {remove_text_description} and seamlessly rebuild the background "
        f"behind it so no text or text-shaped artifact remains. Do NOT change "
        f"anything else: {scene_description}. Keep the 16:9 composition."
    )
    # out_path lives in the run scratch dir; the createTask call is the paid part
    record_call(out_path.parent, "kie", cfg.max_kie_calls_per_run)
    task = requests.post(
        KIE_CREATE_TASK_URL,
        headers={"Authorization": f"Bearer {cfg.kie_api_key}", "Content-Type": "application/json"},
        json={"model": "google/nano-banana-edit",
              "input": {"prompt": prompt, "image_urls": [url], "output_format": "png", "aspect_ratio": "16:9"}},
        timeout=90,
    )
    task.raise_for_status()
    task_id = task.json()["data"]["taskId"]

    img_url = _poll_kie_task(cfg.kie_api_key, task_id, poll_timeout_s)
    img = requests.get(img_url, timeout=60)
    img.raise_for_status()
    out_path.write_bytes(img.content)
    return out_path


def _poll_kie_task(kie_api_key: str, task_id: str, poll_timeout_s: int = 180) -> str:
    """Poll a KIE createTask job to completion and return its first result URL.
    Shared by the thumbnail clean and the in-screen-text clean (screentext.py)
    so the success/fail/timeout handling lives in exactly one place."""
    deadline = time.time() + poll_timeout_s
    while time.time() < deadline:
        time.sleep(5)
        r = requests.get(KIE_RECORD_INFO_URL, headers={"Authorization": f"Bearer {kie_api_key}"},
                         params={"taskId": task_id}, timeout=30)
        r.raise_for_status()
        d = r.json()["data"]
        state = d.get("state")
        if state in ("success", "completed", "SUCCESS"):
            result_json = d.get("resultJson")
            result = json.loads(result_json) if isinstance(result_json, str) else d.get("result")
            urls = (result or {}).get("resultUrls") or (result or {}).get("output") or []
            if not urls:
                raise RuntimeError(f"KIE task succeeded but returned no result URL: {d}")
            return urls[0]
        if state in ("fail", "failed", "FAIL"):
            raise RuntimeError(f"KIE nano-banana-edit task failed: {d}")

    raise TimeoutError(f"KIE task {task_id} did not complete within {poll_timeout_s}s")


def _resolve_xy(anchor: str, position: dict, text_w: int, text_h: int, img_w: int, img_h: int) -> tuple:
    px, py = position.get("x"), position.get("y")
    horiz, _, vert = anchor.partition("-") if "-" in anchor else (anchor, "", anchor)

    if horiz in ("left",):
        x = px if px is not None else 20
    elif horiz in ("right",):
        x = img_w - text_w - (px if px is not None else 20)
    else:  # center / top-center / bottom-center / center-center
        x = (img_w - text_w) // 2 if px is None else px

    if vert in ("top",) or anchor == "top-center":
        y = py if py is not None else 20
    elif vert in ("bottom",) or anchor == "bottom-center":
        y = img_h - text_h - (py if py is not None else 20)
    else:  # plain "center"
        y = (img_h - text_h) // 2 if py is None else py

    return x, y


def render(base_image: Path, thumb_config: dict, out_path: Path) -> Path:
    """Render the configured headline text onto `base_image` (the cleaned
    thumbnail) and save to out_path."""
    img = Image.open(base_image).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img)
    font_path = thumb_config.get("font_path", DEFAULT_FONT)

    for h in thumb_config["headlines"]:
        font = ImageFont.truetype(font_path, h["font_size"], index=h.get("font_index", 2))
        bbox = draw.textbbox((0, 0), h["text"], font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = _resolve_xy(h.get("anchor", "top-center"), h.get("position", {}), text_w, text_h, W, H)
        draw.text(
            (x, y), h["text"], font=font, fill=h.get("color", "#FFFFFF"),
            stroke_width=h.get("stroke_width", 0), stroke_fill=h.get("stroke_color", "#000000"),
        )

    img.save(out_path)
    return out_path


def load_thumb_config(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
