"""
Pre-flight the whole environment before any expensive or paid work starts.

Why this exists: twice in one week a batch died *mid-run*, after paid work.
Frame.io auth failed on video 2 of 5 (the Adobe session behind the refresh
token had been invalidated), and ElevenLabs quota ran out on video 5 of 5 with
160 passages already bought. Both were visible in under a second beforehand --
nobody had asked.

Two design rules follow from that:

1. **Collect, never abort.** The whole point is diagnosing a broken setup, so a
   missing ffmpeg or an unparseable config.json must be reported as one FAIL
   among many, not raised. Nothing here calls get_config() at import time, and
   every check is individually wrapped.

2. **Estimate the paid work in the units the vendor bills.** "Is the key live?"
   is not the useful question -- "will this project finish on the credits I
   have?" is. `estimate_tts_characters` walks the same text-equality cache
   `dub._tts_cached` uses, so it counts only passages that would actually be
   re-bought, and compares that against ElevenLabs' real remaining balance.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

EL_SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
KIE_CREDIT_URL = "https://api.kie.ai/api/v1/chat/credit"


class Check:
    """One diagnostic result. `detail` is shown always; `hint` only on non-PASS,
    and must say what to actually DO -- an opaque status is what made the
    Frame.io outage expensive to diagnose."""

    def __init__(self, name: str, status: str, detail: str = "", hint: str = ""):
        self.name = name
        self.status = status
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict:
        d = {"check": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            d["hint"] = self.hint
        return d


def _c(name, status, detail="", hint=""):
    return Check(name, status, detail, hint)


# --- environment checks (deliberately independent of Config) -----------------

def check_env_file() -> list[Check]:
    """Read .env directly rather than through Config, so a missing key is one
    reportable failure instead of an exception that hides every later check."""
    from cn_pipeline.config import ENV_PATH
    if not Path(ENV_PATH).exists():
        return [_c(".env", FAIL, f"not found at {ENV_PATH}",
                   "run cn-pipeline-setup, or copy .env.example and fill it in")]
    vals = {}
    for line in Path(ENV_PATH).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()

    out = [_c(".env", PASS, str(ENV_PATH))]
    for key in ("ELEVENLABS_API_KEY", "KIE_API_KEY"):
        if vals.get(key):
            out.append(_c(key, PASS, f"set ({len(vals[key])} chars)"))
        else:
            out.append(_c(key, FAIL, "missing or empty",
                          "required for every run -- see README, 'Credentials'"))

    # Frame.io: any ONE of the three auth modes is enough. Report which.
    if vals.get("FRAMEIO_CLIENT_ID") and vals.get("FRAMEIO_CLIENT_SECRET"):
        mode = ("user-auth (refresh token)" if vals.get("FRAMEIO_REFRESH_TOKEN")
                else "server-to-server (client_credentials)")
        out.append(_c("frameio auth mode", PASS, mode))
    elif vals.get("FRAMEIO_TOKEN"):
        out.append(_c("frameio auth mode", WARN, "static FRAMEIO_TOKEN",
                      "static tokens expire ~24h; prefer `review auth`"))
    else:
        out.append(_c("frameio auth mode", WARN, "not configured",
                      "only the `review` stage needs it -- run `cn-pipeline review auth`"))
    return out


def check_config_file() -> list[Check]:
    from cn_pipeline.config import CONFIG_PATH
    if not Path(CONFIG_PATH).exists():
        return [_c("config.json", FAIL, f"not found at {CONFIG_PATH}",
                   "run cn-pipeline-setup, or copy config.example.json")]
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [_c("config.json", FAIL, f"invalid JSON: {e}", "fix the syntax")]

    out = [_c("config.json", PASS, str(CONFIG_PATH))]
    storage = raw.get("storage") or (
        "gdrive" if (raw.get("gdrive_drive_id") or raw.get("gdrive_drive_name")) else "mount")
    out.append(_c("storage mode", PASS, storage))

    if storage == "mount":
        root = raw.get("drive_root")
        if not root:
            out.append(_c("drive_root", FAIL, "missing", "set it, or switch to gdrive storage"))
        elif not Path(root).expanduser().is_dir():
            out.append(_c("drive_root", FAIL, f"unreachable: {root}",
                          "is Google Drive Desktop running and the Shared Drive synced?"))
        else:
            out.append(_c("drive_root", PASS, str(root)))

    # The claim lock labels itself with this. Without it the label is the unix
    # username, which works for the lock (re-entrancy keys on a machine id, not
    # this) but can leave a teammate reading a login name they don't recognise
    # and guessing who to go ask.
    if raw.get("operator"):
        out.append(_c("operator label", PASS, raw["operator"]))
    else:
        import getpass
        out.append(_c("operator label", WARN, f"unset -- claims will say '{getpass.getuser()}'",
                      'set "operator" in config.json to whatever label your teammates '
                      "will recognise (a handle or a role is fine) so claim messages "
                      "point at someone reachable"))
    return out


def check_output_settings() -> list[Check]:
    """Report output-affecting settings this machine overrides.

    These change the deliverable, so an override means the same input renders
    differently here than on a teammate's machine. Not a FAIL -- an override
    can be deliberate -- but it must be visible, because the failure mode is
    silent: nobody notices the M&E bed is 6 dB hotter until a reviewer does.
    """
    from cn_pipeline.config import CONFIG_PATH, output_setting_overrides
    if not Path(CONFIG_PATH).exists():
        return []
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    overrides = output_setting_overrides(raw)
    if not overrides:
        return [_c("output settings", PASS, "match pipeline_defaults.json")]
    return [_c("output settings", WARN,
               "; ".join(f"{k}: repo {dflt!r} -> local {loc!r}" for k, dflt, loc in overrides),
               "these change the rendered output -- teammates will produce different "
               "files from the same input. Remove from config.json to match the team, "
               "or change pipeline_defaults.json so everyone moves together")]


def check_ffmpeg() -> list[Check]:
    """ffmpeg must have libass (or subtitle burn-in silently no-ops). The
    encoder check is a WARN not a FAIL: h264_videotoolbox is macOS-only, so a
    Linux/Windows teammate legitimately lacks it and needs a fallback."""
    from cn_pipeline.config import CONFIG_PATH, Config, ConfigError
    raw = {}
    if Path(CONFIG_PATH).exists():
        try:
            raw = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
    # Resolve without constructing a Config: __init__ validates keys and Drive
    # too, and we want ffmpeg reported independently of those.
    try:
        path = Config._resolve_ffmpeg(Config.__new__(Config), raw.get("ffmpeg_path"))
    except ConfigError as e:
        return [_c("ffmpeg", FAIL, str(e).split(".")[0], "install an ffmpeg built with libass")]
    except Exception as e:  # pragma: no cover - defensive
        return [_c("ffmpeg", FAIL, f"{type(e).__name__}: {e}")]

    out = [_c("ffmpeg", PASS, f"{path} (libass ok)")]
    try:
        enc = subprocess.run([path, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return out + [_c("h264 encoder", WARN, f"could not list encoders: {e}")]

    if "h264_videotoolbox" in enc:
        out.append(_c("h264 encoder", PASS, "h264_videotoolbox (hardware)"))
    elif "libx264" in enc:
        out.append(_c("h264 encoder", WARN, "h264_videotoolbox missing; libx264 available",
                      "renders hardcode h264_videotoolbox (macOS-only) -- this machine "
                      "needs the software-encoder fallback before it can render"))
    else:
        out.append(_c("h264 encoder", FAIL, "neither h264_videotoolbox nor libx264",
                      "this ffmpeg cannot encode H.264"))
    return out


def check_python_deps() -> list[Check]:
    out = [_c("python", PASS, sys.version.split()[0])]
    for mod, why in (("whisper", "transcribe / align-dub"), ("torch", "whisper backend"),
                     ("pydub", "dub assembly"), ("PIL", "thumbnail render"),
                     ("requests", "all HTTP")):
        try:
            __import__(mod)
            out.append(_c(f"import {mod}", PASS, why))
        except ImportError:
            out.append(_c(f"import {mod}", FAIL, f"not installed (needed for {why})",
                          "re-run cn-pipeline-setup to rebuild the venv"))
    return out


# --- paid-API balance checks -------------------------------------------------

def elevenlabs_balance(api_key: str) -> dict:
    """Remaining ElevenLabs characters. ElevenLabs bills per character and the
    quota error reports 'credits', which are characters -- so this number is
    directly comparable to estimate_tts_characters()."""
    import requests
    r = requests.get(EL_SUBSCRIPTION_URL, headers={"xi-api-key": api_key}, timeout=20)
    r.raise_for_status()
    d = r.json()
    used, limit = int(d.get("character_count", 0)), int(d.get("character_limit", 0))
    return {"used": used, "limit": limit, "remaining": max(0, limit - used),
            "tier": d.get("tier", "")}


def check_elevenlabs(api_key: str) -> tuple[list[Check], int | None]:
    """Returns (checks, remaining_characters). The caller needs the number, not
    a re-parse of the display string, so it comes back alongside."""
    if not api_key:
        return [_c("elevenlabs", FAIL, "no API key")], None
    try:
        b = elevenlabs_balance(api_key)
    except Exception as e:
        return [_c("elevenlabs", FAIL, f"{type(e).__name__}: {str(e)[:160]}",
                   "check ELEVENLABS_API_KEY is valid and the key has quota")], None
    pct = (b["remaining"] / b["limit"] * 100) if b["limit"] else 0
    detail = f"{b['remaining']:,} of {b['limit']:,} characters left ({pct:.0f}%)"
    if b["remaining"] == 0:
        return [_c("elevenlabs", FAIL, detail, "quota exhausted -- top up before running")], 0
    if pct < 10:
        return [_c("elevenlabs", WARN, detail, "low; a long video may not finish")], b["remaining"]
    return [_c("elevenlabs", PASS, detail)], b["remaining"]


def check_kie(api_key: str) -> list[Check]:
    if not api_key:
        return [_c("kie", FAIL, "no API key")]
    try:
        import requests
        r = requests.get(KIE_CREDIT_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        r.raise_for_status()
        credit = r.json().get("data")
    except Exception as e:
        return [_c("kie", FAIL, f"{type(e).__name__}: {str(e)[:160]}",
                   "check KIE_API_KEY")]
    if credit is None:
        return [_c("kie", WARN, "balance endpoint returned no data")]
    if float(credit) <= 0:
        return [_c("kie", FAIL, f"{credit} credits", "top up -- thumbnail clean will fail")]
    return [_c("kie", PASS, f"{credit} credits")]


def check_frameio() -> list[Check]:
    """Actually mint a token. Anything less doesn't prove the credential works
    -- the outage that motivated this passed every static check and failed only
    at the IMS token exchange."""
    try:
        from cn_pipeline import frameio
        from cn_pipeline.config import get_config
        token = frameio._access_token(get_config())
        return [_c("frameio token", PASS, f"minted ok ({len(token)} chars)")]
    except Exception as e:
        msg = str(e)
        if "access_denied" in msg or "refresh" in msg.lower():
            # Frame.io is a SHARED account, so adopting a working token is the
            # normal fix. Pointing at `review auth` first sends a teammate to
            # mint a token against their own Adobe session -- which replaces the
            # team's working one and walks them into the single-use-code trap.
            hint = ("this is a SHARED account -- adopt a working token first: get a bundle "
                    "from an operator whose doctor shows `minted ok`, then `cn-pipeline team "
                    "import --file <bundle> --overwrite`. Only re-run `cn-pipeline review auth` "
                    "if nobody has one, and sign in as the shared Frame.io account")
        else:
            hint = "only the `review` stage needs Frame.io; everything else can run"
        return [_c("frameio token", FAIL, msg.splitlines()[0][:200], hint)]


def check_youtube() -> list[Check]:
    """Mint a YouTube token too. Publishing was the one paid/irreversible stage
    doctor couldn't see: a dead token stayed invisible until someone tried to
    upload an approved cut, which is the worst moment to discover it."""
    from cn_pipeline.config import get_config
    cfg = get_config()
    if not (cfg.youtube_client_id and cfg.youtube_client_secret):
        return [_c("youtube token", WARN, "no YOUTUBE_CLIENT_ID/SECRET",
                   "only the `publish` stage needs this; import a team bundle when you get there")]
    if not cfg.youtube_refresh_token:
        return [_c("youtube token", WARN, "no YOUTUBE_REFRESH_TOKEN",
                   "run `cn-pipeline publish auth` as the Chinese channel's Google account, "
                   "or adopt a working one with `cn-pipeline team import`")]
    try:
        from cn_pipeline import publish
        token = publish._access_token(cfg)
        return [_c("youtube token", PASS, f"minted ok ({len(token)} chars)")]
    except Exception as e:
        msg = str(e)
        # WARN, not FAIL: this blocks `publish` only, and publishing is a
        # separate later step a human runs. Failing every localization run over
        # a credential that run doesn't touch is how people learn to ignore
        # doctor -- and then miss the failures that do matter.
        if "invalid_grant" in msg:
            return [_c("youtube token", WARN,
                       "invalid_grant -- expired or revoked (blocks `publish`, not localization)",
                       "if the Google OAuth app is still in Testing mode this recurs every ~7 "
                       "days regardless of use, so re-authenticating only buys a week. Permanent "
                       "fix, one-time, needs a Cloud project owner: Google Cloud Console -> APIs "
                       "& Services -> OAuth consent screen -> PUBLISH APP. Stopgap: `cn-pipeline "
                       "publish auth` as the Chinese channel's Google account")]
        return [_c("youtube token", WARN, msg.splitlines()[0][:200],
                   "only the `publish` stage needs YouTube; localization can still run")]


# --- per-project checks ------------------------------------------------------

def estimate_tts_characters(project_id: str) -> dict:
    """Characters `dub generate` would actually buy for this project right now.

    Mirrors dub._tts_cached's cache rule (exact text equality against the
    sidecar), so an already-generated passage costs 0 and only genuinely new or
    edited ones count. That's what makes the comparison against remaining
    ElevenLabs credits trustworthy rather than a worst-case guess.
    """
    from cn_pipeline import paths
    scratch = paths.run_scratch_dir(project_id)
    script = scratch / "zh_script.json"
    if not script.exists():
        return {"known": False, "reason": "no zh_script.json yet (native mode not authored)"}

    try:
        passages = json.loads(script.read_text(encoding="utf-8")).get("passages") or []
    except json.JSONDecodeError as e:
        return {"known": False, "reason": f"zh_script.json invalid: {e}"}

    total = billable = 0
    pending = []
    for p in passages:
        aid = p.get("anchor_id") or ""
        beats = p.get("beats") or []
        text = "".join(b.get("text", "") for b in beats) or (p.get("text") or "")
        total += len(text)
        raw = scratch / "passages" / f"{aid}_raw.mp3"
        side = raw.with_suffix(".texts.json")
        cached = False
        if raw.exists() and side.exists():
            try:
                # dub_native sends the whole passage as one text
                cached = "".join(json.loads(side.read_text(encoding="utf-8"))) == text
            except json.JSONDecodeError:
                cached = False
        if not cached:
            billable += len(text)
            pending.append(aid)
    return {"known": True, "passages": len(passages), "total_chars": total,
            "billable_chars": billable, "pending": pending}


def check_claim(project_dir) -> list[Check]:
    """Who, if anyone, is working this project right now.

    Answers the question doctor exists for -- "is it safe to start?" -- before
    the operator spends an hour of Whisper and a TTS budget discovering that a
    colleague is mid-run on the same video."""
    from cn_pipeline import gdrive, shared_state
    try:
        claim = shared_state.status(project_dir)
    except OSError as e:
        return [_c("project claim", WARN, f"unreadable: {e}")]
    if not claim or not claim.get("claimed"):
        return [_c("project claim", PASS, "unclaimed")]

    me = gdrive.whoami()
    holder = gdrive.describe_holder(claim)
    if claim.get("operator") == me["operator"] and claim.get("host") == me["host"]:
        return [_c("project claim", PASS, f"held by you since {claim.get('claimed_at')}")]

    age = gdrive.claim_age_hours(claim)
    if age is not None and age >= gdrive.CLAIM_STALE_HOURS:
        return [_c("project claim", WARN,
                   f"{holder}, idle {age:.0f}h (stale)",
                   "stale claims are taken over automatically -- but check they really "
                   "stopped before you start")]
    return [_c("project claim", FAIL, f"held by {holder}",
               "they are working this project now. Coordinate before starting, or you "
               "will both pay for the same TTS and fork the Frame.io review stack.")]


def check_me_gain(project_id: str, me_wav) -> list[Check]:
    """Which M&E gain this project will mix at, and the bed's measured level.

    Reported, not judged. I tried to infer provenance from the level and the
    measurement refuted it: across real projects the Demucs-separated beds came
    in at -31.9/-32.6/-36.5 dBFS and the staff-prepped ones at
    -37.5/-35.4/-34.3/-32.6 -- fully overlapping, with the quietest bed of all
    being staff-prepped. Overall dBFS mostly reflects how much near-silence a
    track contains, not how loud its music is, so any threshold here would fire
    on legitimate files and train people to ignore doctor. The number is shown
    so a human can sanity-check it; the gain comes from the declared source.
    """
    from cn_pipeline import dub, paths
    from cn_pipeline.cli import _me_source
    from cn_pipeline.config import get_config

    source = _me_source(paths.run_scratch_dir(project_id))
    gain = dub.gain_for_source(get_config(), source)
    detail = f"{me_wav.name} -- source {source}, gain {gain:+g} dB"
    try:
        from pydub import AudioSegment
        detail += f" (bed {AudioSegment.from_wav(me_wav).dBFS:.0f} dBFS)"
    except Exception:
        pass
    return [_c("M&E bed", PASS, detail)]


def check_project(project_id: str, el_remaining: int | None) -> list[Check]:
    from cn_pipeline import paths
    out = []
    try:
        project_dir = paths.resolve_project_dir(project_id)
        out.append(_c("project dir", PASS, str(project_dir)))
    except Exception as e:
        return [_c("project dir", FAIL, str(e)[:200],
                   "check the project id matches the Drive folder name")]

    try:
        out.append(_c("master video", PASS, paths.find_master_video(project_dir).name))
    except Exception as e:
        out.append(_c("master video", FAIL, str(e)[:200],
                     "the master must sit in the project ROOT and its filename must "
                     "start with the exact project id"))

    out += check_claim(project_dir)

    me = paths.me_wav_path(project_dir)
    if me.exists():
        out += check_me_gain(project_id, me)
    else:
        out.append(_c("M&E bed", WARN, f"no {me.name}",
                     "dub mix-me will no-op and the dub ships VO-only, or generate "
                     "one with Demucs from the master"))

    est = estimate_tts_characters(project_id)
    if not est["known"]:
        out.append(_c("tts cost estimate", WARN, est["reason"]))
    else:
        detail = (f"{est['passages']} passages, {est['total_chars']:,} chars total; "
                  f"{len(est['pending'])} need generating = {est['billable_chars']:,} chars")
        if el_remaining is None:
            out.append(_c("tts cost estimate", WARN, detail + " (EL balance unknown)"))
        elif est["billable_chars"] == 0:
            out.append(_c("tts cost estimate", PASS, detail + " -- nothing to buy"))
        elif est["billable_chars"] > el_remaining:
            short = est["billable_chars"] - el_remaining
            out.append(_c("tts cost estimate", FAIL,
                          detail + f"; only {el_remaining:,} left -- short by {short:,}",
                          f"top up ElevenLabs before running, or this batch dies partway. "
                          f"Pending: {', '.join(est['pending'][:8])}"
                          + (" ..." if len(est["pending"]) > 8 else "")))
        else:
            out.append(_c("tts cost estimate", PASS,
                          detail + f"; {el_remaining:,} left -- fits"))
    return out


# --- driver ------------------------------------------------------------------

def run(project_id: str | None = None) -> list[Check]:
    checks: list[Check] = []
    checks += check_env_file()
    checks += check_config_file()
    checks += check_output_settings()
    checks += check_ffmpeg()
    checks += check_python_deps()

    # API checks need a usable Config; report that once and skip if broken.
    cfg = None
    try:
        from cn_pipeline.config import get_config
        cfg = get_config()
    except Exception as e:
        checks.append(_c("config load", FAIL, str(e).splitlines()[0][:200],
                         "fix the failures above first -- API and project checks skipped"))
        return checks

    el_checks, el_remaining = check_elevenlabs(cfg.elevenlabs_api_key)
    checks += el_checks
    checks += check_kie(cfg.kie_api_key)
    checks += check_frameio()
    checks += check_youtube()

    if project_id:
        checks += check_project(project_id, el_remaining)
    return checks


def format_report(checks: list[Check]) -> str:
    width = max((len(c.name) for c in checks), default=10)
    lines = []
    for c in checks:
        mark = {PASS: "ok  ", WARN: "warn", FAIL: "FAIL"}[c.status]
        lines.append(f"  [{mark}] {c.name.ljust(width)}  {c.detail}")
        if c.hint and c.status != PASS:
            lines.append(f"         {' ' * width}  -> {c.hint}")
    n_fail = sum(1 for c in checks if c.status == FAIL)
    n_warn = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    if n_fail:
        lines.append(f"{n_fail} FAIL, {n_warn} warning(s) -- fix the failures before running.")
    elif n_warn:
        lines.append(f"All critical checks passed ({n_warn} warning(s)).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)
