"""Speech synthesis via AllTalk (XTTSv2), with a permanent on-disk cache.

Adapted from the proven client in ../storysearch/audiobook/generator.py, with
one substantive change: `language` is a parameter rather than hardcoded "en",
because this project alternates German and English within a single lesson.

Caching is not an optimisation here, it is the design. The GPU is a 6GB RTX
2060 running with AT_LOWVRAM, so synthesis is far too slow to do during a
lesson. Every distinct (text, language, voice) is synthesised once, keyed by
content hash, and reused forever — after a prefetch, sessions start instantly
and run with the container stopped.
"""

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import requests

ALLTALK_URL = "http://localhost:7851"

# XTTSv2 renders at 24kHz mono. Silence padding and any concatenation must match
# it exactly — mixing rates in ffmpeg's concat demuxer produces non-monotonic
# DTS warnings and can corrupt timing in the joined file.
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_SAMPLE_FORMAT = "pcm_f32le"

# Where AllTalk drops generated files on the host (bind mount from compose).
ALLTALK_STORAGE = Path(
    "/home/theurerjohn3/Documents/agents/2026/ragnas/alltalk_storage"
)

CACHE_DIR = Path(__file__).resolve().parent.parent / "audio" / "cache"

# Voice reference wavs, as AllTalk names them (files under xtts/voices).
VOICE_DE = "de_narrator.wav"
VOICE_EN = "female_02.wav"


class TTSUnavailable(RuntimeError):
    """AllTalk is not reachable and the clip is not already cached."""


def clip_hash(text: str, lang: str, voice: str) -> str:
    return hashlib.sha256(f"{text}\x00{lang}\x00{voice}".encode()).hexdigest()[:32]


def cache_path(text: str, lang: str, voice: str) -> Path:
    return CACHE_DIR / f"{clip_hash(text, lang, voice)}.wav"


def is_ready(url: str = ALLTALK_URL, timeout: float = 3.0) -> bool:
    try:
        return requests.get(f"{url}/api/ready", timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def wait_ready(url: str = ALLTALK_URL, retries: int = 30, delay: float = 5.0) -> bool:
    for _ in range(retries):
        if is_ready(url):
            return True
        time.sleep(delay)
    return False


def voice_for(lang: str) -> str:
    return VOICE_DE if lang == "de" else VOICE_EN


def synthesize(
    text: str,
    lang: str,
    voice: str | None = None,
    url: str = ALLTALK_URL,
    conn=None,
) -> Path:
    """Return a wav for `text`, synthesising only on a cache miss.

    Raises TTSUnavailable when the clip is absent and AllTalk cannot be reached,
    so callers can distinguish "not built yet" from "engine down".
    """
    text = text.strip()
    if not text:
        raise ValueError("refusing to synthesize empty text")

    voice = voice or voice_for(lang)
    dest = cache_path(text, lang, voice)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 1000:
        return dest

    if not is_ready(url):
        raise TTSUnavailable(
            f"AllTalk not reachable at {url} and no cached clip for {text!r}. "
            "Start it with: docker compose up -d alltalk-tts"
        )

    filename = dest.stem
    payload = {
        "text_input": text,
        "text_filtering": "standard",
        "character_voice_gen": voice,
        "narrator_enabled": "false",
        "narrator_voice_gen": voice,
        "text_not_inside": "narrator",
        "language": lang,
        "output_file_name": filename,
        "output_file_timestamp": "false",
        "autoplay": "false",
        "autoplay_volume": "0.8",
    }

    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(
                f"{url}/api/tts-generate",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=300,
            )
            if response.status_code == 200 and response.json().get("status") == "generate-success":
                produced = ALLTALK_STORAGE / f"{filename}.wav"
                if produced.exists():
                    # AllTalk runs as root in the container, so its output is
                    # root-owned on the host: copy it out, then remove it only
                    # if permissions allow.
                    shutil.copy2(produced, dest)
                    try:
                        produced.unlink()
                    except OSError:
                        pass
                    if conn is not None:
                        _record(conn, dest, text, lang, voice)
                    return dest
                last_error = f"AllTalk reported success but {produced} is missing"
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < 2:
            time.sleep(5)

    raise TTSUnavailable(f"Synthesis failed for {text!r}: {last_error}")


def _record(conn, path: Path, text: str, lang: str, voice: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO clips(hash, text, lang, voice, path, duration, created)"
        " VALUES(?,?,?,?,?,?,?)",
        (path.stem, text, lang, voice, str(path), duration_of(path), int(time.time())),
    )
    conn.commit()


def duration_of(path: Path) -> float | None:
    """Clip length in seconds via ffprobe; None if it cannot be determined."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def cached_only(text: str, lang: str, voice: str | None = None) -> Path | None:
    """Look up a clip without ever synthesising. Used during playback."""
    path = cache_path(text, lang, voice or voice_for(lang))
    return path if path.exists() and path.stat().st_size > 1000 else None
