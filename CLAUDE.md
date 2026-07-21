# lazy-anki-german

German vocabulary trainer. Reads an Anki collection, speaks each word with a
cloned German voice, and leaves a pause to recall it before giving the answer.

Python + `uv`. State lives in `data/vokabel.db` (SQLite); synthesised audio in
`audio/cache/`. Neither is committed — both derive from a personal collection.

## Setup

```bash
uv sync
docker compose -f ../docker-compose.yml up -d alltalk   # service is `alltalk`
uv run main.py setup-voice
uv run main.py import-anki
```

## Running

```bash
uv run main.py import-anki                  # Anki -> local DB
uv run main.py setup-voice                  # build + install the German voice
uv run main.py setup-voice --compare        # render a voice A/B to listen to
uv run main.py prefetch --limit 300         # synthesise ahead of time
uv run main.py play --minutes 20            # passive session
uv run main.py play --minutes 10 --quiz     # grade yourself as you go
uv run main.py export --minutes 30 -o s.mp3 # for a phone
uv run main.py stats
```

Lesson format flags, valid on `play`, `prefetch` and `export`:

| Flag | Effect |
|---|---|
| `--min-rank N` | Skip words more frequent than rank N (default 150) |
| `--naive-format` | Word, meaning, sentence — no recall pause |
| `--shadow` | Add a pause to repeat each word aloud |

## Architecture

```
anki_import.py   Anki collection -> vocab/sentences/anki_stats
frequency.py     wordfreq Zipf scores -> unified freq_rank
freshness.py     is there a usable difficulty signal?
db.py            schema; Anki-derived vs app-owned tables
tts.py           AllTalk client + permanent content-addressed cache
voice_setup.py   fetch/clean/install reference voices
scheduler.py     what to play, and when to replay it
player.py        lesson assembly, playback, MP3 export
```

## Things that will bite you

**Copy the WAL sidecars.** Anki runs in WAL mode and holds the collection open.
Copying `collection.anki2` alone gives a stale pre-checkpoint snapshot missing
every recent review — silently, with no error.

**Register a `unicase` collation** before querying `notetypes.name` or
`decks.name`, or SQLite raises `OperationalError`.

**Case matters in lemma keys.** German capitalises nouns; lowercasing merges
`das Leben` with `leben` and `das Gut` with `gut`.

**AllTalk reads `/app/voices`**, not the `/app/alltalk/models/xtts/voices` path
the shared `docker-compose.yml` mounts. That mount does nothing. Voices are
installed with `docker cp` instead of repointing a mount `storysearch` and
`ragnas` also use.

**Reference voices must be denoised.** Raw LibriVox audio makes XTTS ramble —
11.6s of speech for "das Haus". The highpass + `afftdn` + `loudnorm` chain in
`voice_setup.FILTER_CHAIN` is what makes cloning stable.

**Match the audio format when concatenating.** XTTS emits `pcm_f32le` at 24kHz.
ffmpeg's concat demuxer needs identical stream parameters across inputs and
silently drops audio otherwise — this quietly cost 18% of every exported
session.

**AllTalk writes output as root.** Copy clips out of `alltalk_storage`; do not
try to move them.

## Verifying changes

```bash
uv run main.py import-anki      # ~82k words, ~5.8k with sentences
uv run main.py stats
uv run main.py export --minutes 5 -o /tmp/t.mp3
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/t.mp3   # ~300s
```

An export that comes out materially short of the requested length means the
session is failing to fill — check word count, timeline durations and the
concat sample format, in that order. Sessions must also work with the AllTalk
container stopped, entirely from cache.
