# Frame.io V4 integration — verified API reference

Everything here was **probed against the live Gravgear account (2026-07-20)**,
because Adobe's V4 docs omit or 404 several of these shapes. If a call starts
failing, re-verify against this list before rewriting code — most of it is not
documented anywhere upstream. Implementation: `cn_pipeline/frameio.py`.

Base URL: `https://api.frame.io/v4` · errors come as `{"errors":[{title,detail}]}`.

## Auth (Adobe IMS)

| Fact | Detail |
|---|---|
| Token type | Adobe IMS OAuth. Legacy `fio-u-…` V2 tokens are **rejected** by V4 (work on `/v2` only). |
| S2S (client_credentials) | Ideal but **license-gated** — greyed out ("License required") without an Adobe Admin Console org. We don't use it. |
| What we use | **User Authentication (Web App credential)**: `FRAMEIO_CLIENT_ID/SECRET` + one-time `cn-pipeline review auth` browser sign-in → refresh token in `.env`; access tokens mint/refresh unattended (cached to ~60s before expiry). |
| Authorize URL | `https://ims-na1.adobelogin.com/ims/authorize/v2` — redirect URI must be **HTTPS** even for localhost (`https://localhost/redirect/`, registered on the credential with pattern `https://localhost/redirect/.*`). |
| Token URL | `https://ims-na1.adobelogin.com/ims/token/v3` |
| Scopes | `openid,AdobeID,email,profile,offline_access,additional_info.roles` — **`offline_access` is required** or no refresh_token is returned. |
| **The 401 trap** | A perfectly valid IMS token still gets `401 "Your Frame user is not linked to an Adobe ID"` until the Frame.io account connects Adobe auth: Frame.io → Account Settings → Profile → Authentication → Connect. Emails must match exactly; SSO/Google sign-in must be off during linking. |

## Verified endpoints & request bodies

| Operation | Call | Notes (all verified live) |
|---|---|---|
| List accounts | `GET /accounts` | responses wrap payloads in `data` |
| Workspaces | `GET /accounts/{a}/workspaces` | "teams" in V2 |
| Projects | `GET /accounts/{a}/workspaces/{w}/projects` | |
| Create project | `POST …/workspaces/{w}/projects` `{"data":{"name":…}}` | response carries `root_folder_id`, `view_url` |
| **Create file (upload)** | `POST /accounts/{a}/folders/{f}/files/local_upload` `{"data":{"name":…,"file_size":N}}` | **`media_type` is rejected (422)** — read it from the response instead. Response: `id`, `view_url`, `upload_urls: [{size, url}]` |
| Upload parts | `PUT` each presigned S3 url | Must send **exactly the signed headers**: check `X-Amz-SignedHeaders` in the url — in practice `content-type;host;x-amz-acl` → send `Content-Type: video/mp4` + `x-amz-acl: private`. Wrong header set = S3 403. |
| Wait/transcode | `GET /accounts/{a}/files/{id}` | `status` reaches `transcoded`. **No fps field anywhere** on the file object. |
| **Create share** | `POST /accounts/{a}/projects/{p}/shares` `{"data":{"name":…,"type":"asset","access":"public"[,"passphrase":…]}}` | The discriminator is **`type:"asset"`** (undocumented; "review"/"presentation"/etc. all 422). `access` only accepts `"public"`. `passphrase` works at create. Response: `short_url` (the stable f.io link), `collection_id`. |
| Attach file to share | `POST /accounts/{a}/shares/{s}/assets` `{"data":{"asset_id":…}}` | |
| Delete share / file | `DELETE /accounts/{a}/shares/{s}` · `DELETE /accounts/{a}/files/{id}` | 204 |
| **Version stack** | `POST /accounts/{a}/folders/{f}/version_stacks` `{"data":{"file_ids":[old,…,new]}}` | 2–10 ids, **oldest→newest; last id = current version**. Response id is the stack. Children: `GET /accounts/{a}/version_stacks/{s}/children`. |
| Append to stack (v3+) | `PATCH /accounts/{a}/files/{id}/move` `{"data":{"parent_id":stack_id}}` | |
| Comments | `GET /accounts/{a}/files/{id}/comments` | paginate via `links.next` |

## Comment timestamps (the subtle one)

V4 comment `timestamp` is a **framestamp, 1-based** — not seconds. The file
object exposes no fps, so `review fetch` probes fps from the **local cndub**
(`render.probe_fps`, `r_frame_rate` e.g. `30000/1001`) and converts:
`ms = (framestamp − 1) / fps × 1000`. Offline comment exports carry seconds or
explicit `timestamp_ms` and take a different branch — `_normalize_comment`
only treats `timestamp` as a framestamp **when fps is passed**. Verified by
resolving six real reviewer comments to their exact cues.

## Review-loop design (why it's shaped this way)

- Each re-cut is stacked as a **new version of the same asset**, so reviewers
  flip v1↔v2 (Compare view) and check off old comments; the share `short_url`
  never changes across versions → the Notion `Frame.io link` stays valid.
- The share is public + passphrase (`FRAMEIO_SHARE_PASSPHRASE`) because the
  native-speaker reviewer is external to the workspace.
- Per-project state: `runs/{id}/frameio_review.json`
  (`{versions: {label: asset_id}, stack_id, share_id, review_link}`). Losing it
  means the next submit opens a new stack/share instead of appending — restore
  it from this file's schema if that ever happens.
- Comment classification (`classify_comment`): only an explicit old→new pair
  auto-applies, and only within its resolved cue; everything else routes to a
  human queue. Don't loosen this — "auto-fix" on vague feedback edits blind.
