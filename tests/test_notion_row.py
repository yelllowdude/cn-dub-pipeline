"""
notion_row: the Chinese-DB row as a validated artifact rather than an
improvised message. Concentrated on the two things that actually go wrong --
an ad disclosure that silently defaults, and a page layout that drifts between
operators.
"""

import json
import tempfile
from pathlib import Path

import _bootstrap
_bootstrap.install()

from cn_pipeline import notion_row  # noqa: E402

GOOD = {
    "title_zh": "我跳绳一个月，结果出乎意料",
    "title_options_zh": ["我跳绳一个月，结果出乎意料", "跳绳一个月的真实变化", "别再乱跳绳了"],
    "description_zh": "这一个月我每天跳绳，记录了体能和体重的变化。",
    "tags_zh": ["跳绳", "健身"],
    "contains_ads": False,
}

MECH = {"dub_mode": "native", "review_link": "https://f.io/x",
        "deliverables": {"cndub_mp4": True, "ensub_mp4": True, "zh_srt": True,
                         "en_srt": True, "bilingual_cndub_srt": True, "cover_jpg": True}}


# --- validation ------------------------------------------------------------------

def test_a_complete_row_validates():
    assert notion_row.validate(dict(GOOD)) == []


def test_missing_required_fields_are_all_reported_at_once():
    problems = notion_row.validate({"title_zh": "x"})
    assert any("description_zh" in p for p in problems)
    assert any("contains_ads" in p for p in problems)


def test_unset_contains_ads_is_not_treated_as_no_ads():
    """The disclosure has no safe default: unset must fail, not render as
    '本片无赞助内容'."""
    row = dict(GOOD, contains_ads=None)
    assert any("contains_ads" in p for p in notion_row.validate(row))


def test_declared_sponsor_without_disclosure_text_is_rejected():
    """This is the failure that ships an undisclosed paid mention."""
    row = dict(GOOD, contains_ads=True)
    assert any("ad_disclosure_zh" in p for p in notion_row.validate(row))


def test_disclosure_text_without_a_declared_sponsor_is_rejected():
    row = dict(GOOD, ad_disclosure_zh="本视频包含赞助内容")
    assert any("pick one" in p for p in notion_row.validate(row))


def test_chosen_title_must_be_one_of_the_candidates():
    row = dict(GOOD, title_zh="一个完全不同的标题")
    assert any("title_options_zh" in p for p in notion_row.validate(row))


def test_typo_in_a_field_name_is_caught_not_silently_dropped():
    row = dict(GOOD, descripton_zh="oops")
    assert any("unknown field" in p for p in notion_row.validate(row))


# --- rendering -------------------------------------------------------------------

def test_reminder_block_is_first():
    body = notion_row.render_page_body(dict(GOOD), MECH)
    assert body.startswith("> 💡"), body[:80]


def test_no_sponsor_renders_an_explicit_statement():
    body = notion_row.render_page_body(dict(GOOD), MECH)
    assert "本片无赞助内容。" in body


def test_sponsor_disclosure_is_rendered_verbatim():
    row = dict(GOOD, contains_ads=True, ad_disclosure_zh="本视频包含付费推广。")
    assert "本视频包含付费推广。" in notion_row.render_page_body(row, MECH)


def test_chosen_candidate_is_marked():
    body = notion_row.render_page_body(dict(GOOD), MECH)
    assert "← 已选" in body
    assert body.count("← 已选") == 1


def test_missing_deliverables_are_surfaced_in_the_page():
    mech = {**MECH, "deliverables": {**MECH["deliverables"], "cover_jpg": False}}
    assert "MISSING cover_jpg" in notion_row.render_page_body(dict(GOOD), mech)


def test_layout_is_identical_for_identical_input():
    """The point of a fixed template: two operators, one layout."""
    assert (notion_row.render_page_body(dict(GOOD), MECH)
            == notion_row.render_page_body(dict(GOOD), MECH))


# --- file lifecycle ----------------------------------------------------------------

def test_stub_round_trips_and_refuses_to_clobber():
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        p = notion_row.write_stub(scratch)
        assert json.loads(p.read_text(encoding="utf-8"))["contains_ads"] is None
        try:
            notion_row.write_stub(scratch)
            raise AssertionError("expected RowError -- must not overwrite authored work")
        except notion_row.RowError:
            pass


def test_missing_file_points_at_the_fix():
    with tempfile.TemporaryDirectory() as td:
        try:
            notion_row.load(Path(td))
            raise AssertionError("expected RowError")
        except notion_row.RowError as e:
            assert "--init" in str(e)


def test_build_refuses_an_incomplete_row():
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        notion_row.row_path(scratch).write_text(
            json.dumps({"title_zh": "x"}), encoding="utf-8")
        try:
            notion_row.build("proj", Path(td), scratch)
            raise AssertionError("expected RowError")
        except notion_row.RowError as e:
            assert "contains_ads" in str(e)


if __name__ == "__main__":
    _bootstrap.run_module(dict(globals()))
