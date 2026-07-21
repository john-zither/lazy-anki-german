"""Choosing what to play, and when to play it again.

Two scheduling problems, at different timescales:

*Across* sessions, an SM-2 variant over the app's own quiz history decides what
is due. Anki's lapse and ease data seeds the initial ordering when it exists;
right now this collection has reviews but no lapses, so ordering falls back to
corpus frequency. That fallback is the normal case, not a failure mode.

*Within* a session, each word is replayed at expanding intervals — roughly +2,
+10 and +40 minutes — rather than back to back. Massed repetition inside one
sitting feels productive and produces very little durable memory; spacing the
same number of repeats is markedly better. This is the single most valuable
thing the scheduler does, and it is why sessions are assembled as a timeline
rather than a shuffled list.
"""

import time

# Expanding within-session replays, in seconds from first exposure.
REPEAT_OFFSETS = (120, 600, 2400)

# Share of a session given to words never seen before. Kept low so most of what
# you hear is already familiar and the material stays comprehensible.
NEW_RATIO = 0.2

# Skip the highest-frequency band by default. Ranks below this are dominated by
# articles, pronouns, prepositions and auxiliaries — already known, and not
# words that passive listening can usefully reinforce.
MIN_RANK = 150

MIN_EASE = 1.3
DAY = 86400


# ------------------------------------------------------------------ selection


def _priority_clause(has_anki_signal: bool) -> str:
    """Ordering for review items.

    With real Anki history, words you have forgotten most come first. Without
    it, frequency is the only meaningful signal available.
    """
    if has_anki_signal:
        return """
            ORDER BY
                COALESCE(a.lapses, 0) DESC,
                COALESCE(a.ease, 2500) ASC,
                COALESCE(v.freq_rank, 999999) ASC
        """
    return "ORDER BY COALESCE(v.freq_rank, 999999) ASC"


def pick_items(
    conn,
    count: int,
    require_sentence: bool = True,
    has_anki_signal: bool = False,
    new_ratio: float = NEW_RATIO,
    min_rank: int = MIN_RANK,
) -> list[dict]:
    """Choose `count` distinct words: mostly due reviews, some new.

    `min_rank` skips the very top of the frequency list. Strict frequency order
    opens a lesson with "die, der, und, in, das" — function words any learner
    past their first week already knows, and which no amount of audio exposure
    improves.
    """
    now = int(time.time())
    sentence_filter = "AND v.has_sentence = 1" if require_sentence else ""
    sentence_filter += f" AND COALESCE(v.freq_rank, 999999) >= {int(min_rank)}"

    n_new = max(1, int(count * new_ratio))
    n_review = count - n_new

    due = conn.execute(
        f"""
        SELECT v.*, s.ease, s.interval_days, s.streak
        FROM vocab v
        JOIN schedule s ON s.lemma = v.lemma
        LEFT JOIN anki_stats a ON a.lemma = v.lemma
        WHERE s.due_ts <= ? {sentence_filter}
        {_priority_clause(has_anki_signal)}
        LIMIT ?
        """,
        (now, n_review),
    ).fetchall()

    # Backfill with unseen words if there are not enough reviews due — which is
    # always the case on a first run.
    shortfall = count - len(due)
    fresh = conn.execute(
        f"""
        SELECT v.*, NULL AS ease, NULL AS interval_days, NULL AS streak
        FROM vocab v
        LEFT JOIN schedule s ON s.lemma = v.lemma
        LEFT JOIN anki_stats a ON a.lemma = v.lemma
        WHERE s.lemma IS NULL {sentence_filter}
        {_priority_clause(has_anki_signal)}
        LIMIT ?
        """,
        (shortfall,),
    ).fetchall()

    items = [dict(r) for r in due] + [dict(r) for r in fresh]
    for item in items:
        item["sentence"] = best_sentence(conn, item["lemma"])
    return items


# A sentence has to actually demonstrate usage. Picking the shortest available
# yields things like "Die Tür." — grammatical, but it teaches nothing about how
# the word behaves. Picking the longest buries the target word in clauses. Aim
# for a middle band and take whatever sits closest to the centre.
IDEAL_SENTENCE_CHARS = 55


def best_sentence(conn, lemma: str) -> dict | None:
    """Pick the example sentence closest to a useful demonstration length."""
    row = conn.execute(
        """
        SELECT de, en FROM sentences
        WHERE lemma = ? AND de IS NOT NULL AND LENGTH(de) >= 18
        ORDER BY ABS(LENGTH(de) - ?) ASC
        LIMIT 1
        """,
        (lemma, IDEAL_SENTENCE_CHARS),
    ).fetchone()
    if row is None:
        # Fall back to anything at all rather than dropping the word.
        row = conn.execute(
            "SELECT de, en FROM sentences WHERE lemma = ? AND de != '' LIMIT 1",
            (lemma,),
        ).fetchone()
    return {"de": row["de"], "en": row["en"]} if row else None


# ------------------------------------------------------------------ timeline


def build_timeline(items: list[dict], item_seconds: float, total_seconds: float) -> list[dict]:
    """Lay items out over a session, interleaving expanding replays.

    Returns a list of {item, repeat_index}. A word's replays are emitted when
    the session clock passes their target offset, so spacing is real elapsed
    time rather than a fixed number of intervening cards.
    """
    timeline: list[dict] = []
    pending: list[tuple[float, dict, int]] = []  # (due_at, item, repeat_index)
    clock = 0.0
    queue = list(items)

    last_lemma = None

    while clock < total_seconds and (queue or pending):
        pending.sort(key=lambda p: p[0])

        # Never play the same word twice in a row: back-to-back repetition is
        # exactly the massed practice the expanding schedule exists to avoid.
        ready = [i for i, p in enumerate(pending) if p[0] <= clock]
        choice = next(
            (i for i in ready if pending[i][1]["lemma"] != last_lemma), None
        )

        if choice is not None:
            _, item, repeat_index = pending.pop(choice)
        elif queue and queue[0]["lemma"] != last_lemma:
            item, repeat_index = queue.pop(0), 0
        elif len(queue) > 1:
            item, repeat_index = queue.pop(1), 0
        elif pending:
            # Nothing new left; idle forward to the next replay.
            index = next(
                (i for i, p in enumerate(pending) if p[1]["lemma"] != last_lemma), 0
            )
            due_at, item, repeat_index = pending.pop(index)
            clock = max(clock, due_at)
        elif queue:
            item, repeat_index = queue.pop(0), 0
        else:
            break

        last_lemma = item["lemma"]

        timeline.append({"item": item, "repeat": repeat_index})
        clock += item_seconds

        if repeat_index < len(REPEAT_OFFSETS):
            pending.append((clock + REPEAT_OFFSETS[repeat_index], item, repeat_index + 1))

    return timeline


# ---------------------------------------------------------------- sm-2 lite


def grade(conn, lemma: str, got_it: bool) -> dict:
    """Update a word's schedule after a quiz answer."""
    now = int(time.time())
    row = conn.execute("SELECT * FROM schedule WHERE lemma = ?", (lemma,)).fetchone()

    ease = row["ease"] if row else 2.5
    interval = row["interval_days"] if row else 0.0
    streak = row["streak"] if row else 0
    seen = (row["seen_count"] if row else 0) + 1

    if got_it:
        streak += 1
        ease = min(3.0, ease + 0.1)
        # First two successes use fixed short intervals; SM-2 growth after.
        interval = 1.0 if streak == 1 else 3.0 if streak == 2 else interval * ease
    else:
        streak = 0
        ease = max(MIN_EASE, ease - 0.2)
        # Due again within the hour rather than tomorrow — a missed word should
        # come back inside the same listening habit, not a day later.
        interval = 0.02

    due_ts = now + int(interval * DAY)
    conn.execute(
        """
        INSERT INTO schedule(lemma, ease, interval_days, due_ts, streak, seen_count)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(lemma) DO UPDATE SET
            ease = excluded.ease,
            interval_days = excluded.interval_days,
            due_ts = excluded.due_ts,
            streak = excluded.streak,
            seen_count = excluded.seen_count
        """,
        (lemma, ease, interval, due_ts, streak, seen),
    )
    conn.commit()
    return {"lemma": lemma, "ease": ease, "interval_days": interval, "due_ts": due_ts}


def touch(conn, lemma: str) -> None:
    """Register a passive exposure.

    Passive listening carries no grade, so it must not drive intervals the way
    a quiz answer does — but a word heard several times should not keep being
    offered as brand new either. It gets a short interval only on first contact.
    """
    row = conn.execute("SELECT lemma FROM schedule WHERE lemma = ?", (lemma,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schedule(lemma, ease, interval_days, due_ts, streak, seen_count)"
            " VALUES(?, 2.5, 0.5, ?, 0, 1)",
            (lemma, int(time.time() + 0.5 * DAY)),
        )
    else:
        conn.execute(
            "UPDATE schedule SET seen_count = seen_count + 1 WHERE lemma = ?", (lemma,)
        )
