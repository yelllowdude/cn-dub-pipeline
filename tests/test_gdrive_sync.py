"""
pull/push round-trip against an in-memory fake Drive: master selection on
pull, scratch restore/sync mapping, md5 skip on the second pass, claim
handling, and push never re-uploading masters. Pure -- the fake client
implements the same surface as gdrive.DriveClient, no network.
"""

import _bootstrap
_bootstrap.install()

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cn_pipeline import gdrive

FOLDER = gdrive.FOLDER_MIME


class FakeDrive:
    """In-memory Drive: nodes keyed by id, folders hold child ids."""

    def __init__(self):
        self.nodes = {"root": {"name": "root", "mime": FOLDER, "children": []}}
        self.next_id = 0
        self.uploads = []      # (name, parent_id, replaced_existing)
        self.downloads = []    # file names served

    def add(self, parent_id, name, content=None, mime=None):
        self.next_id += 1
        fid = f"f{self.next_id}"
        node = {"name": name, "mime": mime or (FOLDER if content is None else "application/octet-stream"),
                "children": [] if content is None and mime is None else None,
                "content": content, "modified": f"2026-01-{self.next_id:02d}T00:00:00Z"}
        self.nodes[fid] = node
        self.nodes[parent_id]["children"].append(fid)
        return fid

    # --- DriveClient surface ---
    def drive_id(self):
        return "root"

    def list_children(self, folder_id):
        out = []
        for cid in self.nodes[folder_id]["children"]:
            n = self.nodes[cid]
            entry = {"id": cid, "name": n["name"], "mimeType": n["mime"],
                     "modifiedTime": n["modified"]}
            if n["content"] is not None:
                entry["md5Checksum"] = hashlib.md5(n["content"]).hexdigest()
                entry["size"] = str(len(n["content"]))
            out.append(entry)
        return out

    def child_folder(self, parent_id, name):
        return next((e for e in self.list_children(parent_id)
                     if e["name"] == name and e["mimeType"] == FOLDER), None)

    def resolve_project_folder(self, project_id):
        parent = "root"
        for part in gdrive.YOUTUBE_LONGFORM.split("/"):
            parent = self.child_folder(parent, part)["id"]
        for e in self.list_children(parent):
            if e["mimeType"] == FOLDER and e["name"].startswith(project_id):
                return e
        raise gdrive.ProjectNotFoundError(project_id)

    def ensure_folder(self, parent_id, name):
        existing = self.child_folder(parent_id, name)
        return existing["id"] if existing else self.add(parent_id, name)

    def download(self, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.nodes[file_id]["content"])
        self.downloads.append(self.nodes[file_id]["name"])

    def upload(self, local, parent_id, existing_file_id=None):
        content = local.read_bytes()
        if existing_file_id:
            self.nodes[existing_file_id]["content"] = content
            self.uploads.append((local.name, parent_id, True))
            return existing_file_id
        fid = self.add(parent_id, local.name, content)
        self.uploads.append((local.name, parent_id, False))
        return fid

    def upload_small(self, name, data, parent_id, existing_file_id=None):
        if existing_file_id:
            self.nodes[existing_file_id]["content"] = data
            return existing_file_id
        return self.add(parent_id, name, data)

    def read_json(self, file_id):
        return json.loads(self.nodes[file_id]["content"])


def _setup():
    tmp = Path(tempfile.mkdtemp(prefix="gdrive_sync_test_"))
    cfg = SimpleNamespace(drive_root=tmp / "mirror", operator="alice")
    scratch = tmp / "runs" / "proj-a_2026-01-01"
    scratch.mkdir(parents=True)
    fake = FakeDrive()
    yl = fake.add("root", "_videos")
    yl = fake.add(yl, "youtube-longform")
    proj = fake.add(yl, "proj-a_2026-01-01")
    fake.add(proj, "proj-a_2026-01-01-video.mp4", b"OLD MASTER")
    fake.add(proj, "proj-a_2026-01-01-video_2.mp4", b"NEW MASTER")
    fake.add(proj, "proj-a_2026-01-01_me.wav", b"ME BED")
    cn = fake.add(proj, "CN")
    fake.add(cn, "proj-a_2026-01-01_zh.srt", b"1\nsub")
    pipe = fake.add(cn, "_pipeline")
    scr = fake.add(pipe, "scratch")
    fake.add(scr, "zh.json", b'["line"]')
    chunks = fake.add(scr, "chunks")
    fake.add(chunks, "chunk_01.mp3", b"PAID TTS")
    return tmp, cfg, scratch, fake


def test_pull_downloads_newest_master_state_and_claims():
    tmp, cfg, scratch, fake = _setup()
    try:
        result = gdrive.pull("proj-a", scratch, cfg=cfg, client=fake)
        assert result["claim"] == "fresh"
        assert result["master"] == "proj-a_2026-01-01-video_2.mp4"
        assert "proj-a_2026-01-01-video.mp4" not in fake.downloads, "old master must not download"
        pd = Path(result["project_dir"])
        assert (pd / "proj-a_2026-01-01-video_2.mp4").read_bytes() == b"NEW MASTER"
        assert (pd / "proj-a_2026-01-01_me.wav").exists()
        assert (pd / "CN" / "proj-a_2026-01-01_zh.srt").exists()
        # shared scratch restored to runs/{id}, not mirrored under CN/_pipeline
        assert (scratch / "zh.json").read_text() == '["line"]'
        assert (scratch / "chunks" / "chunk_01.mp3").read_bytes() == b"PAID TTS"
        assert not (pd / "CN" / "_pipeline").exists()

        # second pull: md5 skip -- nothing re-downloads
        before = len(fake.downloads)
        r2 = gdrive.pull("proj-a", scratch, cfg=cfg, client=fake)
        assert r2["downloaded"] == [] and len(fake.downloads) == before
        assert r2["claim"] == "mine"

        # a second operator is refused, --steal overrides
        cfg2 = SimpleNamespace(drive_root=tmp / "mirror2", operator="bob")
        try:
            gdrive.pull("proj-a", scratch, cfg=cfg2, client=fake)
            assert False, "expected ClaimError"
        except gdrive.ClaimError:
            pass
        r3 = gdrive.pull("proj-a", scratch, cfg=cfg2, client=fake, steal=True)
        assert r3["claim"] == "stolen"
    finally:
        shutil.rmtree(tmp)


def test_push_uploads_deliverables_and_scratch_never_masters():
    tmp, cfg, scratch, fake = _setup()
    try:
        result = gdrive.pull("proj-a", scratch, cfg=cfg, client=fake)
        pd = Path(result["project_dir"])
        # a render lands in CN/, a new paid chunk + big regenerable in scratch
        (pd / "CN" / "proj-a_2026-01-01_cndub.mp4").write_bytes(b"RENDERED")
        (scratch / "chunks" / "chunk_02.mp3").write_bytes(b"PAID 2")
        (scratch / "dub_master_final.wav").write_bytes(b"HUGE")
        (scratch / "frameio_review.json").write_text('{"stack_id": "s1"}')

        r = gdrive.push("proj-a", scratch, cfg=cfg, client=fake, release=True)
        names = [u[0] for u in fake.uploads]
        assert "proj-a_2026-01-01_cndub.mp4" in names
        assert "chunk_02.mp3" in names and "frameio_review.json" in names
        assert "dub_master_final.wav" not in names, "regenerable intermediates must not sync"
        assert "proj-a_2026-01-01-video_2.mp4" not in names, "masters are pull-only"
        assert r["released"]

        # released claim -> a fresh pull by bob succeeds
        cfg2 = SimpleNamespace(drive_root=tmp / "mirror2", operator="bob")
        assert gdrive.pull("proj-a", scratch, cfg=cfg2, client=fake)["claim"] == "fresh"

        # idempotent push: nothing changed, nothing uploads
        before = len(fake.uploads)
        r2 = gdrive.push("proj-a", scratch, cfg=cfg, client=fake)
        assert r2["uploaded"] == [] and len(fake.uploads) == before
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    _bootstrap.run_module(globals())
