"""Import German vocabulary from an Anki collection.

The collection is never opened in place. Anki keeps it in WAL mode and holds it
open while running, so we copy `collection.anki2` *together with* its `-wal` and
`-shm` sidecars — copying the main file alone yields a stale snapshot from the
last checkpoint, silently missing every recent review and sync.

Anki also registers a custom `unicase` collation. Any query touching a collated
column (notetypes.name, decks.name) raises OperationalError unless we provide
our own implementation.

Four notetypes carry usable German, and they disagree about everything:

  English-Deutsch          frequency-ranked, 3 senses/sentence pairs per note
  Basic (and reversed)-*   B1 Goethe list; article + plural + up to 9 sentences
  0 Neri's Frequent Words  sentences buried in an HTML table
  Basic+                   81k bare word -> gloss pairs, no sentences at all
"""

import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_COLLECTION = Path.home() / ".local/share/Anki2/User 1/collection.anki2"

FIELD_SEP = "\x1f"
ARTICLES = ("der", "die", "das")

# Notetype names, as they appear in this collection.
NT_ENGLISH_DEUTSCH = "English-Deutsch"
NT_B1 = "Basic (and reversed card)-7c609"
NT_NERI_FE = "0 Neri's Frequent Words (F to E)"
NT_NERI_EF = "0 Neri's Frequent Words (E to F)"
NT_VOKABELN = "Basic+"


# --------------------------------------------------------------------- text


_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#39);")
_ENTITIES = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'"}


def clean(text: str | None) -> str:
    """Strip Anki sound tags, HTML and entities down to speakable plain text."""
    if not text:
        return ""
    text = _SOUND_RE.sub(" ", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(lambda m: _ENTITIES[m.group(1)], text)
    return re.sub(r"\s+", " ", text).strip(" \t\n\r,;")


def lemma_of(word: str) -> str:
    """Normalise a headword to a dedupe key.

    "die Abbildung, -en"                    -> "abbildung"
    "abbiegen, biegt ab, bog ab, ist ..."   -> "abbiegen"
    """
    base = clean(word).split(",")[0].strip()
    parts = base.split()
    if len(parts) > 1 and parts[0].lower() in ARTICLES:
        base = " ".join(parts[1:])
    return base.lower().strip(" .!?")


def split_article(word: str) -> tuple[str | None, str]:
    """Pull a leading der/die/das off a headword, returning (article, rest)."""
    cleaned = clean(word)
    parts = cleaned.split()
    if len(parts) > 1 and parts[0].lower() in ARTICLES:
        return parts[0].lower(), " ".join(parts[1:])
    return None, cleaned


class _TableRows(HTMLParser):
    """Collect <td> text per <tr>. Used for Neri's example-sentence tables."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_sentence_table(html: str) -> list[tuple[str, str]]:
    """Extract (german, english) pairs from Neri's <table id="fullExSents">."""
    if not html or "<t" not in html:
        return []
    parser = _TableRows()
    try:
        parser.feed(html)
    except Exception:
        return []
    pairs = []
    for row in parser.rows:
        # Columns are p, q, n, Sentence, English.
        if len(row) >= 5 and row[3] and row[4]:
            pairs.append((row[3], row[4]))
    return pairs


# -------------------------------------------------------------------- model


@dataclass
class Entry:
    lemma: str
    display: str
    gloss: str
    source: str
    article: str | None = None
    plural: str | None = None
    ipa: str | None = None
    pos: str | None = None
    freq_rank: int | None = None
    sentences: list[tuple[str, str]] = field(default_factory=list)
    note_ids: list[int] = field(default_factory=list)

    def richness(self) -> int:
        """Merge priority — prefer records that can drive the full lesson format."""
        return (
            4 * bool(self.sentences)
            + 2 * bool(self.article)
            + bool(self.ipa)
            + bool(self.freq_rank)
        )


# ---------------------------------------------------------------- collection


def open_collection(path: Path = DEFAULT_COLLECTION) -> tuple[sqlite3.Connection, Path]:
    """Copy the collection (with WAL sidecars) somewhere safe and open it."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Anki collection not found: {path}")

    tmpdir = Path(tempfile.mkdtemp(prefix="lazy-anki-"))
    dest = tmpdir / "collection.anki2"
    shutil.copy2(path, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, dest.with_name(dest.name + suffix))

    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    conn.create_collation(
        "unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower())
    )
    return conn, tmpdir


def collection_sync_info(conn: sqlite3.Connection) -> dict:
    """Raw sync/review facts from the collection.

    `col.ls` is Anki's own last-sync timestamp — a far more reliable freshness
    signal than inferring one from the newest revlog row, which stays silent
    when a sync legitimately brings nothing down.
    """
    row = conn.execute("SELECT crt, mod, ls FROM col").fetchone()
    revlog_count = conn.execute("SELECT COUNT(*) FROM revlog").fetchone()[0]
    newest = conn.execute("SELECT MAX(id) FROM revlog").fetchone()[0]
    lapsed = conn.execute("SELECT COUNT(*) FROM cards WHERE lapses > 0").fetchone()[0]
    reviewed = conn.execute("SELECT COUNT(*) FROM cards WHERE reps > 0").fetchone()[0]
    return {
        "last_sync": int(row["ls"] / 1000) if row["ls"] else None,
        "collection_modified": int(row["mod"] / 1000) if row["mod"] else None,
        "revlog_count": revlog_count,
        "newest_review": int(newest / 1000) if newest else None,
        "cards_reviewed": reviewed,
        "cards_lapsed": lapsed,
    }


def _notetypes(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM notetypes")}


def _field_index(conn: sqlite3.Connection, ntid: int) -> dict[str, int]:
    return {
        r["name"]: i
        for i, r in enumerate(
            conn.execute("SELECT name FROM fields WHERE ntid = ? ORDER BY ord", (ntid,))
        )
    }


def _notes(conn: sqlite3.Connection, ntid: int):
    for row in conn.execute("SELECT id, flds FROM notes WHERE mid = ?", (ntid,)):
        yield row["id"], row["flds"].split(FIELD_SEP)


def _get(fields: list[str], idx: dict[str, int], name: str) -> str:
    i = idx.get(name)
    return fields[i] if i is not None and i < len(fields) else ""


# ------------------------------------------------------------------ parsers


def parse_english_deutsch(conn, ntid: int) -> list[Entry]:
    """Frequency-ranked, up to three senses each with its own sentence pair."""
    idx = _field_index(conn, ntid)
    entries = []
    for nid, f in _notes(conn, ntid):
        word = _get(f, idx, "Word")
        lemma = lemma_of(word)
        if not lemma:
            continue
        article, display = split_article(word)

        glosses, poss, sentences = [], [], []
        for n in (1, 2, 3):
            definition = clean(_get(f, idx, f"Definition {n}"))
            if definition:
                glosses.append(definition)
            pos = clean(_get(f, idx, f"Part-of-Speech {n}"))
            if pos:
                poss.append(pos)
            de = clean(_get(f, idx, f"German {n}"))
            en = clean(_get(f, idx, f"English {n}"))
            if de:
                sentences.append((de, en))

        if not glosses:
            continue

        rank = clean(_get(f, idx, "Rank"))
        entries.append(
            Entry(
                lemma=lemma,
                display=display,
                gloss="; ".join(glosses),
                source=NT_ENGLISH_DEUTSCH,
                article=article,
                ipa=clean(_get(f, idx, "IPA")) or None,
                pos=poss[0] if poss else None,
                freq_rank=int(rank) if rank.isdigit() else None,
                sentences=sentences,
                note_ids=[nid],
            )
        )
    return entries


def parse_b1(conn, ntid: int) -> list[Entry]:
    """Goethe/DTZ B1 list — the only source with explicit article and plural.

    `original_order` is alphabetical, not a frequency ranking, so it is not
    used as freq_rank.
    """
    idx = _field_index(conn, ntid)
    entries = []
    for nid, f in _notes(conn, ntid):
        base = _get(f, idx, "base_d") or _get(f, idx, "full_d")
        lemma = lemma_of(base)
        gloss = clean(_get(f, idx, "base_e"))
        if not lemma or not gloss:
            continue

        article = clean(_get(f, idx, "artikel_d")).lower() or None
        display = clean(_get(f, idx, "base_d")) or clean(base)
        if article and not display.lower().startswith(article):
            display = f"{article} {display}"

        sentences = []
        for n in range(1, 10):
            de = clean(_get(f, idx, f"s{n}"))
            en = clean(_get(f, idx, f"s{n}e"))
            if de:
                sentences.append((de, en))

        entries.append(
            Entry(
                lemma=lemma,
                display=display,
                gloss=gloss,
                source="B1_Wortliste_DTZ_Goethe",
                article=article,
                plural=clean(_get(f, idx, "plural_d")) or None,
                sentences=sentences,
                note_ids=[nid],
            )
        )
    return entries


def parse_neri(conn, ntid: int, source: str) -> list[Entry]:
    """Top-500 frequency words; sentences live in an HTML table."""
    idx = _field_index(conn, ntid)
    entries = []
    for nid, f in _notes(conn, ntid):
        word = _get(f, idx, "Word")
        lemma = lemma_of(word)
        if not lemma:
            continue

        raw_english = _get(f, idx, "English")
        pos_match = re.search(r'<span id="pos">.*?<i>(.*?)</i>', raw_english)
        gloss = clean(re.sub(r'<span id="pos">.*?</span>', " ", raw_english))
        if not gloss:
            continue

        article, display = split_article(_get(f, idx, "Word with Article") or word)
        index = clean(_get(f, idx, "Index"))

        entries.append(
            Entry(
                lemma=lemma,
                display=display,
                gloss=gloss,
                source=source,
                article=article,
                ipa=clean(_get(f, idx, "IPA")).strip("/") or None,
                pos=clean(pos_match.group(1)) if pos_match else None,
                freq_rank=int(index) if index.isdigit() else None,
                sentences=parse_sentence_table(_get(f, idx, "Example Sentences")),
                note_ids=[nid],
            )
        )
    return entries


def parse_vokabeln(conn, ntid: int) -> list[Entry]:
    """81k bare word -> gloss pairs. No sentences; the extended pool."""
    idx = _field_index(conn, ntid)
    front = idx.get("Front", 0)
    back = idx.get("Back", 1)
    entries = []
    for nid, f in _notes(conn, ntid):
        if len(f) <= max(front, back):
            continue
        lemma = lemma_of(f[front])
        gloss = clean(f[back])
        if not lemma or not gloss:
            continue
        article, display = split_article(f[front])
        entries.append(
            Entry(
                lemma=lemma,
                display=display,
                gloss=gloss,
                source="Vokabeln/DeuEng",
                article=article,
                note_ids=[nid],
            )
        )
    return entries


# -------------------------------------------------------------------- merge


def merge(groups: list[list[Entry]]) -> dict[str, Entry]:
    """Fold all sources into one entry per lemma, richest record winning.

    Sentences accumulate across sources even when another record wins the
    headword, so a B1 word also picks up its English-Deutsch examples.
    """
    merged: dict[str, Entry] = {}
    for entries in groups:
        for entry in entries:
            existing = merged.get(entry.lemma)
            if existing is None:
                merged[entry.lemma] = entry
                continue

            if entry.richness() > existing.richness():
                winner, loser = entry, existing
            else:
                winner, loser = existing, entry

            # Fill gaps from the loser rather than discarding it outright.
            winner.article = winner.article or loser.article
            winner.plural = winner.plural or loser.plural
            winner.ipa = winner.ipa or loser.ipa
            winner.pos = winner.pos or loser.pos
            if winner.freq_rank is None:
                winner.freq_rank = loser.freq_rank
            elif loser.freq_rank is not None:
                winner.freq_rank = min(winner.freq_rank, loser.freq_rank)

            seen = {s[0] for s in winner.sentences}
            winner.sentences.extend(s for s in loser.sentences if s[0] not in seen)
            winner.note_ids.extend(loser.note_ids)
            merged[entry.lemma] = winner
    return merged


# ------------------------------------------------------------- anki signal


def load_anki_stats(conn, entries: dict[str, Entry]) -> dict[str, dict]:
    """Per-lemma difficulty from cards/revlog.

    Empty-ish until the collection has genuine review history — every consumer
    must cope with that. Where a lemma maps to several notes (and each note to
    several cards) we keep the worst case: most lapses, lowest ease.
    """
    note_to_lemma: dict[int, str] = {}
    for lemma, entry in entries.items():
        for nid in entry.note_ids:
            note_to_lemma[nid] = lemma

    stats: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT nid, lapses, reps, factor, ivl, mod FROM cards WHERE reps > 0"
    )
    for row in rows:
        lemma = note_to_lemma.get(row["nid"])
        if lemma is None:
            continue
        cur = stats.setdefault(
            lemma,
            {"lapses": 0, "reps": 0, "ease": None, "ivl": 0, "last_review": 0},
        )
        cur["lapses"] = max(cur["lapses"], row["lapses"] or 0)
        cur["reps"] += row["reps"] or 0
        if row["factor"]:
            cur["ease"] = min(cur["ease"] or row["factor"], row["factor"])
        cur["ivl"] = max(cur["ivl"], row["ivl"] or 0)
        cur["last_review"] = max(cur["last_review"], row["mod"] or 0)
    return stats


# -------------------------------------------------------------------- write


def import_collection(db_conn, collection_path: Path = DEFAULT_COLLECTION) -> dict:
    """Parse the collection and replace the Anki-derived tables.

    App-owned tables (exposures, schedule, clips) are deliberately untouched.
    """
    from . import db as dbmod

    anki, tmpdir = open_collection(collection_path)
    try:
        available = _notetypes(anki)
        groups: list[list[Entry]] = []

        if NT_ENGLISH_DEUTSCH in available:
            groups.append(parse_english_deutsch(anki, available[NT_ENGLISH_DEUTSCH]))
        if NT_B1 in available:
            groups.append(parse_b1(anki, available[NT_B1]))
        for name in (NT_NERI_FE, NT_NERI_EF):
            if name in available:
                groups.append(parse_neri(anki, available[name], "Neri's Frequent Words"))
        if NT_VOKABELN in available:
            groups.append(parse_vokabeln(anki, available[NT_VOKABELN]))

        if not groups:
            raise RuntimeError(
                "No recognised German notetypes found in the collection. "
                f"Available: {sorted(available)}"
            )

        merged = merge(groups)
        anki_stats = load_anki_stats(anki, merged)
        sync_info = collection_sync_info(anki)
    finally:
        anki.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    with db_conn:
        db_conn.execute("DELETE FROM sentences")
        db_conn.execute("DELETE FROM anki_stats")
        db_conn.execute("DELETE FROM vocab")

        db_conn.executemany(
            "INSERT INTO vocab(lemma, display, article, plural, ipa, pos, gloss,"
            " curated_rank, source, has_sentence) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    e.lemma, e.display, e.article, e.plural, e.ipa, e.pos,
                    e.gloss, e.freq_rank, e.source, 1 if e.sentences else 0,
                )
                for e in merged.values()
            ],
        )
        db_conn.executemany(
            "INSERT INTO sentences(lemma, de, en, source) VALUES(?,?,?,?)",
            [
                (e.lemma, de, en, e.source)
                for e in merged.values()
                for de, en in e.sentences
            ],
        )
        db_conn.executemany(
            "INSERT INTO anki_stats(lemma, lapses, reps, ease, ivl, last_review)"
            " VALUES(?,?,?,?,?,?)",
            [
                (lemma, s["lapses"], s["reps"], s["ease"], s["ivl"], s["last_review"])
                for lemma, s in anki_stats.items()
                if lemma in merged
            ],
        )
        dbmod.set_meta(db_conn, "last_import", str(int(time.time())))
        dbmod.set_meta(db_conn, "anki_last_sync", str(sync_info["last_sync"] or 0))

    # Curated ranks cover only ~5.5k of the words; fill in the rest from corpus
    # frequency so lesson ordering is meaningful across the whole collection.
    from . import frequency

    freq_info = frequency.assign_ranks(db_conn)

    result = dbmod.stats(db_conn)
    result["sync"] = sync_info
    result["frequency"] = freq_info
    return result
