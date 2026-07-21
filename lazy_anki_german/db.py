"""SQLite schema and queries for the local vocabulary store.

Two kinds of state live here, and the distinction matters:

  * Imported from Anki (`vocab`, `sentences`, `anki_stats`) — wiped and rebuilt
    on every import, so it always mirrors the collection.
  * Owned by this app (`exposures`, `schedule`, `clips`) — never touched by an
    import. This is what lets the trainer keep working, and keep adapting, when
    the Anki collection has no review history to learn from.
"""

import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "vokabel.db"

SCHEMA = """
-- ---------------------------------------------------------------- from Anki

CREATE TABLE IF NOT EXISTS vocab (
    lemma        TEXT PRIMARY KEY,   -- normalised key, e.g. "abbiegen"
    display      TEXT NOT NULL,      -- what gets spoken, e.g. "die Abbildung"
    article      TEXT,               -- der/die/das when known
    plural       TEXT,
    ipa          TEXT,
    pos          TEXT,
    gloss        TEXT NOT NULL,      -- English meaning
    curated_rank INTEGER,            -- rank as shipped by the deck; NULL if none
    zipf         REAL,               -- wordfreq Zipf score; 0 = unknown to corpora
    freq_rank    INTEGER,            -- unified 1-based ordering, 1 = most frequent
    source       TEXT NOT NULL,      -- which notetype it came from
    has_sentence INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vocab_freq ON vocab(freq_rank);
CREATE INDEX IF NOT EXISTS idx_vocab_zipf ON vocab(zipf);
CREATE INDEX IF NOT EXISTS idx_vocab_sent ON vocab(has_sentence);

CREATE TABLE IF NOT EXISTS sentences (
    id     INTEGER PRIMARY KEY,
    lemma  TEXT NOT NULL REFERENCES vocab(lemma) ON DELETE CASCADE,
    de     TEXT NOT NULL,
    en     TEXT,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentences_lemma ON sentences(lemma);

-- Per-lemma difficulty signal derived from Anki's cards/revlog. Empty until
-- the collection actually has review history.
CREATE TABLE IF NOT EXISTS anki_stats (
    lemma       TEXT PRIMARY KEY REFERENCES vocab(lemma) ON DELETE CASCADE,
    lapses      INTEGER NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    ease        INTEGER,             -- Anki `factor`, 2500 = default
    ivl         INTEGER,
    last_review INTEGER              -- unix seconds
);

-- ------------------------------------------------------------- owned by app

CREATE TABLE IF NOT EXISTS exposures (
    id      INTEGER PRIMARY KEY,
    lemma   TEXT NOT NULL,
    ts      INTEGER NOT NULL,        -- unix seconds
    mode    TEXT NOT NULL,           -- 'passive' | 'quiz'
    outcome TEXT                     -- 'got' | 'missed' | NULL for passive
);

CREATE INDEX IF NOT EXISTS idx_exposures_lemma ON exposures(lemma);
CREATE INDEX IF NOT EXISTS idx_exposures_ts ON exposures(ts);

CREATE TABLE IF NOT EXISTS schedule (
    lemma         TEXT PRIMARY KEY,
    ease          REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    due_ts        INTEGER NOT NULL DEFAULT 0,
    streak        INTEGER NOT NULL DEFAULT 0,
    seen_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_schedule_due ON schedule(due_ts);

-- Content-addressed TTS cache. Every distinct (text, language, voice) is
-- synthesised exactly once, ever.
CREATE TABLE IF NOT EXISTS clips (
    hash     TEXT PRIMARY KEY,
    text     TEXT NOT NULL,
    lang     TEXT NOT NULL,
    voice    TEXT NOT NULL,
    path     TEXT NOT NULL,
    duration REAL,
    created  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the vocabulary DB with the schema applied."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------- meta


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ----------------------------------------------------------------- exposures


def record_exposure(
    conn: sqlite3.Connection, lemma: str, mode: str, outcome: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO exposures(lemma, ts, mode, outcome) VALUES(?, ?, ?, ?)",
        (lemma, int(time.time()), mode, outcome),
    )


# --------------------------------------------------------------------- stats


def stats(conn: sqlite3.Connection) -> dict:
    """Counts used by the `stats` command and by import verification."""
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "vocab": one("SELECT COUNT(*) FROM vocab"),
        "with_sentences": one("SELECT COUNT(*) FROM vocab WHERE has_sentence = 1"),
        "sentences": one("SELECT COUNT(*) FROM sentences"),
        "with_article": one("SELECT COUNT(*) FROM vocab WHERE article IS NOT NULL AND article != ''"),
        "anki_reviewed": one("SELECT COUNT(*) FROM anki_stats WHERE reps > 0"),
        "anki_lapsed": one("SELECT COUNT(*) FROM anki_stats WHERE lapses > 0"),
        "exposures": one("SELECT COUNT(*) FROM exposures"),
        "scheduled": one("SELECT COUNT(*) FROM schedule"),
        "clips": one("SELECT COUNT(*) FROM clips"),
    }
