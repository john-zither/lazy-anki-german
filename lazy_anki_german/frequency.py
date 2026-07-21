"""Assign every word a usable frequency rank.

Only two of the four Anki sources carry frequency information — English-Deutsch
(`Rank`) and Neri (`Index`), together about 5,500 words. The other ~76,000 have
nothing. B1_Wortliste looks like it does, but `original_order` is alphabetical
("ab"=1, "abbiegen"=2), so using it as a rank would order lessons by spelling.

`wordfreq` supplies a Zipf score for any German word from a blend of corpora
including subtitles, which suits spoken vocabulary better than an encyclopedic
corpus would. Measured against this collection's 4,968 curated ranks it has
100% coverage and Spearman rho 0.71 — genuinely correlated, not identical
(different source corpora), so the curated ranks are kept as a tiebreaker
rather than thrown away.

Zipf is a log scale: 7 is "der", 4 is a common everyday word, 2 is rare, 0
means the corpora never saw it. About 23% of this collection scores 0 — mostly
obscure compounds like "nachfüllend". Those genuinely are rare, so sorting them
last is the right outcome, not a failure.
"""

from wordfreq import zipf_frequency

LANG = "de"

# Anything at or below this is treated as "corpus never saw it".
UNKNOWN_ZIPF = 0.0

# Penalty in Zipf points per extra token in a multiword entry. wordfreq scores
# phrases far too generously for our purposes — it rates "und ich" at 6.92,
# above "Haus" at 5.41 — which floated 258 phrases into the top 500 and would
# have opened the trainer on function-word patterns instead of vocabulary. A
# phrase can be no more frequent than its rarest component, discounted for
# having to co-occur, so that is what we model.
PHRASE_PENALTY = 1.2


def zipf_for(lemma: str) -> float:
    """Zipf frequency for a lemma, 0.0 when unknown.

    wordfreq lowercases internally, so the lemma having lost German noun
    capitalisation during normalisation does not matter here.
    """
    if not lemma:
        return UNKNOWN_ZIPF
    try:
        tokens = lemma.split()
        if len(tokens) <= 1:
            return zipf_frequency(lemma, LANG)

        # Phrase: bounded by its rarest component, then discounted per extra token.
        parts = [zipf_frequency(t, LANG) for t in tokens]
        if any(p <= UNKNOWN_ZIPF for p in parts):
            return UNKNOWN_ZIPF
        return max(UNKNOWN_ZIPF, min(parts) - PHRASE_PENALTY * (len(tokens) - 1))
    except Exception:
        return UNKNOWN_ZIPF


def assign_ranks(conn) -> dict:
    """Compute `zipf` and a unified `freq_rank` for every row in vocab.

    freq_rank is 1-based, 1 being the most frequent. Ordering is by Zipf
    descending, with the curated rank breaking ties; unknown-Zipf words sort
    last, ordered among themselves by curated rank where they have one.
    """
    rows = conn.execute("SELECT lemma, curated_rank FROM vocab").fetchall()

    scored = []
    for row in rows:
        z = zipf_for(row["lemma"])
        curated = row["curated_rank"]
        scored.append((row["lemma"], z, curated))

    # Known-frequency words first (Zipf desc), then the unknown tail.
    scored.sort(
        key=lambda t: (
            t[1] <= UNKNOWN_ZIPF,                  # unknowns last
            -t[1],                                 # higher Zipf first
            t[2] if t[2] is not None else 10**9,   # curated rank breaks ties
        )
    )

    with conn:
        conn.executemany(
            "UPDATE vocab SET zipf = ?, freq_rank = ? WHERE lemma = ?",
            [(z, rank, lemma) for rank, (lemma, z, _) in enumerate(scored, start=1)],
        )

    known = sum(1 for _, z, _ in scored if z > UNKNOWN_ZIPF)
    phrases_top500 = sum(1 for lemma, _, _ in scored[:500] if " " in lemma)
    return {
        "total": len(scored),
        "with_zipf": known,
        "unknown": len(scored) - known,
        "coverage": known / len(scored) if scored else 0.0,
        "phrases_in_top500": phrases_top500,
    }
