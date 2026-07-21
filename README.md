# lazy-anki-german

A hands-free German vocabulary trainer. It reads your Anki collection, speaks
each word in a cloned German voice, and gives you a moment to recall it before
telling you what it means.

Built as a *consolidation layer* over Anki and Duolingo, for commutes, walks and
the gym — places where you can listen but not tap.

```
die Verantwortung          German word, with its article
...........                pause — try to recall it
Er übernimmt die           German sentence, so context can do some work
Verantwortung.
...........                pause
responsibility             English meaning, confirming or correcting
He takes responsibility.
```

## Why this order

The obvious arrangement is *word → meaning → sentence*. It reads naturally and
it wastes most of its value, because handing over the English immediately
removes any reason to retrieve. Recognition feels like learning; retrieval is
what actually builds memory.

Four things this does differently, roughly in order of how much they matter:

**A pause after the German word, before any English.** The single biggest
lever, and nearly free. It converts passive listening into a retrieval attempt,
and it still works when you are only half paying attention.

**The German sentence before the English gloss.** Context first gives you a
chance to infer the meaning; the gloss then confirms or corrects. Being told
first removes the attempt entirely.

**Articles, always.** "die Abbildung", never "Abbildung". Gender is very hard to
retrofit onto a noun you already know, and cheap to learn alongside it.

**Expanding replays within a session.** Each word comes back at roughly +2, +10
and +40 minutes rather than three times in a row. Massed repetition inside one
sitting feels productive and retains poorly; the same repeats spaced out do
much better.

Two optional extras: `--shadow` adds a beat to repeat the word aloud, and the
new-word share is kept low so most of what you hear is already familiar.

If you would rather judge for yourself, `--naive-format` plays the conventional
order so you can compare them directly.

### What this is not good at

Passive audio is strong for **consolidating words you have already met** and
weak for **acquiring new ones cold**. It earns its place next to spaced
repetition and lesson apps, not instead of them.

## Where the content comes from

Everything is read out of your own Anki collection — no sentences are generated
by a language model, which would risk teaching confidently wrong German.

| Source | Words | Sentences |
|---|---|---|
| English-Deutsch (by frequency) | 5,009 | up to 3 each |
| B1_Wortliste (Goethe/DTZ) | 2,632 | up to 9 each, with gender and plural |
| Neri's Frequent Words | 1,000 | yes |
| Vokabeln/DeuEng | 81,623 | none — word/gloss only |

Deduplicated: **82,256 words, 5,805 with example sentences, 29,757 sentence
pairs.**

Only ~5,500 of those shipped with a frequency rank, and B1's `original_order`
is alphabetical rather than frequency — so ordering by it would sort lessons by
spelling. Corpus frequency from `wordfreq` fills the gap for every word.

## Struggle-weighting

Given Anki review history, lessons lead with the words you forget most —
highest lapses, lowest ease first. Without it, ordering falls back to word
frequency, which works fine.

`stats` tells you which mode you are in. If you review on a phone, its history
has to reach AnkiWeb *and* the desktop collection before it can be read here.

## Requirements

- AllTalk TTS (XTTSv2) on the GPU, from the parent `docker-compose.yml`
- `ffmpeg` / `ffplay`
- An Anki collection at `~/.local/share/Anki2/User 1/collection.anki2`

Audio is cached permanently, so after the first prefetch sessions start
instantly and run with the TTS container stopped.

See [CLAUDE.md](CLAUDE.md) for commands and architecture.
