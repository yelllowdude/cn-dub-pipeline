"""
Google Drive API storage layer ("storage": "gdrive" in config.json).

Why this exists: the original setup required every operator to run Google
Drive for Desktop signed into an account with the Shared Drive synced, and
config.json carried a per-person mount path. This module replaces the mount
with the Drive REST API: the pipeline works against a LOCAL MIRROR that
reproduces the Drive layout exactly ({mirror}/_videos/youtube-longform/{id}/),
so paths.py and every stage gate keep their local-file semantics unchanged.
Only two commands move bytes between the mirror and Drive:

    cn-pipeline drive pull --project-id <id>   # Drive -> mirror (+ scratch), claims the project
    cn-pipeline drive push --project-id <id>   # mirror (+ scratch) -> Drive

What syncs where:
  - pull: the master video (same newest-candidate rule as find_master_video,
    so we never download every 5 GB export), {id}_me.wav, all of CN/, and
    CN/_pipeline/scratch/ -> runs/{id}/ (segments, translations, TTS chunk
    cache, frameio_review.json, api_spend.json -- the paid/irreplaceable
    state that used to be trapped on one operator's machine).
  - push: CN/** plus {id}_me.wav, and runs/{id}/ -> CN/_pipeline/scratch/
    filtered through SCRATCH_EXCLUDE (huge regenerable intermediates stay
    local; the paid TTS chunks go up).

Concurrency: CN/_pipeline/claim.json is an advisory lock. `drive pull` claims
the project (refusing if someone else holds it -- --steal overrides);
`drive release` (or `drive push --release`) hands it back. Release rewrites
the file with claimed=false instead of deleting it, so the flow never needs
Drive delete permission. Drive sync isn't transactional -- the claim turns a
silent two-operator race (double TTS spend, a forked Frame.io version stack)
into a loud error, which is the job.

Auth mirrors cn_pipeline.publish: a Desktop OAuth client + one browser
sign-in (`drive auth`, consented as an account that can edit the Shared
Drive), then a stored refresh token mints access tokens unattended. Plain
requests, no Google SDK -- same call as publish.py. GDRIVE_CLIENT_ID/SECRET
fall back to the YouTube client credentials (same Google Cloud project works
for both; only the consenting ACCOUNT differs).
"""

import getpass
import hashlib
import json
import socket
import time
from pathlib import Path

import requests

from cn_pipeline.config import ConfigError, get_config, save_env_var
from cn_pipeline.paths import YOUTUBE_LONGFORM, ProjectNotFoundError

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Full drive scope: the pipeline reads masters it didn't create and writes
# deliverables into folders it didn't create, so drive.file isn't enough.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
REDIRECT_URI = "http://localhost"
FOLDER_MIME = "application/vnd.google-apps.folder"

UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024

PIPELINE_DIRNAME = "_pipeline"   # CN/_pipeline/ -- shared cross-operator state
SCRATCH_DIRNAME = "scratch"      # CN/_pipeline/scratch/ mirrors runs/{id}/
CLAIM_FILENAME = "claim.json"

# runs/{id}/ entries that never sync: big and mechanically regenerable from
# what DOES sync (or pure local logs). Everything else -- segments/zh jsons,
# anchors, zh_script, project.json, frameio_review.json, api_spend.json and
# the PAID chunks/ TTS cache -- goes up, because losing any of those either
# re-spends money or forks review state.
SCRATCH_EXCLUDE_DIRS = {"align_chunks", "align_passages", "screentext", "__pycache__"}
SCRATCH_EXCLUDE_NAMES = {"audio_16k.wav", "dub_master_final.wav",
                         "dub_master_padded.wav", "dub_master_mixed.wav"}
SCRATCH_EXCLUDE_SUFFIXES = {".log"}

_token_cache = {"value": None, "expires_at": 0.0}


class ClaimError(RuntimeError):
    pass


# --- pure planning helpers (unit-tested, no network) ---------------------------

def pick_master(entries: list[dict], project_name: str) -> dict | None:
    """Same selection rule as paths.find_master_video, applied to a remote
    listing: *.mp4 named {project}*.mp4 directly in the project root, newest
    modifiedTime wins. Returns the entry to download, or None."""
    cands = [e for e in entries
             if e["name"].endswith(".mp4") and e["name"].startswith(project_name)
             and e.get("mimeType") != FOLDER_MIME]
    if not cands:
        return None
    cands.sort(key=lambda e: e.get("modifiedTime", ""))
    return cands[-1]


def scratch_syncable(rel_path: str) -> bool:
    """Whether a runs/{id}/-relative path is part of the shared scratch set."""
    parts = Path(rel_path).parts
    if any(p in SCRATCH_EXCLUDE_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in SCRATCH_EXCLUDE_NAMES:
        return False
    if Path(name).suffix in SCRATCH_EXCLUDE_SUFFIXES:
        return False
    return True


def claim_verdict(existing: dict | None, me: dict, steal: bool) -> str:
    """Decide whether `me` may claim. Returns 'fresh'|'mine'|'stolen'; raises
    ClaimError when someone else holds the claim and steal is False."""
    if not existing or not existing.get("claimed"):
        return "fresh"
    if existing.get("operator") == me["operator"] and existing.get("host") == me["host"]:
        return "mine"
    if steal:
        return "stolen"
    raise ClaimError(
        f"project is claimed by {existing.get('operator')}@{existing.get('host')} "
        f"since {existing.get('claimed_at')}. Coordinate with them (they release with "
        "`cn-pipeline drive release`), or take over deliberately with --steal."
    )


def make_claim(me: dict) -> dict:
    return {"claimed": True, "operator": me["operator"], "host": me["host"],
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def whoami(cfg=None) -> dict:
    operator = ""
    if cfg is not None:
        operator = getattr(cfg, "operator", "") or ""
    return {"operator": operator or getpass.getuser(), "host": socket.gethostname()}


def _file_md5(path: Path, cache: dict) -> str:
    """md5 of a local file, memoized against (size, mtime) in the mirror's
    meta cache so repeat pulls/pushes don't re-hash multi-GB videos."""
    st = path.stat()
    entry = cache.get(str(path))
    if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime:
        return entry["md5"]
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    cache[str(path)] = {"size": st.st_size, "mtime": st.st_mtime, "md5": h.hexdigest()}
    return cache[str(path)]["md5"]


# --- OAuth ----------------------------------------------------------------------

def build_authorize_url(cfg) -> str:
    from urllib.parse import urlencode
    if not (cfg.gdrive_client_id and cfg.gdrive_client_secret):
        raise ConfigError(
            "Set GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET in .env (a Desktop OAuth "
            "client with the Google Drive API enabled -- the YouTube client from the "
            "same Google Cloud project works, and is used automatically if the GDRIVE_ "
            "vars are blank but YOUTUBE_ ones are set)."
        )
    return f"{GOOGLE_AUTH_URL}?" + urlencode({
        "client_id": cfg.gdrive_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })


def exchange_code_for_tokens(cfg, redirect_or_code: str) -> dict:
    from cn_pipeline import publish
    tok = publish._token_post({
        "grant_type": "authorization_code",
        "client_id": cfg.gdrive_client_id,
        "client_secret": cfg.gdrive_client_secret,
        "code": publish._code_from_redirect(redirect_or_code),
        "redirect_uri": REDIRECT_URI,
    })
    if not tok.get("refresh_token"):
        raise ConfigError(
            "Google returned no refresh_token. Re-run `drive auth` and make sure the "
            "consent screen actually appears (prompt=consent should force it)."
        )
    return tok


def save_refresh_token(refresh_token: str) -> Path:
    return save_env_var("GDRIVE_REFRESH_TOKEN", refresh_token)


def _access_token(cfg) -> str:
    if not (cfg.gdrive_client_id and cfg.gdrive_client_secret and cfg.gdrive_refresh_token):
        raise ConfigError(
            "No Drive credentials. Run `cn-pipeline drive auth` once, signed in as an "
            "account that can edit the Shared Drive, to store a refresh token."
        )
    from cn_pipeline import publish
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    tok = publish._token_post({
        "grant_type": "refresh_token",
        "client_id": cfg.gdrive_client_id,
        "client_secret": cfg.gdrive_client_secret,
        "refresh_token": cfg.gdrive_refresh_token,
    })
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


# --- REST client ----------------------------------------------------------------

class DriveClient:
    """Thin Drive v3 REST wrapper. Every call passes supportsAllDrives -- the
    content lives on a Shared Drive, and without that flag the API pretends
    the files don't exist."""

    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._drive_id = None

    # -- plumbing --
    def _headers(self):
        return {"Authorization": f"Bearer {_access_token(self.cfg)}"}

    def _request(self, method: str, url: str, **kw):
        for attempt in range(4):
            resp = requests.request(method, url, headers={**self._headers(), **kw.pop("headers", {})},
                                    timeout=kw.pop("timeout", 120), **kw)
            if resp.status_code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Drive API {method} {url.split('?')[0]} "
                                   f"failed ({resp.status_code}): {resp.text[:300]}")
            return resp
        raise RuntimeError("unreachable")

    # -- lookups --
    def drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        if self.cfg.gdrive_drive_id:
            self._drive_id = self.cfg.gdrive_drive_id
            return self._drive_id
        drives, token = [], None
        while True:
            params = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/drives", params=params).json()
            drives += data.get("drives", [])
            token = data.get("nextPageToken")
            if not token:
                break
        matches = [d for d in drives if d["name"] == self.cfg.gdrive_drive_name]
        if not matches:
            raise ConfigError(
                f"No Shared Drive named '{self.cfg.gdrive_drive_name}' visible to this "
                f"account (it sees: {', '.join(d['name'] for d in drives) or 'none'}). "
                "Check gdrive_drive_name in config.json and that the consented account "
                "is a member of the drive."
            )
        self._drive_id = matches[0]["id"]
        return self._drive_id

    def list_children(self, folder_id: str) -> list[dict]:
        files, token = [], None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "corpora": "drive", "driveId": self.drive_id(),
                "includeItemsFromAllDrives": "true", "supportsAllDrives": "true",
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",
            }
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/files", params=params).json()
            files += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return files

    def child_folder(self, parent_id: str, name: str) -> dict | None:
        return next((f for f in self.list_children(parent_id)
                     if f["name"] == name and f["mimeType"] == FOLDER_MIME), None)

    def resolve_project_folder(self, project_id: str) -> dict:
        """Walk _videos/youtube-longform and locate the project folder with the
        same exact-then-prefix rule as paths.resolve_project_dir."""
        parent = self.drive_id()  # shared drive root doubles as a folder id
        for part in YOUTUBE_LONGFORM.split("/"):
            folder = self.child_folder(parent, part)
            if not folder:
                raise ProjectNotFoundError(
                    f"folder '{part}' not found under the Shared Drive -- expected the "
                    f"{YOUTUBE_LONGFORM} layout")
            parent = folder["id"]
        folders = [f for f in self.list_children(parent) if f["mimeType"] == FOLDER_MIME]
        exact = [f for f in folders if f["name"] == project_id]
        if exact:
            return exact[0]
        matches = sorted((f for f in folders if f["name"].startswith(project_id)),
                         key=lambda f: f["name"])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProjectNotFoundError(
                f"Ambiguous project id '{project_id}' -- multiple Drive folders matched: "
                + ", ".join(m["name"] for m in matches))
        raise ProjectNotFoundError(
            f"No Drive folder found for '{project_id}' under {YOUTUBE_LONGFORM}. "
            "Check the project ID matches the Notion page exactly.")

    def ensure_folder(self, parent_id: str, name: str) -> str:
        existing = self.child_folder(parent_id, name)
        if existing:
            return existing["id"]
        resp = self._request("POST", f"{API}/files", params={"supportsAllDrives": "true"},
                             json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]})
        return resp.json()["id"]

    # -- bytes --
    def download(self, file_id: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(f"{API}/files/{file_id}", headers=self._headers(),
                          params={"alt": "media", "supportsAllDrives": "true"},
                          stream=True, timeout=600) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"download of {dest.name} failed "
                                   f"({resp.status_code}): {resp.text[:300]}")
            with open(tmp, "wb") as fh:
                for block in resp.iter_content(DOWNLOAD_CHUNK_BYTES):
                    fh.write(block)
        tmp.replace(dest)

    def upload(self, local: Path, parent_id: str, existing_file_id: str | None = None) -> str:
        """Resumable upload; PATCH updates an existing file in place (same file
        id -> links keep working), POST creates. Returns the file id."""
        size = local.stat().st_size
        if existing_file_id:
            url = f"{UPLOAD_API}/files/{existing_file_id}?uploadType=resumable&supportsAllDrives=true"
            init = self._request("PATCH", url, json={},
                                 headers={"X-Upload-Content-Length": str(size)})
        else:
            url = f"{UPLOAD_API}/files?uploadType=resumable&supportsAllDrives=true"
            init = self._request("POST", url, json={"name": local.name, "parents": [parent_id]},
                                 headers={"X-Upload-Content-Length": str(size)})
        session_url = init.headers.get("Location")
        if not session_url:
            raise RuntimeError(f"resumable init for {local.name} returned no session URL")

        if size == 0:
            resp = requests.put(session_url, headers={"Content-Length": "0"}, timeout=60)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"empty-file upload of {local.name} failed ({resp.status_code})")
            return resp.json()["id"]

        sent = 0
        with open(local, "rb") as fh:
            while sent < size:
                chunk = fh.read(UPLOAD_CHUNK_BYTES)
                end = sent + len(chunk) - 1
                resp = requests.put(session_url, data=chunk, timeout=1800,
                                    headers={"Content-Length": str(len(chunk)),
                                             "Content-Range": f"bytes {sent}-{end}/{size}"})
                if resp.status_code in (200, 201):
                    return resp.json()["id"]
                if resp.status_code != 308:
                    raise RuntimeError(f"chunk PUT for {local.name} failed at byte {sent} "
                                       f"({resp.status_code}): {resp.text[:300]}")
                sent = end + 1
        raise RuntimeError(f"upload loop for {local.name} ended without completion")

    def read_json(self, file_id: str) -> dict:
        resp = self._request("GET", f"{API}/files/{file_id}",
                             params={"alt": "media", "supportsAllDrives": "true"})
        return json.loads(resp.text)

    def upload_small(self, name: str, data: bytes, parent_id: str,
                     existing_file_id: str | None = None) -> str:
        """Multipart upload for small control files (claim.json). Not for media."""
        boundary = "cn_pipeline_boundary"
        metadata = {"name": name} if existing_file_id else {"name": name, "parents": [parent_id]}
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\nContent-Type: application/json\r\n\r\n").encode() \
            + data + f"\r\n--{boundary}--".encode()
        if existing_file_id:
            url = f"{UPLOAD_API}/files/{existing_file_id}?uploadType=multipart&supportsAllDrives=true"
            resp = self._request("PATCH", url, data=body,
                                 headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        else:
            url = f"{UPLOAD_API}/files?uploadType=multipart&supportsAllDrives=true"
            resp = self._request("POST", url, data=body,
                                 headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return resp.json()["id"]


# --- mirror sync ------------------------------------------------------------------

def _meta_path(project_dir: Path) -> Path:
    return project_dir / ".gdrive_meta.json"


def _load_meta(project_dir: Path) -> dict:
    p = _meta_path(project_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"md5_cache": {}, "file_ids": {}}


def _save_meta(project_dir: Path, meta: dict) -> None:
    _meta_path(project_dir).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _walk_remote(client: DriveClient, folder_id: str, prefix: str = "") -> list[tuple[str, dict]]:
    """Recursive (relpath, entry) listing of a remote folder's files."""
    out = []
    for entry in client.list_children(folder_id):
        rel = f"{prefix}{entry['name']}"
        if entry["mimeType"] == FOLDER_MIME:
            out += _walk_remote(client, entry["id"], f"{rel}/")
        else:
            out.append((rel, entry))
    return out


def _local_project_dir(cfg, remote_name: str) -> Path:
    return cfg.drive_root / YOUTUBE_LONGFORM / remote_name


def _pull_one(client, entry, dest: Path, md5_cache: dict, force: bool) -> bool:
    if not force and dest.exists() and entry.get("md5Checksum"):
        if _file_md5(dest, md5_cache) == entry["md5Checksum"]:
            return False
    client.download(entry["id"], dest)
    md5_cache.pop(str(dest), None)  # mtime changed; re-hash lazily next time
    return True


def pull(project_id: str, scratch_dir: Path, steal: bool = False, force: bool = False,
         claim: bool = True, cfg=None, client=None) -> dict:
    """Drive -> mirror + scratch. Claims the project first (see module doc).
    cfg/client are injectable for the pure tests; real callers pass neither."""
    cfg = cfg or get_config()
    client = client or DriveClient(cfg)
    folder = client.resolve_project_folder(project_id)
    project_dir = _local_project_dir(cfg, folder["name"])
    project_dir.mkdir(parents=True, exist_ok=True)
    meta = _load_meta(project_dir)
    cache = meta["md5_cache"]

    root_entries = client.list_children(folder["id"])
    cn = next((e for e in root_entries
               if e["name"] == "CN" and e["mimeType"] == FOLDER_MIME), None)

    # claim before any expensive transfer
    claim_state = "skipped"
    if claim:
        cn_id = cn["id"] if cn else client.ensure_folder(folder["id"], "CN")
        claim_state = _claim(client, cn_id, whoami(cfg), steal)

    downloaded = []
    master = pick_master(root_entries, folder["name"])
    if master and _pull_one(client, master, project_dir / master["name"], cache, force):
        downloaded.append(master["name"])
    me_wav = next((e for e in root_entries if e["name"] == f"{folder['name']}_me.wav"), None)
    if me_wav and _pull_one(client, me_wav, project_dir / me_wav["name"], cache, force):
        downloaded.append(me_wav["name"])

    if cn:
        for rel, entry in _walk_remote(client, cn["id"], "CN/"):
            scratch_rel = f"CN/{PIPELINE_DIRNAME}/{SCRATCH_DIRNAME}/"
            if rel.startswith(scratch_rel):
                dest = scratch_dir / rel[len(scratch_rel):]
            elif rel == f"CN/{PIPELINE_DIRNAME}/{CLAIM_FILENAME}":
                continue  # claim already handled; don't mirror it
            else:
                dest = project_dir / rel
            meta["file_ids"][rel] = entry["id"]
            if _pull_one(client, entry, dest, cache, force):
                downloaded.append(rel)

    _save_meta(project_dir, meta)
    return {"project_dir": str(project_dir), "claim": claim_state,
            "downloaded": downloaded,
            "master": master["name"] if master else None}


def push(project_id: str, scratch_dir: Path, release: bool = False,
         cfg=None, client=None) -> dict:
    """mirror + scratch -> Drive. Uploads only what changed (md5 diff)."""
    cfg = cfg or get_config()
    client = client or DriveClient(cfg)
    folder = client.resolve_project_folder(project_id)
    project_dir = _local_project_dir(cfg, folder["name"])
    if not project_dir.is_dir():
        raise ProjectNotFoundError(
            f"no local mirror at {project_dir} -- run `drive pull` before `drive push`")
    meta = _load_meta(project_dir)
    cache = meta["md5_cache"]

    remote = {rel: e for rel, e in _walk_remote(client, folder["id"], "")}
    folder_ids = {"": folder["id"]}

    def ensure_remote_dir(rel_dir: str) -> str:
        if rel_dir in folder_ids:
            return folder_ids[rel_dir]
        parent = ensure_remote_dir(str(Path(rel_dir).parent) if "/" in rel_dir else "")
        folder_ids[rel_dir] = client.ensure_folder(parent, Path(rel_dir).name)
        return folder_ids[rel_dir]

    def push_file(local: Path, rel: str):
        entry = remote.get(rel)
        if entry and entry.get("md5Checksum") and _file_md5(local, cache) == entry["md5Checksum"]:
            return False
        parent_id = ensure_remote_dir(str(Path(rel).parent) if "/" in rel else "")
        fid = client.upload(local, parent_id, entry["id"] if entry else None)
        meta["file_ids"][rel] = fid
        return True

    uploaded = []
    # deliverables: everything under CN/ except the _pipeline subtree (scratch
    # is pushed from runs/{id} below; the claim file has its own lifecycle)
    cn_dir = project_dir / "CN"
    if cn_dir.is_dir():
        for f in sorted(cn_dir.rglob("*")):
            if not f.is_file() or f.name == ".gdrive_meta.json":
                continue
            rel = str(f.relative_to(project_dir))
            if rel.startswith(f"CN/{PIPELINE_DIRNAME}/"):
                continue
            if push_file(f, rel):
                uploaded.append(rel)
    me_wav = project_dir / f"{folder['name']}_me.wav"
    if me_wav.exists() and push_file(me_wav, me_wav.name):
        uploaded.append(me_wav.name)

    # scratch -> CN/_pipeline/scratch/
    if scratch_dir.is_dir():
        for f in sorted(scratch_dir.rglob("*")):
            if not f.is_file():
                continue
            rel_scratch = str(f.relative_to(scratch_dir))
            if not scratch_syncable(rel_scratch):
                continue
            rel = f"CN/{PIPELINE_DIRNAME}/{SCRATCH_DIRNAME}/{rel_scratch}"
            if push_file(f, rel):
                uploaded.append(rel)

    released = False
    if release:
        cn_id = ensure_remote_dir("CN")
        _release(client, cn_id, whoami(cfg))
        released = True

    _save_meta(project_dir, meta)
    return {"uploaded": uploaded, "released": released}


# --- claim -----------------------------------------------------------------------

def _pipeline_folder(client: DriveClient, cn_folder_id: str) -> str:
    return client.ensure_folder(cn_folder_id, PIPELINE_DIRNAME)


def _read_claim(client: DriveClient, pipeline_id: str) -> tuple[dict | None, str | None]:
    entry = next((e for e in client.list_children(pipeline_id)
                  if e["name"] == CLAIM_FILENAME), None)
    if not entry:
        return None, None
    try:
        return client.read_json(entry["id"]), entry["id"]
    except (json.JSONDecodeError, RuntimeError):
        return None, entry["id"]


def _write_claim(client: DriveClient, pipeline_id: str, data: dict, file_id: str | None) -> None:
    client.upload_small(CLAIM_FILENAME, json.dumps(data, indent=2).encode("utf-8"),
                        pipeline_id, file_id)


def _claim(client: DriveClient, cn_folder_id: str, me: dict, steal: bool) -> str:
    pipeline_id = _pipeline_folder(client, cn_folder_id)
    existing, file_id = _read_claim(client, pipeline_id)
    verdict = claim_verdict(existing, me, steal)
    if verdict != "mine":
        _write_claim(client, pipeline_id, make_claim(me), file_id)
    return verdict


def _release(client: DriveClient, cn_folder_id: str, me: dict) -> None:
    pipeline_id = _pipeline_folder(client, cn_folder_id)
    existing, file_id = _read_claim(client, pipeline_id)
    data = {"claimed": False, "operator": me["operator"], "host": me["host"],
            "released_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _write_claim(client, pipeline_id, data, file_id)


def claim_project(project_id: str, steal: bool = False) -> str:
    cfg = get_config()
    client = DriveClient(cfg)
    folder = client.resolve_project_folder(project_id)
    cn = client.child_folder(folder["id"], "CN") or {"id": client.ensure_folder(folder["id"], "CN")}
    return _claim(client, cn["id"], whoami(cfg), steal)


def release_project(project_id: str) -> None:
    cfg = get_config()
    client = DriveClient(cfg)
    folder = client.resolve_project_folder(project_id)
    cn = client.child_folder(folder["id"], "CN")
    if not cn:
        return
    _release(client, cn["id"], whoami(cfg))


def claim_status(project_id: str) -> dict | None:
    cfg = get_config()
    client = DriveClient(cfg)
    folder = client.resolve_project_folder(project_id)
    cn = client.child_folder(folder["id"], "CN")
    if not cn:
        return None
    pipeline = client.child_folder(cn["id"], PIPELINE_DIRNAME)
    if not pipeline:
        return None
    existing, _ = _read_claim(client, pipeline["id"])
    return existing
