"""Assemble and play lessons.

The order of a lesson item is the whole pedagogical argument of this project,
so it is worth stating plainly. The obvious format — German word, English
meaning, German sentence — hands over the answer before you have tried to
recall it, and recognition builds far weaker memory than retrieval does.

What plays instead:

    die Verantwortung          German word, with its article
    ...........                pause: try to recall it
    Er übernimmt die           German sentence, so context can do some work
    Verantwortung.
    ...........                pause
    responsibility             English gloss, confirming or correcting
    He takes responsibility.   English sentence (optional)

The two pauses are the point. They cost seconds and convert passive listening
into retrieval practice, and they still work when you are only half paying
attention. `LessonFormat` keeps the order configurable so the original
word-meaning-sentence arrangement can be compared against this one.

Playback is ffplay: mpv is not installed on this machine, so the storysearch
autoplay path would fail here.
"""

import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import scheduler, tts


@dataclass
class LessonFormat:
    """Ordering and pacing of one lesson item."""

    # Sequence of beats. Each is (kind, language) where kind is one of
    # word / sentence_de / gloss / sentence_en / pause / shadow.
    beats: list[tuple[str, str]] = field(
        default_factory=lambda: [
            ("word", "de"),
            ("pause", "recall"),
            ("sentence_de", "de"),
            ("pause", "short"),
            ("gloss", "en"),
            ("sentence_en", "en"),
        ]
    )
    recall_pause: float = 2.5
    short_pause: float = 1.5
    shadow_pause: float = 2.0
    between_items: float = 1.0

    @classmethod
    def naive(cls) -> "LessonFormat":
        """Word, meaning, sentence — the format this project argues against.

        Kept so the two can be compared directly rather than taken on faith.
        """
        return cls(
            beats=[
                ("word", "de"),
                ("gloss", "en"),
                ("sentence_de", "de"),
            ]
        )

    @classmethod
    def with_shadowing(cls) -> "LessonFormat":
        """Adds a beat to repeat the word aloud. Production aids encoding."""
        fmt = cls()
        fmt.beats = fmt.beats[:2] + [("shadow", "de")] + fmt.beats[2:]
        return fmt


def item_texts(item: dict, fmt: LessonFormat) -> list[tuple[str, str, str]]:
    """Expand one vocabulary item into (kind, text, language) beats.

    Beats whose content is missing are dropped rather than spoken empty.
    """
    sentence = item.get("sentence") or {}
    content = {
        "word": (item["display"], "de"),
        "gloss": (item["gloss"], "en"),
        "sentence_de": (sentence.get("de"), "de"),
        "sentence_en": (sentence.get("en"), "en"),
    }

    out = []
    for kind, lang in fmt.beats:
        if kind in ("pause", "shadow"):
            out.append((kind, "", lang))
            continue
        text, text_lang = content.get(kind, (None, lang))
        if text:
            out.append((kind, text, text_lang))
    return out


def pause_seconds(kind: str, lang: str, fmt: LessonFormat) -> float:
    if kind == "shadow":
        return fmt.shadow_pause
    return fmt.recall_pause if lang == "recall" else fmt.short_pause


# ------------------------------------------------------------------ playback


def play_wav(path: Path) -> None:
    subprocess.run(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
        check=False,
    )


def item_duration(item: dict, fmt: LessonFormat) -> float | None:
    """Exact wall time for one item, or None if its clips are not all cached."""
    total = 0.0
    for kind, text, lang in item_texts(item, fmt):
        if kind in ("pause", "shadow"):
            total += pause_seconds(kind, lang, fmt)
            continue
        clip = tts.cached_only(text, lang)
        if clip is None:
            return None
        total += tts.duration_of(clip) or 0.0
    return total + fmt.between_items


def measured_duration_fn(fmt: LessonFormat, fallback: float):
    """A per-item duration function for build_timeline, falling back per item."""

    def duration(item: dict) -> float:
        return item_duration(item, fmt) or fallback

    return duration


def estimate_item_seconds(fmt: LessonFormat, items: list[dict] | None = None) -> float:
    """Mean wall time per item, used to size and lay out a session.

    Measured across every item whose clips are cached, not a leading sample —
    sentence lengths vary enough that the first dozen items are not
    representative, and an over-estimate here directly under-fills the session.
    """
    if items:
        measured = [d for d in (item_duration(i, fmt) for i in items) if d]
        if measured:
            return sum(measured) / len(measured)

    pauses = sum(
        pause_seconds(kind, lang, fmt)
        for kind, lang in fmt.beats
        if kind in ("pause", "shadow")
    ) + fmt.between_items
    return 1.2 + 2.8 + 1.0 + 2.8 + pauses


def prepare(conn, items: list[dict], fmt: LessonFormat, verbose: bool = True) -> int:
    """Synthesise every clip a set of items needs. Returns clips created."""
    created = 0
    for index, item in enumerate(items, 1):
        for kind, text, lang in item_texts(item, fmt):
            if kind in ("pause", "shadow"):
                continue
            if tts.cached_only(text, lang) is None:
                tts.synthesize(text, lang, conn=conn)
                created += 1
        if verbose:
            print(f"\r  prepared {index}/{len(items)} words", end="", flush=True)
    if verbose:
        print()
    return created


def play_item(conn, item: dict, fmt: LessonFormat, mode: str = "passive") -> str | None:
    """Play one item. In quiz mode, collect a grade at the recall pause."""
    outcome = None
    for kind, text, lang in item_texts(item, fmt):
        if kind in ("pause", "shadow"):
            if mode == "quiz" and kind == "pause" and lang == "recall":
                outcome = _ask()
            else:
                time.sleep(pause_seconds(kind, lang, fmt))
            continue

        clip = tts.cached_only(text, lang)
        if clip is None:
            try:
                clip = tts.synthesize(text, lang, conn=conn)
            except tts.TTSUnavailable:
                continue
        play_wav(clip)

    time.sleep(fmt.between_items)
    return outcome


def _ask() -> str:
    """Ask whether the word was recalled. Anything else counts as unsure."""
    try:
        answer = input("    did you get it? [y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise
    return "got" if answer.startswith("y") else "missed"


def run_session(
    conn,
    minutes: float,
    mode: str = "passive",
    fmt: LessonFormat | None = None,
    count: int | None = None,
    require_sentence: bool = True,
    has_anki_signal: bool = False,
    min_rank: int = scheduler.MIN_RANK,
) -> dict:
    """Play a full session. Returns a summary."""
    fmt = fmt or LessonFormat()
    per_item = estimate_item_seconds(fmt)
    total_seconds = minutes * 60
    count = count or scheduler.words_needed(total_seconds, per_item)

    items = scheduler.pick_items(
        conn,
        count,
        require_sentence=require_sentence,
        has_anki_signal=has_anki_signal,
        min_rank=min_rank,
    )
    if not items:
        return {"played": 0, "words": 0, "note": "no items available"}

    timeline = scheduler.build_timeline(
        items, measured_duration_fn(fmt, per_item), total_seconds
    )

    played = 0
    graded = {"got": 0, "missed": 0}
    started = time.time()

    try:
        for slot in timeline:
            if time.time() - started > total_seconds:
                break
            item = slot["item"]
            outcome = play_item(conn, item, fmt, mode=mode)
            played += 1

            from . import db as dbmod

            dbmod.record_exposure(conn, item["lemma"], mode, outcome)
            if outcome:
                graded[outcome] += 1
                scheduler.grade(conn, item["lemma"], outcome == "got")
            else:
                scheduler.touch(conn, item["lemma"])
            conn.commit()
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C, or stdin closing in a non-interactive run: end the session
        # cleanly and keep whatever grades were already recorded.
        pass

    return {
        "played": played,
        "words": len({s["item"]["lemma"] for s in timeline[:played]}),
        "elapsed_min": round((time.time() - started) / 60, 1),
        **({"graded": graded} if mode == "quiz" else {}),
    }


# -------------------------------------------------------------------- export


def export_session(
    conn,
    minutes: float,
    out_path: Path,
    fmt: LessonFormat | None = None,
    **kwargs,
) -> Path:
    """Render a session to a single MP3 for offline listening.

    This is how the trainer actually gets used — on a commute or at the gym,
    where a laptop session is not an option.
    """
    fmt = fmt or LessonFormat()
    per_item = estimate_item_seconds(fmt)
    count = kwargs.pop("count", None) or scheduler.words_needed(minutes * 60, per_item)

    items = scheduler.pick_items(conn, count, **kwargs)
    if not items:
        raise RuntimeError("no items available to export")

    prepare(conn, items, fmt)

    # The word count above came from a generic estimate; now that clips exist
    # their real durations are known, and they are typically shorter. Without
    # this refinement the session runs out of material and finishes ~25% short
    # of the requested length.
    total_seconds = minutes * 60
    per_item = estimate_item_seconds(fmt, items)
    needed = scheduler.words_needed(total_seconds, per_item)
    if needed > len(items):
        seen = {item["lemma"] for item in items}
        extra = [
            item
            for item in scheduler.pick_items(conn, needed + len(seen), **kwargs)
            if item["lemma"] not in seen
        ][: needed - len(items)]
        if extra:
            prepare(conn, extra, fmt)
            items.extend(extra)
            per_item = estimate_item_seconds(fmt, items)

    timeline = scheduler.build_timeline(
        items, measured_duration_fn(fmt, per_item), total_seconds
    )

    segments: list[Path] = []
    silences: dict[float, Path] = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="lazy-anki-export-"))

    def silence(seconds: float) -> Path:
        if seconds not in silences:
            path = tmpdir / f"sil_{int(seconds * 1000)}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i",
                    f"anullsrc=r={tts.OUTPUT_SAMPLE_RATE}:cl=mono",
                    "-t", str(seconds),
                    # Must match the TTS clips exactly. XTTS emits pcm_f32le,
                    # and ffmpeg's concat demuxer requires identical stream
                    # parameters across inputs — mismatched sample formats
                    # silently cost ~18% of every exported session.
                    "-c:a", tts.OUTPUT_SAMPLE_FORMAT,
                    str(path),
                ],
                check=True,
            )
            silences[seconds] = path
        return silences[seconds]

    for slot in timeline:
        for kind, text, lang in item_texts(slot["item"], fmt):
            if kind in ("pause", "shadow"):
                segments.append(silence(pause_seconds(kind, lang, fmt)))
                continue
            clip = tts.cached_only(text, lang)
            if clip is not None:
                segments.append(clip)
        segments.append(silence(fmt.between_items))

    listing = tmpdir / "list.txt"
    listing.write_text("".join(f"file '{s}'\n" for s in segments))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-ar", str(tts.OUTPUT_SAMPLE_RATE), "-ac", "1",
            "-c:a", "libmp3lame", "-q:a", "5",
            str(out_path),
        ],
        check=True,
    )

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
    return out_path
