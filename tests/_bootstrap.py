"""
Test bootstrap: put the repo root on sys.path, and stub the heavy runtime deps
(torch/whisper/easyocr/pydub/PIL/requests) ONLY when they aren't installed.

These are pure-logic tests -- cue clustering, timeline math, comment
classification, the stage gate. None of them calls into TTS, OCR, ffmpeg, or
an HTTP client; they only need the modules to *import*. So in the full project
venv the real deps are used, and in a bare checkout (CI, a fresh clone) the
stubs let the same tests run unchanged. Import this and call install() at the
top of every test file, before importing anything from cn_pipeline.
"""

import os
import sys
import types


def install() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    def ensure(name: str, factory=None) -> None:
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            if factory:
                factory(mod)
            sys.modules[name] = mod

    ensure("requests")
    ensure("whisper")
    ensure("torch")
    ensure("easyocr")
    ensure("pydub", lambda m: setattr(m, "AudioSegment", object))

    try:
        import PIL  # noqa: F401
    except ImportError:
        pil = types.ModuleType("PIL")
        for sub in ("Image", "ImageDraw", "ImageFont"):
            m = types.ModuleType(f"PIL.{sub}")
            setattr(pil, sub, m)
            sys.modules[f"PIL.{sub}"] = m
        sys.modules["PIL"] = pil


def run_module(namespace: dict) -> None:
    """Standalone runner: call every test_* function in `namespace` so a file
    runs with plain `python tests/test_x.py` (no pytest needed) as well as
    under pytest."""
    failures = 0
    for name in sorted(namespace):
        fn = namespace[name]
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all passed")
