"""Judge whether the Anki collection carries a usable difficulty signal.

The trainer would rather weight lessons toward words you actually struggle
with. That requires review history, and history can be missing for reasons the
raw numbers do not distinguish:

  * the desktop collection was never synced;
  * it synced, but the reviews live on a phone that has not pushed yet;
  * the decks genuinely have not been studied.

`col.ls` (Anki's own last-sync timestamp) separates the first case from the
others — a recent sync with no history means the data is elsewhere, not stale
locally. Getting this wrong is expensive: it silently degrades every lesson
ordering with no visible symptom, so it is reported explicitly instead.
"""

import time

# Below this many reviews there is nothing to learn a difficulty signal from.
MIN_USEFUL_REVIEWS = 100

# A sync older than this is worth mentioning.
STALE_SYNC_DAYS = 3


def assess(sync_info: dict) -> dict:
    """Classify collection freshness into a state plus a human explanation."""
    now = time.time()
    last_sync = sync_info.get("last_sync")
    reviews = sync_info.get("revlog_count") or 0
    lapsed = sync_info.get("cards_lapsed") or 0
    reviewed = sync_info.get("cards_reviewed") or 0

    sync_age_days = (now - last_sync) / 86400 if last_sync else None

    if reviews >= MIN_USEFUL_REVIEWS and lapsed > 0:
        state = "ok"
        message = (
            f"{reviews:,} reviews, {lapsed:,} lapsed cards — "
            "lessons will be weighted toward words you find hard."
        )
    elif reviews >= MIN_USEFUL_REVIEWS:
        state = "no_lapses"
        message = (
            f"{reviews:,} reviews but no lapses yet — nothing has been forgotten, "
            "so ordering falls back to word frequency."
        )
    elif last_sync and sync_age_days is not None and sync_age_days < STALE_SYNC_DAYS:
        state = "synced_but_empty"
        message = (
            f"Synced {_ago(sync_age_days)} but only {reviews} reviews came down "
            f"({reviewed} cards studied). If you review on a phone, it has not "
            "pushed to AnkiWeb yet — sync there, then re-run import-anki."
        )
    elif last_sync:
        state = "stale"
        message = (
            f"Last synced {_ago(sync_age_days)}, only {reviews} reviews locally. "
            "Sync Anki desktop, then re-run import-anki."
        )
    else:
        state = "never_synced"
        message = (
            f"This collection has never been synced and holds {reviews} reviews. "
            "Ordering falls back to word frequency."
        )

    return {
        "state": state,
        "message": message,
        "usable_signal": state == "ok",
        "reviews": reviews,
        "lapsed": lapsed,
        "sync_age_days": sync_age_days,
    }


def _ago(days: float | None) -> str:
    if days is None:
        return "never"
    if days < 1 / 24:
        return "minutes ago"
    if days < 1:
        return f"{int(days * 24)}h ago"
    return f"{int(days)}d ago"


def describe(sync_info: dict) -> str:
    """One-line status suitable for printing after an import."""
    verdict = assess(sync_info)
    icon = {"ok": "✓"}.get(verdict["state"], "!")
    return f"{icon} Anki: {verdict['message']}"
