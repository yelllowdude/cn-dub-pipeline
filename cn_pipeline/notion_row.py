"""
The Chinese-DB row as a structured artifact instead of an improvised message.

The pipeline deliberately has no Notion client -- Notion is orchestration,
owned by the Claude-side skill (see publish.py). That split is right, but it
left the row itself unspecified: every run re-improvised which fields to fill
and how to lay the page out, so two operators produced visibly different rows
for identical work, and a field could be quietly forgotten with nothing to
catch it.

This module fixes the SHAPE without taking over the posting:

    runs/{id}/notion_row.json   the skill authors the creative fields
                                (title, description, tags, ad disclosure)
    `cn-pipeline notion row`    validates them, fills the mechanical fields
                                the pipeline already knows, and renders the
                                page body from one fixed template

So the words stay a judgment call and the structure stops being one. The skill
still does the posting, through the Notion MCP tools, from this output.

`contains_ads` has no default on purpose. It is a legal disclosure, it cannot
be derived from anything on disk, and "nobody set it" must never silently
render as "no ads" -- so validation fails until a human states it.
"""

import json
from pathlib import Path

# Fields the skill must author. Everything else is derived.
REQUIRED = ("title_zh", "description_zh", "contains_ads")
OPTIONAL = ("title_options_zh", "tags_zh", "ad_disclosure_zh",
            "thumbnail_headline_zh", "translation_notes")

ROW_FILENAME = "notion_row.json"

TEMPLATE_STUB = {
    "title_zh": "",
    "title_options_zh": ["", "", ""],
    "description_zh": "",
    "tags_zh": [],
    "contains_ads": None,
    "ad_disclosure_zh": "",
    "thumbnail_headline_zh": "",
    "translation_notes": "",
}


class RowError(ValueError):
    pass


def row_path(scratch_dir: Path) -> Path:
    return Path(scratch_dir) / ROW_FILENAME


def write_stub(scratch_dir: Path) -> Path:
    """Create the file for the skill to fill in. Never clobbers existing work."""
    p = row_path(scratch_dir)
    if p.exists():
        raise RowError(f"{p} already exists -- edit it, or delete it first")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(TEMPLATE_STUB, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def load(scratch_dir: Path) -> dict:
    p = row_path(scratch_dir)
    if not p.exists():
        raise RowError(
            f"no {ROW_FILENAME} at {p}. Create one with "
            "`cn-pipeline notion row --project-id <id> --init`, then fill in the "
            "title, description and ad disclosure before re-running.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RowError(f"{p} is not valid JSON: {e}") from e


def validate(row: dict) -> list[str]:
    """Every problem at once, so the author fixes them in one pass."""
    problems = []
    for key in REQUIRED:
        if key not in row or row[key] in ("", None):
            problems.append(f"{key} is required and empty")

    if row.get("contains_ads") not in (True, False, None):
        problems.append("contains_ads must be true or false")
    # A declared sponsor with no disclosure text is the failure that actually
    # matters -- it ships an undisclosed paid mention.
    if row.get("contains_ads") is True and not row.get("ad_disclosure_zh"):
        problems.append("contains_ads is true, so ad_disclosure_zh must say so in Chinese")
    if row.get("contains_ads") is False and row.get("ad_disclosure_zh"):
        problems.append("ad_disclosure_zh is set but contains_ads is false -- pick one")

    opts = row.get("title_options_zh") or []
    if opts and len([o for o in opts if o]) < 3:
        problems.append("title_options_zh should carry all 3 candidates, or be omitted")
    if opts and row.get("title_zh") and row["title_zh"] not in opts:
        problems.append("title_zh is not one of title_options_zh -- "
                        "record the option you actually picked")

    unknown = set(row) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        problems.append(f"unknown field(s): {', '.join(sorted(unknown))}")
    return problems


def mechanical_fields(project_id: str, project_dir: Path, scratch_dir: Path) -> dict:
    """What the pipeline already knows -- never re-typed by hand, so it can't
    disagree with what actually shipped."""
    from cn_pipeline import paths

    out = paths.deliverable_paths(project_dir)
    review_link = ""
    rec = Path(scratch_dir) / "frameio_review.json"
    if rec.exists():
        try:
            review_link = json.loads(rec.read_text(encoding="utf-8")).get("review_link") or ""
        except json.JSONDecodeError:
            pass

    dub_mode = "cue_locked"
    proj = Path(scratch_dir) / "project.json"
    if proj.exists():
        try:
            dub_mode = json.loads(proj.read_text(encoding="utf-8")).get("dub_mode", dub_mode)
        except json.JSONDecodeError:
            pass

    return {
        "project_id": project_id,
        "dub_mode": dub_mode,
        "review_link": review_link,
        "deliverables": {k: out[k].exists() for k in
                         ("cndub_mp4", "ensub_mp4", "zh_srt", "en_srt",
                          "bilingual_cndub_srt", "cover_jpg")},
    }


# The reminder block goes at the TOP of the page: it is the thing a human
# needs to see before they touch anything, and it gets deleted wholesale once
# Bilibili API access lands.
REMINDER_BLOCK = """> 💡 **Delete this reminder once Bilibili API access is live.**
> Link in a publish property = published.
> Publish statuses:
> - [ ] ENsub Bilibili
> - [ ] CNdub Bilibili
> - [ ] CNdub YouTube
"""


def render_page_body(row: dict, mech: dict) -> str:
    """The fixed page layout. Same sections, same order, every project --
    that's the whole point of rendering it rather than writing it by hand."""
    parts = [REMINDER_BLOCK, "", "## 标题", "", row["title_zh"], ""]

    opts = [o for o in (row.get("title_options_zh") or []) if o]
    if opts:
        parts += ["### 候选标题", ""]
        parts += [f"{i}. {o}" + ("  ← 已选" if o == row["title_zh"] else "")
                  for i, o in enumerate(opts, 1)]
        parts += [""]

    parts += ["## 简介", "", row["description_zh"], ""]

    if row.get("tags_zh"):
        parts += ["## 标签", "", ", ".join(row["tags_zh"]), ""]

    parts += ["## 广告声明", ""]
    parts += ([row["ad_disclosure_zh"]] if row.get("contains_ads")
              else ["本片无赞助内容。"])
    parts += [""]

    if row.get("thumbnail_headline_zh"):
        parts += ["## 封面文案", "", row["thumbnail_headline_zh"], ""]
    if row.get("translation_notes"):
        parts += ["## 翻译说明", "", row["translation_notes"], ""]

    missing = [k for k, present in mech["deliverables"].items() if not present]
    parts += ["## 交付状态", "",
              f"- dub mode: `{mech['dub_mode']}`",
              f"- Frame.io: {mech['review_link'] or '(not submitted)'}",
              "- deliverables: " + ("all present" if not missing
                                    else f"MISSING {', '.join(missing)}"),
              ""]
    return "\n".join(parts)


def build(project_id: str, project_dir: Path, scratch_dir: Path) -> dict:
    """Validated row + mechanical fields + the rendered page body."""
    row = load(scratch_dir)
    problems = validate(row)
    if problems:
        raise RowError("notion_row.json is not ready:\n  - " + "\n  - ".join(problems))
    mech = mechanical_fields(project_id, project_dir, scratch_dir)
    return {"properties": {"Name": row["title_zh"],
                           "Contains ads?": bool(row["contains_ads"]),
                           "Frame.io link": mech["review_link"]},
            "page_body": render_page_body(row, mech),
            "mechanical": mech}
