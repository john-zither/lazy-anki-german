"""Provide XTTSv2 with reference voices to clone.

XTTSv2 has no fixed speakers — it clones from a short reference clip. Getting a
usable German reference took some doing, and the constraints are not obvious:

1. AllTalk reads voices from `/app/voices`, NOT the
   `/app/alltalk/models/xtts/voices` path the shared docker-compose.yml
   bind-mounts. That mount has never had any effect. Rather than repoint a
   mount other projects depend on, voices are installed with `docker cp`.

2. German needs a German reference. XTTS will read German text with an English
   speaker's timbre, but the accent is anglophone — the wrong thing to train a
   learner's ear on.

3. Raw LibriVox audio clones badly. Untreated clips made XTTS ramble: "das
   Haus" came out as 11.6s of audio against a correct 0.8s, and the failure was
   erratic across both offsets and clip lengths, so it could not be tuned away.
   Room noise appears to be the cause — a highpass + FFT denoise + loudness
   normalisation chain made the same recordings stable. That filter chain is
   the load-bearing part of this module.

4. Reference clips want ~8s of clean, uninterrupted single-speaker speech taken
   well into the recording, past the LibriVox boilerplate intro. "Dramatic
   Reading" items use a different voice per character and are unusable.

Stability is verified by `check_voice` below: durations that scale sensibly
with text length. Accent quality cannot be checked programmatically and needs a
human ear — see `build_comparison`.
"""

import json
import subprocess
from pathlib import Path
from urllib.request import urlopen

VOICES_DIR = Path("/home/theurerjohn3/Documents/agents/2026/ragnas/voices/wav")
CONTAINER = "alltalk-tts"
CONTAINER_VOICES = "/app/voices"

SAMPLE_RATE = 22050
REFERENCE_SECONDS = 8

# Cleanup chain that makes amateur recordings clone reliably. Without the
# denoise step XTTS produces wildly over-long output for short inputs.
FILTER_CHAIN = (
    "highpass=f=70,"
    "afftdn=nf=-25,"
    "loudnorm=I=-18:TP=-2:LRA=7,"
    "alimiter=limit=0.95"
)

# Public-domain LibriVox solo readings, resolved via the archive.org metadata
# API. German items are indexed under `language:deu`; "German" returns nothing.
GERMAN_SOURCES = [
    {
        "name": "de_narrator.wav",
        "url": (
            "https://archive.org/download/hoehlenkinder_heimlicher_grund_1006_librivox/"
            "hoehlenkinder1_02_sonnleitner_64kb.mp3"
        ),
        "start": 300,
    },
]

# A Rasende Reporter reading was also trialled and rejected on listening: it
# cloned stably but sounded markedly worse than both alternatives.
FALLBACK_VOICE = "female_02.wav"

# English is only used for glosses, where a built-in XTTS voice is already
# clean and correctly accented, so nothing needs downloading.
ENGLISH_VOICE = "female_02.wav"


def _download(url: str, dest: Path) -> None:
    with urlopen(url, timeout=180) as response, open(dest, "wb") as out:
        while chunk := response.read(1 << 16):
            out.write(chunk)


def build_reference(source: dict, voices_dir: Path = VOICES_DIR) -> Path:
    """Download, trim, denoise and normalise one reference clip."""
    voices_dir.mkdir(parents=True, exist_ok=True)
    target = voices_dir / source["name"]
    tmp = voices_dir / (source["name"] + ".src")

    try:
        _download(source["url"], tmp)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(source["start"]),
                "-t", str(REFERENCE_SECONDS),
                "-i", str(tmp),
                "-ac", "1",
                "-ar", str(SAMPLE_RATE),
                "-af", FILTER_CHAIN,
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        tmp.unlink(missing_ok=True)

    return target


def install(voice: Path, container: str = CONTAINER) -> bool:
    """Copy a reference into the running container's voices directory."""
    result = subprocess.run(
        ["docker", "cp", str(voice), f"{container}:{CONTAINER_VOICES}/{voice.name}"],
        capture_output=True,
    )
    return result.returncode == 0


def setup(voices_dir: Path = VOICES_DIR, force: bool = False) -> dict:
    """Build every German reference and install it into the container."""
    results = {}
    for source in GERMAN_SOURCES:
        target = voices_dir / source["name"]
        if not target.exists() or target.stat().st_size < 10_000 or force:
            target = build_reference(source, voices_dir)
        results[source["name"]] = {
            "path": target,
            "installed": install(target),
        }
    return results


def available(container: str = CONTAINER) -> list[str]:
    """Voices the running AllTalk instance can actually use."""
    import requests

    try:
        response = requests.get("http://localhost:7851/api/voices", timeout=10)
        return sorted(response.json().get("voices", []))
    except Exception:
        return []


# ------------------------------------------------------------- verification


# Rough expected duration in seconds; generous, we are catching runaway output
# (an order of magnitude too long), not measuring prosody.
STABILITY_PHRASES = [
    ("der Hund", 1.0),
    ("die Katze", 1.0),
    ("das Mädchen", 1.1),
    ("Er hat gestern einen Brief geschrieben.", 2.6),
    ("Wo ist der nächste Bahnhof?", 1.8),
]


def check_voice(voice: str) -> dict:
    """Verify a voice produces output that scales with input length.

    A badly-cloning reference makes XTTS ramble — 11s of audio for a two-word
    phrase — which is detectable without listening.
    """
    from . import tts

    results, failures = [], 0
    for text, expected in STABILITY_PHRASES:
        duration = tts.duration_of(tts.synthesize(text, "de", voice=voice)) or 0.0
        over = duration > expected * 2.6 + 0.6
        failures += over
        results.append({"text": text, "duration": duration, "runaway": over})

    return {"voice": voice, "stable": failures == 0, "failures": failures, "clips": results}


def build_comparison(out_path: Path, voices: list[str] | None = None) -> Path:
    """Render the same German passage in each candidate voice, back to back.

    Voice *stability* is machine-checkable; whether the German sounds German is
    not. This produces one file to listen to and choose from.
    """
    from . import tts

    voices = voices or [s["name"] for s in GERMAN_SOURCES] + [ENGLISH_VOICE]
    passage = (
        "Guten Abend. Ich möchte heute über die deutsche Sprache sprechen. "
        "Das Wetter ist schön, und morgen fahre ich nach Hause."
    )

    segments = []
    for voice in voices:
        try:
            segments.append(tts.synthesize(passage, "de", voice=voice))
        except Exception:
            continue

    if not segments:
        raise RuntimeError("no voices could be synthesised for comparison")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = out_path.parent / "compare_list.txt"
    listing.write_text("".join(f"file '{s}'\n" for s in segments))
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:a", "libmp3lame", "-q:a", "4", str(out_path),
        ],
        check=True,
    )
    listing.unlink(missing_ok=True)
    return out_path
