"""lazy-anki-german — command line entry point."""

import argparse
import sys
import time
from pathlib import Path

from lazy_anki_german import (
    anki_import,
    db,
    freshness,
    player,
    scheduler,
    tts,
    voice_setup,
)


def _fmt_from_args(args) -> player.LessonFormat:
    if getattr(args, "naive_format", False):
        return player.LessonFormat.naive()
    if getattr(args, "shadow", False):
        return player.LessonFormat.with_shadowing()
    return player.LessonFormat()


def _anki_signal(conn) -> bool:
    """Whether Anki lapse data is rich enough to order lessons by."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM anki_stats WHERE lapses > 0"
    ).fetchone()
    return (row["n"] or 0) > 20


# ------------------------------------------------------------------ commands


def cmd_import(args) -> int:
    conn = db.connect(args.db)
    print(f"Reading {args.collection} ...")
    result = anki_import.import_collection(conn, Path(args.collection))

    freq = result["frequency"]
    print(f"\n  {result['vocab']:,} words   {result['with_sentences']:,} with sentences"
          f"   {result['sentences']:,} sentence pairs")
    print(f"  {result['with_article']:,} with articles"
          f"   {freq['with_zipf']:,} frequency-ranked ({freq['coverage']:.0%})")
    print(f"\n{freshness.describe(result['sync'])}")
    return 0


def cmd_voice(args) -> int:
    print("Building German reference voice ...")
    results = voice_setup.setup(force=args.force)
    for name, info in results.items():
        state = "installed" if info["installed"] else "BUILT but not installed"
        print(f"  {name}: {state}")

    if not tts.is_ready():
        print("\n! AllTalk is not running — start it with:")
        print("    docker compose up -d alltalk")
        return 1

    print("\nChecking the voice produces sane output ...")
    for name in results:
        check = voice_setup.check_voice(name)
        mark = "OK" if check["stable"] else "UNSTABLE"
        print(f"  {name}: {mark} ({check['failures']} runaway clips of "
              f"{len(check['clips'])})")

    if args.compare:
        out = Path("audio/voice_comparison.mp3")
        voice_setup.build_comparison(out)
        print(f"\nComparison written to {out} — listen and pick a voice.")
    return 0


def cmd_prefetch(args) -> int:
    conn = db.connect(args.db)
    fmt = _fmt_from_args(args)
    items = scheduler.pick_items(
        conn, args.limit, has_anki_signal=_anki_signal(conn), min_rank=args.min_rank
    )
    if not items:
        print("Nothing to prefetch — run import-anki first.")
        return 1

    print(f"Synthesising clips for {len(items)} words ...")
    started = time.time()
    created = player.prepare(conn, items, fmt)
    print(f"  {created} new clips in {time.time() - started:.0f}s "
          f"({conn.execute('SELECT COUNT(*) FROM clips').fetchone()[0]:,} cached total)")
    return 0


def cmd_play(args) -> int:
    conn = db.connect(args.db)
    fmt = _fmt_from_args(args)
    mode = "quiz" if args.quiz else "passive"

    print(f"{mode.title()} session, {args.minutes} minutes. Ctrl-C to stop.\n")
    summary = player.run_session(
        conn,
        minutes=args.minutes,
        mode=mode,
        fmt=fmt,
        has_anki_signal=_anki_signal(conn),
        min_rank=args.min_rank,
    )
    print(f"\n  played {summary['played']} clips over {summary['words']} words "
          f"in {summary.get('elapsed_min', 0)} min")
    if "graded" in summary:
        print(f"  got {summary['graded']['got']}, missed {summary['graded']['missed']}")
    return 0


def cmd_export(args) -> int:
    conn = db.connect(args.db)
    fmt = _fmt_from_args(args)
    out = Path(args.output)

    print(f"Building a {args.minutes}-minute session ...")
    path = player.export_session(
        conn,
        minutes=args.minutes,
        out_path=out,
        fmt=fmt,
        has_anki_signal=_anki_signal(conn),
        min_rank=args.min_rank,
    )
    size = path.stat().st_size / 1e6
    print(f"\n  wrote {path} ({size:.1f} MB)")
    return 0


def cmd_stats(args) -> int:
    conn = db.connect(args.db)
    counts = db.stats(conn)

    print("vocabulary")
    print(f"  {counts['vocab']:,} words, {counts['with_sentences']:,} with sentences")
    print(f"  {counts['sentences']:,} sentence pairs, "
          f"{counts['with_article']:,} with articles")

    print("\nyour progress")
    print(f"  {counts['exposures']:,} exposures, {counts['scheduled']:,} words scheduled")
    due = conn.execute(
        "SELECT COUNT(*) FROM schedule WHERE due_ts <= ?", (int(time.time()),)
    ).fetchone()[0]
    print(f"  {due:,} due now")

    print("\naudio")
    cached = counts["clips"]
    size = sum(p.stat().st_size for p in tts.CACHE_DIR.glob("*.wav")) / 1e6 if tts.CACHE_DIR.exists() else 0
    print(f"  {cached:,} clips cached ({size:.0f} MB)")
    print(f"  AllTalk: {'running' if tts.is_ready() else 'not running'}")

    print("\nanki")
    print(f"  {counts['anki_reviewed']:,} words reviewed, "
          f"{counts['anki_lapsed']:,} lapsed")
    last_sync = db.get_meta(conn, "anki_last_sync")
    if last_sync and last_sync != "0":
        age = (time.time() - int(last_sync)) / 3600
        print(f"  last synced {age:.0f}h ago")
    print(f"  weighting by difficulty: "
          f"{'yes' if _anki_signal(conn) else 'no — using word frequency'}")
    return 0


# --------------------------------------------------------------------- parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazy-anki-german",
        description="German vocabulary trainer that reads your Anki collection aloud.",
    )
    parser.add_argument("--db", default=str(db.DEFAULT_DB), help="path to the local DB")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-anki", help="import vocabulary from Anki")
    p.add_argument("--collection", default=str(anki_import.DEFAULT_COLLECTION))
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("setup-voice", help="build and install the German voice")
    p.add_argument("--force", action="store_true", help="rebuild even if present")
    p.add_argument("--compare", action="store_true", help="render a voice comparison")
    p.set_defaults(func=cmd_voice)

    def lesson_args(p):
        p.add_argument("--min-rank", type=int, default=scheduler.MIN_RANK,
                       help="skip words more frequent than this rank")
        p.add_argument("--naive-format", action="store_true",
                       help="word, meaning, sentence — no recall pause")
        p.add_argument("--shadow", action="store_true",
                       help="add a pause to repeat each word aloud")

    p = sub.add_parser("prefetch", help="synthesise clips ahead of a session")
    p.add_argument("--limit", type=int, default=200)
    lesson_args(p)
    p.set_defaults(func=cmd_prefetch)

    p = sub.add_parser("play", help="run a listening session")
    p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--quiz", action="store_true", help="grade yourself as you go")
    lesson_args(p)
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("export", help="render a session to MP3")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("-o", "--output", default="audio/session.mp3")
    lesson_args(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="show collection and progress stats")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except tts.TTSUnavailable as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
