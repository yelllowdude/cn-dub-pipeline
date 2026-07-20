"""Publish stage: OAuth URL construction, redirect/code parsing, video metadata
body limits, and the shared .env persistence helper. Pure logic only -- no HTTP."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _bootstrap import install, run_module

install()

from cn_pipeline import publish


class _Cfg:
    youtube_client_id = "cid123"
    youtube_client_secret = "sec"
    youtube_refresh_token = ""


def test_authorize_url():
    url = publish.build_authorize_url(_Cfg())
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid123" in url and "response_type=code" in url
    # offline + consent are what force Google to return a refresh_token
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "youtube.upload" in url  # least-privilege scope


def test_code_from_redirect_forms():
    assert publish._code_from_redirect("http://localhost/?code=4/0AbcD&scope=x") == "4/0AbcD"
    assert publish._code_from_redirect("'http://localhost/?code=XYZ'") == "XYZ"
    assert publish._code_from_redirect("  BARECODE  ") == "BARECODE"


def test_video_body_limits_and_privacy():
    body = publish.build_video_body("t" * 200, "d" * 6000, [f"tag{i}" for i in range(50)])
    assert len(body["snippet"]["title"]) == 100          # YouTube title cap
    assert len(body["snippet"]["description"]) == 4900   # description cap w/ margin
    assert len(body["snippet"]["tags"]) == 30
    # private IS the draft; made-for-kids must be explicit
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    # no tags key at all when none given
    assert "tags" not in publish.build_video_body("t")["snippet"]


def test_save_env_var_roundtrip():
    from cn_pipeline import config
    old = config.ENV_PATH
    try:
        d = Path(tempfile.mkdtemp())
        config.ENV_PATH = d / ".env"
        config.ENV_PATH.write_text("A=1\nYOUTUBE_REFRESH_TOKEN=old\nB=2\n", encoding="utf-8")
        config.save_env_var("YOUTUBE_REFRESH_TOKEN", "new")
        text = config.ENV_PATH.read_text(encoding="utf-8")
        assert "YOUTUBE_REFRESH_TOKEN=new" in text and "old" not in text
        assert "A=1" in text and "B=2" in text          # neighbors untouched
        config.save_env_var("C", "3")                    # append when missing
        assert "C=3" in config.ENV_PATH.read_text(encoding="utf-8")
    finally:
        config.ENV_PATH = old


if __name__ == "__main__":
    run_module(dict(globals()))
