# pendant

**A new back door into a discontinued piece of hardware, and a new front door to it as an iPhone app.**

The [Limitless Pendant](https://www.limitless.ai/) is a small wearable that records conversations all day. It is no longer sold, and it was only ever usable through its maker's app and cloud. This project cuts both out:

- **The back door.** The phone talks to the pendant directly over Bluetooth LE, using the pendant's own protocol (documented in [`docs/protocol.md`](docs/protocol.md)). The phone drains the pendant's flash, decrypts the audio and frees the pendant's memory only after the audio is safely stored. A USB path does the same when the pendant is plugged into the server to charge.
- **The front door.** A new iPhone app, "Pendant", pairs your pendants, uploads their audio to a server you own, and shows you what was said:
  - transcripts by day, with playback from any line;
  - summaries and action items;
  - who was speaking, learned from your own corrections;
  - each pendant's battery and memory.

Everything after the pendant runs on hardware you control. Transcription and speaker recognition run on your own computer, and transcripts land as Markdown files in your notes folder (for example an Obsidian vault).

```
pendant ──BLE──▶ iPhone app ──HTTPS over your tailnet──▶ your server ──▶ <notes>/Pendant/Raw/YYYY-MM-DD.md
   └──USB (backup while charging)──────────────────────────▶ your server
```

It is one person's working system, published as is. The author wears a pendant all day, and the code is tuned for that: one wearer, a handful of people they talk to often, and a Linux box at home. It is not a product, and setting it up takes some comfort with Xcode, Python and systemd. In this README, **the server** is the Linux machine you run it on.

## What it does

- **iPhone app** (`ios/`):
  - pairs any number of pendants and connects to whichever is nearby;
  - streams audio live and drains the pendant's flash;
  - saves everything on the phone, frees the pendant's flash only after the save is on disk, and uploads in the background;
  - shows today's conversations, past days, search, the people you talk to, and each pendant's status, with a Live Activity and widget.
- **Server** (`pendant_direct/`, `server/`), four small services on one Linux machine:
  - `pendant-ingest`: receives the phone's uploads (`/pendant/ingest`);
  - `pendant-process` (every 5 minutes):
    - dedupes pages and splits the audio into conversations on 60 s pauses;
    - transcribes locally with faster-whisper (large-v3-turbo);
    - labels speakers with local ECAPA voiceprints;
    - writes each day's transcript to the notes folder;
  - `pendant-feed`: the JSON API the app reads and writes (`/pendant/feed`);
  - `pendant-usb`: drains a pendant plugged into the server, then wipes it.

## Privacy model

- **Stays on your machines:** the audio, the transcripts and the voiceprints. Audio moves pendant → phone → your server over your tailnet (the setup uses Tailscale), and the server is reachable only there.
- **No cloud speech-to-text:** transcription and speaker recognition run on the server's CPU.
- **What leaves, and only if you configure it:**
  - **Summaries.** Titles, summaries and action items come from an LLM API (DeepSeek on Fireworks), which receives the transcript text. The key comes from `FIREWORKS_API_KEY`, else from OpenCode's `~/.local/share/opencode/auth.json` if you use OpenCode with Fireworks. With neither, or with `"summarizer": "heuristic"`, a local heuristic writes plain titles and nothing is sent.
  - **Optional judgments.** Action items are checked with the TypeSafe API, which receives the transcript text, when `TYPESAFE_API_KEY` is set (in the environment or `~/.config/pendant-direct/.env`); without one they are skipped and nothing is sent.
  - **Optional integrations.** An optional people, calendar and task service (the author's own LifeOS API) is reached on localhost or the tailnet and skipped when absent. Low-battery and full-memory alerts run a notifier script you provide (`notify_script` in `config.json` or `PENDANT_NOTIFY_SCRIPT`, called with the message); without it they are skipped.
- **Voiceprints need consent:** they are made only for names listed in `speakers.allowed`.
- **Retention:** audio is kept for 7 days by default (starred conversations forever, and the speech of each voice you name by hand); transcripts stay in your notes.
- **No telemetry, no accounts.** The app talks only to your server.

## Personal setup: what you change

Everything specific to one person lives outside the repo:

| Where | What |
|---|---|
| `~/pendant-direct-data/config.json` | `notes_dir` (where daily notes go; default `~/Pendant/Raw`), `speakers.wearer` and `speakers.wearer_emails` (you, and your calendar addresses), `speakers.allowed` (who may get a voiceprint), `lifeos_url`, `summarizer`, retention |
| `~/pendant-direct-data/pendants.json` | Friendly names for your pendants: `{"<16-hex device ID>": "old"}` |
| `~/.config/pendant-direct/token` | The shared secret between app and server |
| `FIREWORKS_API_KEY`, `TYPESAFE_API_KEY` | Optional model APIs (environment of the services) |
| `ios/Secrets.xcconfig` (from `Secrets.xcconfig.example`) | Server URL and token, Apple team ID, the server's display name, Obsidian vault name, pendant names |
| `PENDANT_BUNDLE_ID` in `ios/Secrets.xcconfig` | Your bundle ID; the widget, UI tests and app group derive from it |
| Code constants | Time zone (`America/New_York` in the server modules), and LifeOS, calendar and Telegram integrations that assume the author's setup |

## Status

This tracks the author's own use. Interfaces change without notice, and there are no releases. Issues and pull requests are welcome, but there's no promise of support.

## How it works

### Names, summaries, highlights, speakers

- **Pendant names**: `~/pendant-direct-data/pendants.json` (`{"HEX": "name"}`, e.g. `old`
  and `new`; `pendant_direct/names.py`); a pendant without a name shows its ID. Raw headings read `## HH:MM — <Title> (XmYYs) · <name>`.
- **Tiny recordings** (`process.merge_tiny`): before transcription, a conversation under `grouping.tiny_audio_s`
  (20 s) of audio joins the nearest untranscribed conversation on the same pendant whose gap is at most
  `grouping.tiny_join_gap_s` (120 s; the preceding one when both are close). The merged audio keeps its real gap, stars
  inside it go with it, and the merged conversation must stay within 30 min. It never joins across audio already
  transcribed or held back, a waiting or added enrollment window, or a deleted recording's span (range and page
  tombstones). A tiny conversation with no such neighbour is transcribed as usual and stored with kind `fragment`,
  importance 0 and reason `tiny`. Conversations under 2 s (blips) are left as they were. `tiny_audio_s` is capped at 60 s
  and 0 turns the rule off; any failure in the pass falls back to the plain 60 s grouping. The thresholds sit in the
  gaps of the Nov 8 2026 distribution: no conversation lasted 19-25 s, and conversation gaps clustered at 60-140 s.
  `PENDANT_DATA=<dir> python -m pendant_direct.process --plan-day YYYY-MM-DD` is a read-only dry run printing counts
  for that local day (chunks with audio only, as live grouping sees them): conversations without and with the rule, tiny ones merged and flagged, summary calls saved, and
  merged conversations of 60 s or more (always 0). `PENDANT_DATA` moves the processor's data directory.
- **Day signals** (`day_signals.py`): one quiet row per New York day in `pages.db` `day_signals`
  (`day`, `signals` JSON, `computed_at`, `source_key`) for Dailies suggestions: `emotional_moment`
  (null unless a moment was unmistakably emotional for the wearer himself, positive or negative; a path on
  the vendored Dailies feelings wheel `dailies_wheel.json`, invalid paths dropped), `resonant` and up to
  3 `words`. DeepSeek on Fireworks reads the day's `kind=conversation` transcripts (at most 30,000
  characters, higher importance and starred first); only today and yesterday are computed, after each
  processing run, and again only when the day's conversation set changes, at most once every 30
  minutes per day. Failures log a category and
  never block transcription. `GET /pendant/feed/day-signals?day=YYYY-MM-DD` serves it (feed auth); the
  app shows it nowhere. Bump `day_signals.PROMPT_VERSION` when its prompt or schema changes.
- **Summaries** (`summarize.py`): each conversation gets a title, overview, action items
  and category from DeepSeek on Fireworks (key from `FIREWORKS_API_KEY` or OpenCode's
  `auth.json`). Transcripts are never sent to Claude. If DeepSeek is unavailable the local
  `heuristic` (opening words) is used; `"summarizer": "heuristic"` forces it.
  DeepSeek results are cached in `pages.db` (`summaries`, keyed by sha256 of model, `PROMPT_VERSION` and
  transcript text), so identical text, including reprocessing and `backfill_kind`, never pays twice; bump
  `PROMPT_VERSION` whenever `SYSTEM` or `SCHEMA` changes (a test pins both). Heuristic fallbacks are not
  cached. Every Fireworks call appends token counts (prompt, completion, reasoning, cached) to `llm_usage`;
  `/status` shows `llm_today` and `llm_7d` (calls, tokens, estimated USD from `config.json`'s `llm`
  rates, default $0.30 in / $1.20 out per million tokens). The Jev action-item filter is not cached or logged.
- **Kind and importance** (`classify.py`): each conversation is a `conversation`, `media` (TV, movie,
  podcast or other playing audio) or `fragment` (too little to keep), with importance 0 routine, 1 worth a
  look, 2 important. DeepSeek proposes both in the summary call (the heuristic fallback proposes nothing);
  `classify(summary, signals)` decides, with reasons stored as JSON in `conversations.kind_reasons` next to
  `kind` and `importance`. Signals: words, words per minute, the share of whisper segments dropped as low
  confidence, and the wearer's labelled share of speech from the speaker clusters. Unlabelled speech is
  unknown, not absent: the wearer is absent only when at least 80% of speech is labelled as someone else, or
  he has at most 10% and every unlabelled voice scores under 0.4 against his voiceprint (`voices.score`).
  A real conversation is never dimmed, so when signals disagree or are unknown it is one.
  Fragment: under 25 words, but only if the model also says fragment or it has importance 0 and no action
  items; or over 40% low-confidence segments; or under 5 words/min over at least 2 min, but only if the model
  didn't say conversation or the wearer is absent; or the model says fragment and under 80 words. Media: the
  model says media, the wearer is absent and speaks at most 10% (without a model opinion: also at least
  150 words at 150+ words/min).
  The wearer speaking over 25% always makes it a conversation. Media and fragments get importance 0.
  Thresholds are in `config.json` under `classify`. Any failure writes a conversation with importance 0.
  The day feed gives `kind`, `importance` and `kind_reasons` (rows from before: conversation, 0, []). The
  app dims media and fragment rows with a "Media" / "Fragment" badge and folds the unstarred ones of
  each day's feed into one slim "N hidden · Show" line at the latest one's place; tapping it lists them grouped
  under it, with a left accent bar (per day, until relaunch), and "Hide" folds them again. They swipe like any
  other row (swipe left deletes, see below); nothing is deleted automatically. Starred and notable ones never
  fold, and search ignores the fold (`Core/JunkFold.swift`). Delete and restore carry `kind`, `importance` and
  `kind_reasons` with the rest of the conversation row. Importance 2 makes a conversation notable.
  `.venv/bin/python -m pendant_direct.backfill_kind [--data DIR] [--notes DIR] [--all] [--dry-run]
  [--no-vault] [--table]` classifies conversations without a kind from their Raw sections (transcripts go to
  DeepSeek like a new summary; titles stay), committing each row as it goes. Then, unless `--no-vault`, a
  separate step with no model calls makes importance-2 conversations notable (honoring
  `notable_overrides.json`, skipping ones with a meeting note), writes their Event notes and rewrites those
  days' Highlights; rerun it to repair a failed write.
- **Tasks** (`tasks.py`): a LifeOS task (`POST localhost:8000/api/tasks`, source `pendant`,
  notes linking `[[YYYY-MM-DD#<Raw heading>]]`) exists only when the wearer asks. In the app each
  action item has an "Add to tasks" button (`POST /pendant/feed/tasks` with `device_id`,
  `start_ms`, `item`; the day feed lists each conversation's `tasked` items). Out loud, a
  phrase pass (to-do, task list, my list, add that, remind me) over the paragraphs labeled as the wearer
  (unlabeled conversations yield no spoken tasks) cuts the task text from the
  sentence, and Jev must answer >= 0.8 to "did the wearer explicitly ask to add something
  to his task list"; the Raw section then gets `Added to tasks: <item>`. Requests live in
  `task_requests` (unique per conversation and item, so no duplicates); while LifeOS is
  down they stay `pending` and the processor retries them each run, along with a scan of the
  last 7 days of conversations not yet judged (`task_scans`). Without a TypeSafe key no
  spoken tasks are created.
- **Events and Highlights** (`highlights.py`): notable conversations get an Event note and show a star.
  A conversation is notable when its importance is 2, it has a button or app star (`notable_overrides.json`,
  `{"<HEX>-<start_ms>": true}`), or it matches an optional `notable` rule (`keywords`, `duration_minutes`,
  `action_items_min`; length and action items are off by default, `null`). Notable conversations get `<vault>/Pendant/Events/<YYYY-MM-DDTHHMM00> - <slug> -
  <HEX>-<start_ms>.md`. `<vault>/Pendant/Highlights/YYYY-MM-DD Highlights.md` is rebuilt
  from the `conversations` table after every run that wrote to that day.
- **Stars**: a short press of a pendant's button stars that moment (long and double
  presses and the reset gesture are ignored). Presses are stored in the `stars` table at
  ingest, keyed on the press's own clock so live and drained copies count once (segments
  ingested before stars existed are rescanned once). The paragraph covering the press, in
  a conversation from the pendant that was pressed, gets `⭐ ` after its `[HH:MM] ` stamp, the conversation
  becomes notable (a linked meeting note doesn't change that), and the day's Highlights
  list it under `## Starred Moments`. A press that arrives after its conversation was
  written, or whose section isn't in the Raw file, is placed on a later run. The feed gives each conversation's `stars` (`HH:MM`);
  the app shows a star badge on the timeline and highlights starred paragraphs.
- **Feed requests and errors** (`Feed.swift`, `Core/FeedClient.swift`): every feed request goes through one retry layer.
  Offline, timeouts and 5xx answers (502/503/504 during a deploy or restart) are retried 3 times after 0.5 s, 1.5 s
  and 4 s; 4xx answers and undecodable replies are final. A final failure is classified (`FeedProblem`) and shown
  with its real cause: "No connection to the server (check Tailscale)", "the server is busy, retrying…" or
  "Couldn't read the server's reply (app needs an update?)", and written to the app log as
  `feed <route> failed: <kind> <status> <error>` (search words never reach the log). A screen showing an error asks
  again when the app returns to the foreground and every 10 s while it stays.
- **Search-only Today**: while the search field holds text, the Today and day lists show only the matching
  conversations and an "N matches" count; the live header, stats, chart, Who's this card and review filter return
  when it is cleared.
- **Swipe to star or delete** (home feed: Today and History days, a native `List` with `.swipeActions` so the
  system keeps scrolling and swiping apart; the header, chart and Who's this card are rows of the same list):
  swiping right (full swipe) stars a conversation
  (`POST /pendant/feed/conversations/<device>/<start_ms>/star` `{"on": true}`, a `true` entry in
  `notable_overrides.json`, so it gets an Event note and a Highlights line); swiping right again takes that
  app star off (`{"on": false}` removes only a `true` entry; button-press stars stay). The day feed gives
  `app_star`. Swiping left (full swipe) deletes it with no dialog: the row turns into "Deleted · Undo" for 5 s, and Undo
  restores it. `POST .../delete` (`trash.py`) runs under the processor's lock and moves the conversation to
  `~/pendant-direct-data/trash/<device>-<start_ms>/`: its Raw section (cut from the daily note), Event note,
  clip, audio chunk rows (`chunks.db`), and its rows in `conversations`, `speaker_clusters`, `speaker_turns`, `stars`,
  `paragraph_samples`, `task_requests` and `task_scans` (plus its voices' `strangers`, `noise_exemplars` and
  `voice_offers` rows), with every voiceprint sample taken from its voices or lines, all listed in
  `manifest.json`. That day's Highlights are rebuilt. Either all of it moves or nothing does: a failure before
  the database commit puts the note and voiceprints back. After the commit, the live Event note and clip are
  removed, Highlights rebuilt and the manifest marked `finished` before the entry takes its final name. A
  delete or restore cut short by a crash is finished or undone by the next feed request or processor run
  (`trash.recover`). Until that succeeds, the processor skips its runs (uploads keep landing; transcription
  waits) and delete, restore and star answer 503. A trash entry whose manifest can't be read stays where it
  is and blocks the same way until it is repaired by hand (entries no trash row owns are set aside as
  `.<id>.stale-<time>`). `POST .../restore` puts back
  exactly what the manifest holds (the section where it was, else in time order; a cluster id reused meanwhile
  gets a new id); if a newer file already sits at the clip's or Event note's path with different
  content, restore answers 409 and changes nothing; restoring something not in the trash changes nothing. `GET /pendant/feed/trash` lists
  deleted recordings (`deleted_at`, `purge_at`); History's "Recently deleted" shows them with Restore. The
  processor purges entries 7 days after deletion. Tombstones (`tombstones`: device, page, Opus hash and page timestamp;
  `range_tombstones`: the recording's span; `star_tombstones`) outlive the purge, so pages of a deleted
  recording that arrive again from a pendant drain or the USB backup are dropped at ingest. A packet matches
  only within 6 h of the deleted page's timestamp (14 days of its upload when its page clock was invalid), so
  identical packets after a wipe reuses page numbers are kept. When the
  processor is mid-run for more than 30 s the endpoints answer 503 and the app puts the row back.
- **Audio retention** (`retention.py`, off by default): `config.json`'s `retention` (`enabled` false, `days` 7,
  `keep_starred` true). When enabled, the processor runs one pass a day under its lock, after transcription: audio
  older than `days` is dropped unless the recording is starred (a button press, an app star in
  `notable_overrides.json`, or `notable = 1`; starring after the audio went can't bring it back). Each pass first
  brings the kept voices up to date (below), so a hand-named voice's speech is cut from its clip before the clip goes;
  a conversation whose hand-named voices couldn't be kept for a reason that can clear (a queued relabel, an
  unreadable note, a failed cut; every such conversation when the keep itself fails) keeps all its audio, is
  logged and counted in the report, and the next pass tries again. It deletes the
  conversation's clip in `clips/` (and clips no conversation owns) and its playback transcode `audio_cache/<clip stem>.m4a` (orphaned cache files follow the same rule), and sets `chunks.opus` to NULL, keeping every
  chunk row with `opus_sha`, `done` and `ts_ms`, so a page that arrives again is still deduped and never
  re-transcribed. Chunks not yet transcribed, audio in waiting or added enrollment windows, the trash (which keeps
  its own copies), transcripts, summaries, speaker embeddings, voiceprints and the vault are never touched; `pages.db`
  is VACUUMed at most weekly. Nothing without audio can be replayed: Who's this and Voice Scoring leave those
  voices out of the question queue, a line reassign changes only the text, and the clip route 404s.
  `python -m pendant_direct.retention --dry-run [--data DIR] [--now YYYY-MM-DD]` reports per day the clips and chunk
  bytes it would remove and the starred recordings it keeps, opening the database read-only; without `--dry-run`
  it runs only when `enabled` is true. Raw spool segments (`spool/*.pmsg`, every uploaded page) go once they are ingested and all their audio is older than
  the cutoff, unless they overlap a starred recording, an enrollment window or untranscribed audio; `usb/` holds only
  resume state. Dedupe needs only the `chunks` rows and tombstones, so a purged page arriving again is still ignored.
- **Voice library** (`voice_library.py`): `~/pendant-direct-data/voice_library/<Name>/<id>.opus` plus `index.json`, up to
  20 clips of 4-15 s per allowed person (`speakers.allowed`; a LifeOS link is not consent), which retention never deletes (its report lists the clips and MB
  kept per person). Ranked hand tags (Who's this, Voice Scoring, a tapped line or conversation, the tapped voice of a
  bulk confirm), then read-aloud enrollment readings, then auto matches by score margin; a batch prefers different days
  and conversations, then a higher voiced share and longer speech; a full library swaps out its lowest-ranked clip only for
  a higher-ranked one. Never a removed name (a stranger), noise or a `mixed` voice. Clips are added when a voice is named or
  answered (feed), a reading is accepted (`enroll.py`) or a conversation is written (`process.py`); moving a voice away
  from a person removes its clip; trash delete moves a recording's clips into the trash copy and restore puts them
  back. `python -m pendant_direct.voice_library --backfill [--dry-run]` builds it from the clips that exist (readings
  keep no clip, so they join only as they are recorded).
- **Kept voices** (`voice_audio.py`): the whole voice of every voice the wearer names by hand (`labeled_by =
  'user'`, a person in `speakers.allowed`, not `mixed`, not noise) is kept for good, so voiceprints can be built
  from whole voices: `~/pendant-direct-data/voice_audio/<device>_<start_ms>/<Name>.opus` holds every line of that
  conversation's Raw section showing the name, cut from the clip at the line's offsets and joined in time order
  (the speech, not the conversation), and `<Name>.json` holds what re-embedding needs: the lines' spans in the clip
  and in the file, the voices it covers (several hand-named voices of one person in a conversation give one file;
  `other_ids` are voices named another way that show the same tag) and its speech length. The feed brings a
  conversation's files in line after every answer and reassignment, and the processor every run, before
  retention: a file goes as soon as no hand-named voice of that person is left there (a name removed, a voice moved
  to someone else or marked noise; a voice moved to someone else is kept under that person, and its old file goes
  only once the new one is written), and is re-cut when
  the person's lines change. While the note can't be read or a relabel is still queued, files stay as they are;
  after the clip is gone a file can only lose lines (re-cut from itself). Retention never touches them (its report
  gives their count and MB). Deleting a conversation moves its folder into the trash copy: restore brings it back,
  and it goes for good when the trash entry is purged. `python -m pendant_direct.voice_audio --backfill
  [--dry-run]` keeps the hand-named voices whose clips still exist (idempotent; the dry run opens the database
  read-only and reports voices and bytes); without flags it prints what is kept. At about 3.5 kB per second of
  speech, it grows by (hand-named voices per day) x (kept bytes per hand-named voice) x 30 a month.
- **Speakers** (`speakers.py`, local CPU): transcription keeps per-word timestamps. SpeechBrain
  ECAPA embeddings of 1.5 s windows (0.75 s hop, voiced parts only, at most 600) are clustered
  per conversation (average linkage at `cluster_threshold`) and matched against
  `~/pendant-direct-data/voiceprints/`. A voice with under 20 s of speech (or 3% of the talk) folds
  into the nearest voice that has that much. When no voice has it (noisy audio scatters every voice
  into fragments of a few seconds), the voice with the least speech merges into its nearest voice,
  one at a time, until each holds 15% of the talk (or the 20 s), so the fragments don't each become
  a "Speaker X". A voiceprint
  (`<name>.npz`) keeps every sample: one unit embedding per contributing speaker
  cluster, keyed by cluster id, plus an opaque legacy part (files from before per-cluster
  tracking, migrated by the processor; samples in it can't be removed by id), which scores
  as one sample (its mean) until replaced. **Scoring** is chosen by `speakers.scoring`: `"mean"` (the default) is the cosine to the mean
  of the samples against `match_threshold`; `"topk"` is the mean of the `min(3, samples)` closest
  samples' cosines against `match_threshold`; `"calibrated"` (`calibration.py`) adds adaptive
  s-norm against a cohort (the 10 strongest cohort cosines of the voice and of the person's
  samples) and compares with `match_threshold_calibrated`. The cohort is the strangers, other
  people's samples and unlabeled clusters of other conversations, never the scored voice, its
  conversation or the person's own labelled voices (left out by identity, so similar strangers
  and unlabeled voices stay). A voice whose raw score is under 0.4 is capped at 0. With fewer
  than 5 cohort voices for a person, that person's score falls back to raw top-k and
  `match_threshold`. A voiceprint's legacy part scores as one sample (its mean) until replaced.
  Every way of naming a voice (processor matching, Voice Scoring's current label, Who's this,
  the bulk reassign) honours the mode; the stranger veto compares the stranger's cosine with the
  same score used for matching (raw top-k, or the mean cosine). `"mean"` stays the default until
  `eval_speakers` has enough labels (at least 30, including some stranger answers).
  **One name per conversation**: voices are named best pairs first and each name goes to at most one voice of a
  conversation. With `split_voice_cosine` set (default `null`, off), a voice the clustering split off that clears
  a person on its own (threshold, margin, stranger veto) also takes a name already given when its centroid is
  within that cosine of every voice holding it. Two voices sharing a name keep their own clusters for Voice
  Scoring, enrollment and the voice library; a relabel of one leaves the shared tag's text alone, and a tap on
  the shared name reassigns by line only. At 0.5 the cluster-level eval names more voices right and none more
  wrong, but on the hand-labelled lines (`--lines`) it names more lines wrongly, so it stays off.
  **Voiceprint hygiene**: nobody but the wearer is named automatically after a person whose voiceprint has fewer
  than `min_samples` (2) samples (a reading counts each of its pieces; the legacy part its count). Cluster naming
  and turn rescoring run as without the rule; then every voice named after such a person is left unnamed, never
  handed to the runner-up, and its turns stay its own. Hints, Who's this and hand labels are unchanged. With `min_sample_speech_s` set
  (seconds; default 0, off), a voice with less speech is named by an answer, a meeting or the calendar but never
  becomes a voiceprint sample (readings and single lines are unaffected). It applies to new names only:
  `python -m pendant_direct.sample_hygiene [--apply] [--restore FILE]` lists the samples already stored from
  shorter voices and, with `--apply`, takes them out under the processor's lock after saving them to
  `backups/short-samples-<time>.json` (`--restore` puts them back). At 20 s the cluster-level eval names fewer
  voices wrongly but the hand-labelled lines get more wrong names, so it stays off.
  Adding a
  cluster that is already in, or removing one that isn't, changes nothing, so a retried
  answer is harmless. Every voiceprint write (feed and processor) holds
  `voiceprints/.lock` and lands through a unique temp file and `os.replace`. Each word takes
  the cluster of the windows around it, a change with under 1 s of speech is absorbed
  (`speakers.min_run_s`), and a new paragraph starts where the voice changes, even inside one
  whisper segment. Without word
  timestamps, or if windowed embedding fails, speakers are assigned per whisper segment.
  **Turn rescoring** (`speakers.rescore_turns`, `speakers.turn_rescore`, on by default) then gives
  each turn (one voice's words inside one whisper segment) with at least 2 s of speech a second look:
  its embedding is the mean of the windows centred in it (nothing is embedded again), scored with
  the `speakers.scoring` scorer against the conversation's voices (their centroids) and the
  voiceprints. It moves to another voice when it scores higher for that voice than for its own by
  `turn_margin`, or to a person when it scores higher for them than for the person its voice is
  named after (an unnamed voice: than every other person and the threshold) by that margin; targets
  must reach the match threshold, except a person replacing the one a voice is named after, who
  needs only `turn_person_floor` (0.4 raw cosine: a single turn scores lower against a voiceprint
  than a whole voice); a person must beat the closest stranger, and a turn never moves
  to a voice it sounds less like than its own, unless its own voice is unnamed and within `split_voice_cosine` of
  that voice. A person with two voices takes the turn into the one it sounds most like. Among people, the turn goes to the one `speakers.match` would pick:
  with a closeness prior, by margin above each person's threshold plus the prior. Audio inside a voice-sample
  reading window never reaches the speaker step. Margins are cosine (`turn_margin`, 0.1) unless both
  scores are calibrated (`turn_margin_calibrated`, 1.0 s-norm). A moved turn takes that voice's tag,
  or the person's name (a new voice, named automatically); a shorter turn follows its nearest turns
  of the same voice when they moved. A voice left with under 60% of its speech is marked `mixed`
  (two people in one voice): it is always offered in Voice Scoring (outside the budget), its
  embedding is never folded into a voiceprint (answers, meeting names and calendar enrollment name
  it without enrolling it) nor kept as a stranger or noise exemplar, its samples never vouch for a
  voice-sample reading, a trash restore keeps the flag, bulk relabel never matches it and a mixed tapped voice matches nothing,
  and the wearer's speech share treats it as unknown. If rescoring fails the cluster-level labels
  are written. Rescoring runs only when a conversation is first written, before any line can be
  reassigned by hand, and later voice-wide relabels still leave reassigned lines alone. Voices that
  lost or gained turns keep the centroid of the windows they end with.
  **Turn boundaries** (`speakers.Boundaries`, set by `speakers.*` in config.json; each pass that fails
  leaves the turns as they were): blip absorption (`blip_absorb`, on) runs after rescoring: a run of at
  most 3 words (`blip_words`) between two runs of one other voice joins them, and a run with under 1.5 s
  of speech (`short_run_s`) joins the voice either side when both are one voice, else its longer
  neighbour. Only on evidence: the windows with at least half their span inside the run must not favour
  its own voice's centroid over the target's by `blip_margin` (0.15 cosine); with no such window the run
  is kept, so a short reply is never taken away for lack of audio. Change points
  (`change_points`, off) run before rescoring: inside a run, where the mean of `cp_side_s` of windows
  before a point and after it are less alike than `cp_threshold`, the run splits; per voice, the pieces
  of its split runs are grouped at `cp_same`, the group most like the voice keeps it and the others join
  a voice they match or become new unnamed voices. Boundary snapping (`snap_words`, off; 1 or 2
  words) moves a voice change to just after a nearby sentence end, comma or pause when where it fell
  has none (a capitalized sentence start where it fell counts as natural). Audio cleanup (`denoise`, off) gives the voice model
  a spectrally gated copy of the clip (numpy STFT, noise floor per 30 s block); Whisper has already
  transcribed the clip as written. The three switched off didn't help on real data: change points only
  produced voices of a few seconds, cleanup lowered line accuracy (voiceprints hold un-cleaned
  embeddings), and snapping left line accuracy unchanged while naming a few more wrong lines. `python -m pendant_direct.eval_speakers --turns [--day D] [--cache DIR] [--only HH:MM]
  [--voices] [--set KEY=VALUE ...]` replays a day's conversations (leave-one-out, as `--lines`) and
  reports per conversation, as written and as replayed (with `--set`, also with those settings): lines,
  distinct voices, A–B–A blips of at most 3 words, paragraphs of 150+ words (and those in conversations
  with 2+ voices), line accuracy on hand-labelled lines and lines reassigned by hand; `--voices` adds
  each voice's speech seconds and closest voiceprints. Paragraph breaks at pauses are rebuilt from the
  chunk clock. Counts, tags and names only.
  **Wearer signal** (`wearer.py`): the pendant sits a few centimetres from the wearer's mouth and a
  metre or more from anyone else. Each rescored turn gets three near-field features from the clip:
  its speech level above the conversation's noise floor, how steeply energy falls after syllables
  (room echo smears it), and spectral flatness; plus its score for the wearer (`speakers.wearer`)
  from the configured scorer. They are stored per turn in `speaker_turns` (CPU: about 1% of the
  speaker step). `python -m pendant_direct.eval_speakers --fit-wearer [--day D] [--cache DIR]
  [--include-auto] [--save]` fits a logistic model over the four from the turns the wearer labelled
  (Voice Scoring answers, named voices, reassigned lines; `--include-auto` adds voices named by
  voiceprint, never ones the wearer signal named, which flags them `speaker_clusters.wearer_named`),
  reports leave-one-conversation-out accuracy on hand-labelled turns next to score-only and
  acoustic-only models and what the model would change, and with
  `--save` writes it to `speakers.wearer_model`. Acoustic features are z-scored per pendant (its
  own stats from the fit; a pendant the model never saw uses its own `speaker_turns` once it has
  100, else the pooled stats). With no model (the default) naming is unchanged. With one fitted for
  the running `speakers.scoring`, each voice's wearer probability (its turns' speech-weighted mean
  logit) at or above `wearer_p` (0.9) names it the wearer (a voice needs 5 s of speech, and a voice
  named someone else only when that score is under its threshold plus the turn margin); at or below 1 − `wearer_p` it loses his
  name unless its own score for him reaches `wearer_strict` (0.65 cosine) or
  `wearer_strict_calibrated` (2.5). Segment-level turns and `turn_rescore: false` skip it.
  Paragraphs read `[HH:MM] **The wearer:** …` once a voiceprint exists. Every voice is
  stored in `speaker_clusters` (an auto-matched voice keeps its score in `match_score`); the app asks about
  them through `/pendant/feed/speakers/*`. `/pendant/feed/interactions` serves who was in each conversation
  without any text (feed.py documents the shape).
  Only clusters that are really speech are offered: the feed picks each cluster's most
  speech-dense 3-12 s window (Silero VAD), needs at least 40% voiced and a 4-word excerpt,
  and stores the result (`voiced`, up to 5 new checks per request). Labels enroll only
  names in `speakers.allowed` (default `["the wearer"]`); a name picked or typed in the app
  (sent with `source: "user"`) is added to `speakers.allowed` in `config.json` first.
- **Voice Scoring** (`voices.py`, the app's Voice Scoring screen, opened from Today's
  toolbar or the Pendants tab, and the "Who's this?" card on Today: a large play button, the suggested name, and equal-width
  Skip / Someone else… / Not a voice buttons, where Skip is the same skip for today; Someone else… opens the
  name picker with a Remove name row, led by up to three of the card's likely people not already on it as one-tap
  name buttons): candidates are
  unlabeled clusters and clusters named automatically (`labeled_by` match, meeting or
  calendar), never ones the wearer answered or marked noise, least confident first, after the
  voices whose best match (at or above the new-voice cutoff) is a person with fewer than `min_samples` samples: nobody
  is named after them automatically yet, so an answer about them teaches the most.
  The score is the one for `speakers.scoring` (above). Confidence is min(score of the best voiceprint,
  2 × its margin over the second best), clipped to 0..1 (on the calibrated scale also divided by
  twice the threshold, so the match threshold sits at 0.5, as 0.5 cosine does on the raw one); 0 without voiceprints. A card has
  `similarity` (raw cosine), `score` and `calibrated` (false on the raw scale). A voice scoring below `new_voice_threshold`
  (scaled in the same ratio as `match_threshold` on the calibrated scale) is always
  offered; the rest share `daily_budget` (8) cards per local day (`voice_offers`). Each card
  plays its clip and asks "Is this <name>?" or "Who's this?": swipe right = yes, left = pick
  from known people (most recognized first, `recognized` in `GET /pendant/feed/speakers`),
  a new name, "Remove name" or "Not speech"; up = skip for today. A confirm folds the
  sample into that voiceprint once; a correction takes it out of the wrong voiceprint
  exactly (`speaker_clusters.enrolled_in` records where each sample went), adds it to the
  right one, and renames the cluster's paragraphs in that conversation's Raw section.
  When another cluster of the same conversation shows the same tag (e.g. two voices both
  auto-labeled "Alex"), their paragraphs can't be told apart: the voiceprints are still
  fixed but the transcript text is left as it is. Remove name ("Back to an unnamed speaker",
  `POST /pendant/feed/speakers/<id>/stranger`, which answers with the new label as `name`) keeps the voice in
  `strangers` (one row per answer, never merged), takes its sample out of any voiceprint and voice library, and
  labels it a "Speaker X" no other voice, line or queued relabel of the conversation uses (`voices.fresh_tag`; an
  unnamed voice keeps its own). Older transcripts label such voices **Someone:**; they stay as they are and still
  read as strangers. A voice whose raw top-k score for a person is below its closest stranger's cosine is never matched to that
  person, but similar voices are still asked about. "Not speech" marks the cluster `noise`
  and keeps its embedding in `noise_exemplars`; voices within `noise_similarity` of one go to
  the back of the queue. Transcript renames are queued in `transcript_relabels` and applied
  under the processor's lock (by the feed when it is free, otherwise the next processor
  run). The app never notifies about voices.
- **Record a voice sample** (`enroll.py`; "Record a voice sample" in the People view and Voice Scoring toolbars):
  a person reads 8 bundled sentences (`Core/VoiceSample.swift`) near the pendant; the pendant and the phone's
  microphone both record (the app asks for microphone permission; denied, only the pendant records). Pick a person
  with the shared LifeOS-first picker (tagged people first while nothing is typed, then LifeOS, a new name last),
  Start, read, Done. The app sends the name, the
  connected pendant's id and the wall-clock start and end (`POST /pendant/feed/enroll {name, device, start_ms,
  end_ms}` returns `{id, status}`; `GET /pendant/feed/enroll/<id>` is `waiting`, `added` with `samples`, or
  `rejected` with `reason`) and polls. A reading under 25 s, or a reserved name (Someone, Me, Speaker X), is
  rejected at once. Otherwise the row waits in `enrollments` until the pendant's audio for the window (padded 1 s
  each side) has settled: audio follows it, or 5 min have passed since the last of it; with no audio at all the
  reason becomes `no audio from that pendant` after 2 h. The processor then renders that audio, finds speech with
  Silero VAD and rejects `too little speech` under 25 s (a failing check waits up to 20 min for a late drain
  first). The speech is cut into 5-10 s pieces (at most 12), each embedded; the pieces must be one voice
  (average-linkage clusters at 0.5 cosine, `speakers.enroll_consistency`; the main voice must hold at least 85% of
  the speech, a smaller stray piece is dropped) or it is `more than one voice`. A name whose voiceprint already has
  5 or more samples (the wearer's always does) must match it at `match_threshold`, else `not allowed`: a different
  person typed that name. On success the name joins `speakers.allowed` (source `enrolled`: reading is the
  consent) and every piece is added as its own voiceprint sample keyed `enroll:<id>:<n>` (a retry adds nothing
  twice: a retry first removes every `enroll:<id>:*` sample, in the same locked write; an enrollment that ends rejected or expired removes them all too, and a request's overlap check and insert are one `BEGIN IMMEDIATE` transaction). A window holds its audio
  back from conversations only while `waiting` (`conversations()` skips chunks inside waiting and added windows);
  only an `added` enrollment consumes it (its chunks are marked done, and audio that drains in later stays out). A
  `rejected` window releases its audio, which is transcribed as an ordinary conversation, and so does one that
  expires: still waiting 2 h after the reading, or its check failing 5 times (`could not be checked`). A request
  ending over a minute ahead of the server's clock or starting over 2 h ago is a 400, and one overlapping any
  non-rejected enrollment on the same pendant is a 409, whatever the name (the same name and window again returns
  the first id). A conversation already written when the reading was reported is left as it is.
  The phone's recording (16 kHz mono WAV, at most 60 s, `PhoneRecorder.swift`) goes to
  `POST /pendant/feed/enroll/<id>/phone` (raw body, at most 8 MB) as soon as Done is tapped and is judged
  synchronously by `enroll.add_phone`: the same speech, one-voice, consent and wearer checks on the decoded upload,
  samples keyed `enroll:<id>:phone:<n>` (a re-send replaces them, a rejection removes them), and the verdict stored in
  the row's `phone_*` columns and returned as `phone: {status, samples|reason}` in the status. It never depends on
  the pendant copy, which is still judged when the pendant syncs (its samples, keyed `enroll:<id>:<n>`, have
  voice-library clips tagged origin `pendant`; the phone copy has no library clips). A reserved name is refused for
  the phone copy too. Undecodable audio or one over 60 s is a 400.
  Pieces of one voice score about 0.6-0.85 against each other and different voices 0.1-0.45, hence 0.5.
- **Reassigning from a transcript**: in a conversation, speaker names are tinted chips with a person icon
  (a plain caption under the header says to tap one); each chip and each line's time stamp takes taps across at
  least 44 × 44 pt (`minimumTapArea()`, drawn as before). Tapping a chip, or long-pressing it and choosing
  "Change speaker…", opens a
  sheet with an "Apply to" choice (This line / This conversation, the default / All past conversations), the
  known people (most recognized first), "New person…" and "Remove name" (destructive, "Back to an unnamed
  speaker"; it never applies to All past conversations, which then shows that reason). A scope that can't apply stays visible but
  disabled with its reason under it (a line that can't be addressed alone; a voice whose name another voice here
  shares, for the two voice-wide scopes), and the sheet never changes the chosen scope by itself (`ScopeChoice` in
  `Core/Reassign.swift`). The calendar's "Probably <name>" card has a full-width button that applies the selected
  scope in one tap ("Yes, <name> · this conversation"). Tapping any other name saves it at once for This line or
  This conversation and closes the sheet. With All past conversations, and for Remove name, the tap only picks, and
  the button at the bottom names the scope ("Change all past conversations…", "Remove name · this line",
  "Remove name · this conversation") and saves (`ScopeChoice.confirmsPick`). The conversation reloads after saving and
  shows "Changed to <Name> · Undo" ("Name removed · Undo") under the line for 10 s, one row
  per recent change (`UndoOffers`); a row stays while its undo runs or after one fails, with the reason or Retry,
  and undoing takes back only that change's names on screen.
  `POST /pendant/feed/conversations/<device>/<start_ms>/speaker` takes `name`, `scope` and `source: "user"`
  (which adds a new name to `speakers.allowed`; without it an unlisted name is refused). Remove name is `remove: true`
  (or `name: null`; the name "Someone" from older app versions means the same) with scope `line` or `conversation`
  (`all` is a 400): the voice or line becomes a stranger and a fresh "Speaker X", returned as `name` with
  `removed: true`. `scope: "conversation"`
  with the paragraph's `tag` relabels that voice cluster exactly like a Voice Scoring answer (also when it was
  answered before: the sample leaves its old voiceprint, stranger or noise record). A tag shared by two clusters is
  refused (409). `scope: "line"` with `paragraph` (index among the stamped paragraphs, from 0) changes only that
  paragraph's label and embeds its audio span (at most 20 s, at least 1 s) as a voiceprint sample keyed
  `para:<device>:<start_ms>:<index>` (`paragraph_samples`; assigning the line again changes nothing, assigning it to
  someone else moves the sample; Remove name stores it in `strangers`, holder `#stranger`, and drops its voice
  library clip; with no sample only the text changes). Conversations written before
  `conversations.paragraph_offsets` (JSON `[[start_s, end_s], ...]` per stamped paragraph, recorded at write time)
  existed get the text change only; the skip is logged and the response says `"sample": "skipped"`. Both scopes
  edit the Raw file through the `transcript_relabels` queue under the processor's lock. The day feed gives each
  conversation `speaker_tags` (tag -> cluster id, tags naming one voice only), `paragraph_offsets` and `line_overrides`
  (paragraphs reassigned one by one: a later voice-wide relabel leaves them alone). Line requests are serialized in
  the feed, so one line's sample is held by exactly one person. The app shows a saved name at once and keeps it until
  a reload's transcript agrees (the edit can be queued behind the processor).
  Timeline previews and search snippets show the same chips (not tappable: the row opens the conversation); the
  search `snippet` keeps `**Name:**` labels for that.
- **Reassigning across all past conversations**: `scope: "all"` (with the paragraph's `tag`) relabels the tapped
  voice like `scope: "conversation"` and also every voice in other conversations that scores at least the threshold for
  `speakers.scoring`: the cosine to the tapped voice (`mean`), or the score against the target person's samples plus the
  tapped voice (`topk`, `calibrated`). Left out: voices the wearer answered by hand (`user`/`dismissed`), voices marked as
  noise, voices already carrying the target name, mixed voices, voices whose tag another voice of their own conversation
  shares, and voices named automatically (`match`, `meeting`, `calendar`) after an enrolled person whose voiceprint
  fits them at least as well as the tapped voice does (the wearer's own lines stay the wearer's; not applied when that name
  is the one being corrected); all of these count as `skipped`. `GET /pendant/feed/conversations/<device>/<start_ms>/speaker/preview?tag=&name=`
  (`source=user` lets a typed name preview without adding it to `speakers.allowed`) changes nothing and returns
  `{conversations, days, words, skipped, name, ids, digest, sample}` (`ids` are the matched voice ids, `digest`
  fingerprints them with the words each would relabel); `words` counts only paragraphs labeled with a matched
  voice's tag, minus line overrides, and the apply must send the preview's `ids` and `digest` (400 without): the server recomputes the matches and
  applies only when they are identical, otherwise it changes nothing and answers 409 `{"error": "preview changed"}`
  (a conversation processed meanwhile, a new line override, a changed threshold), and the app fetches the preview
  again and shows the new counts. A sample entry has `date`, `time`,
  `title`, `device_id`, `start_ms`, `label` (the current name), `similarity`, `excerpt`, `words` and `paragraphs`
  (the lines the relabel changes); a sample holds at most 20 conversations, taken a round at a time across days
  (newest day first) and best match first within a day. Applying queues one `transcript_relabels` row per matched
  voice (line overrides stay), marks each as answered by the wearer (Voice Scoring stops asking), takes its sample out
  of any other voiceprint it was folded into, and records the batch in `bulk_relabels` (`old_*` columns hold the
  previous state). Only the tapped voice is added to the voiceprint, so a
  wrong match can't snowball. An unlisted name needs `source: "user"`; otherwise preview and apply return 403. In
  the app, choosing All past conversations and a name opens a preview (counts, a sample by day with excerpts);
  a sample conversation opens with its affected lines marked, and the confirm button shows the word and day counts.
- **Undoing a reassignment** (`reassign_undo.py`): every reassignment from a transcript (all three scopes) answers
  with `change_id` and `undo_until` (24 h later), and `POST /pendant/feed/reassign/<change_id>/undo` puts back
  everything it changed: the Raw section text, the voice rows (`speaker_clusters`, `strangers`, `noise_exemplars`,
  `paragraph_samples`, `voice_offers`), its `transcript_relabels` and `bulk_relabels` rows, the voiceprint and voice
  library files and `config.json` (a new allowed name, a LifeOS link). The record (`reassign_undo` row,
  `~/pendant-direct-data/reassign_undo/<id>/` with the rows before and after and copies of each changed file) keeps
  the section text from when its relabels were applied (`reassign_undo_sections`, filled by `voices.apply_relabels`).
  A reassignment and its undo both run under the processor's lock (503 while a processor run holds it past 30 s), so
  nothing else writes between a change's before and after snapshots. The undo refuses with 409, changing nothing,
  when the voices changed since, the conversation was edited after the change (undo the later change first) or a
  voiceprint it touched was deleted since (never rebuilt). A file changed by something else since (another sample
  enrolled, another name allowed or linked) keeps that change and loses only this one's samples, clips or config
  entries; otherwise each file and section comes back byte for byte. Rows, files and the note change together or not
  at all (`journal.json`, finished or rolled back by the next undo, the feed's start or the processor's next run).
  Undoing twice returns `already: true`; 410 after 24 h; records are purged after two days.
- **Meetings** (`meetings.py`): a conversation overlapping Granola/Wispr meeting notes
  links to every one of them ("Also recorded in [[note]]" each, the vault path when two notes
  share a name); a note that recorded it owns its follow-ups and events. Granola's
  `Me`/`Them` name the pendant's speakers (text labels; only allowed names get voiceprints).
  Notes that land later are linked by a relink pass over the last 2 days
  (`process.relink_meetings`), which also gives each conversation its calendar match.
- **Calendar match** (`attendance.event_for`, stored in `conversations.calendar_event`): the
  event that best overlaps the conversation. It may start at most 5 minutes before the event
  and run up to 30 minutes past its end (no overrun for an event listing nobody else); events
  with people outrank personal blocks. Attendees are resolved to LifeOS people (full name,
  email, entity id). The Raw section gets a `*Calendar: title (HH:MM–HH:MM) · names*` line and
  the day feed a `calendar` object (the line itself is left out of the feed's text). Each day's
  events are cached in `calendar_days` (today's refetched at most every 15 minutes, past days'
  hourly); all LifeOS calls of a run share a 10 s budget, and on a failure or a spent budget the
  cache stands. Consecutive conversations on one pendant split from continuous audio (at most 5 s apart, or
  a part of 25+ minutes the 30-minute cap cut at a pause under a minute: `process.cap_cut`), where the earlier one matched a note or an event itself, are one meeting
  (`CONTINUE_GAP_MS`); after a real gap a conversation matches on its own. Every part gets the same event and all
  of the parts' notes (`meeting_notes`; a note only another part overlaps is marked `via:
  continuation`, listed as "Same meeting as [[note]]", and leaves this part's follow-ups alone). With LifeOS down the stored match
  stands; a conversation without one shows no calendar line.
- **Probably <name>** (`calendar_hint.py`): for a voice, the event's attendees minus the
  wearer, people named by hand (or by a meeting or calendar pass) in the same conversation, and
  the voice's own name. One left: `likely` (name, label, entity_id, source "calendar", event);
  otherwise `candidates`. Voice Scoring and Who's this cards carry both, and the reassign sheet
  asks `GET .../conversations/<device>/<start_ms>/speaker/<tag>/suggest`. A voice carrying the
  wearer's name gets none unless tapped. Every picker leads with "Yes, <full name>" (from
  <event>); candidates head the list. Confirming is an ordinary `source: user` label with the
  `entity_id`; nothing is named by the suggestion itself.
- **Maybe <name>** (`calendar_hint.likely_present`, `calendar_hint.maybe`): the suggest route also asks LifeOS
  who was probably around for the conversation's window (`GET /api/crm/people/likely-present?start=&end=&limit=5`,
  UTC ISO times) and returns them as `maybe` (name, label, entity_id, `reasons`, source "lifeos") after the
  calendar's, in LifeOS's order, minus the same people the calendar leaves out and anyone the calendar already
  offers. One call per conversation window, cached 10 minutes, abandoned after 3 s; after a failure (LifeOS down,
  slow, or without that endpoint) no call is made for a minute and the answer is the calendar's alone. In the
  reassign sheet an unnamed voice ("Speaker X", Someone) with no calendar hint (no "Probably", no candidates) gets the first as
  "maybe: <name>?" with LifeOS's reasons and the same one-tap "Yes, <name> · <scope>"; the other maybes follow the
  calendar candidates at the head of the picker, their reasons under the name. Confirming or picking one is the
  sheet's ordinary hand label with the `entity_id`, so it allows the name and teaches the voiceprint exactly as
  tagging by hand does. Speaker matching never sees these suggestions. The Voice Scoring and Who's this cards
  (`GET /pendant/feed/speakers/unknown`) carry their `conversation` (device, start_ms, tag) and the same `hints`
  (likely, candidates, maybe): `likely_present_many` asks once per distinct conversation window on the page,
  first one call alone (so LifeOS being down costs one call), then a few at a time, through the same cache and
  back-off, and the page waits no longer than 3 s in all (calendar hints only after that). The Who's this card
  shows its top guess as one "<name>?" button with the reason under it: the calendar's likely person, else the
  first maybe when the calendar offers nobody and the card asks about no name. One tap saves it as the card's
  ordinary hand label (`source: user`, `entity_id`) and moves to the next card. The Voice Scoring card shows the
  same guess button above No and Yes, and the "Who is it?" sheet leads with it. Once a voice is named, the remaining
  cards from the same conversation stop offering that person (by `entity_id`, else by name) and fall to
  their next guess or to none. The Who's this card's "Someone else" offers up to three of the rest (calendar
  candidates, then maybes) as one-tap name buttons above the search, never a name already on the card.
- **Calendar voices** (`attendance.py`): a coworker's voiceprint is learned only from a
  calendar meeting whose attendees all resolve to people with emails in LifeOS, with 3 or
  fewer people (the wearer included) and no more distinct voices than invitees. Outcomes are
  stored per conversation in `conversations.calendar_enroll`.
- **LifeOS people** (`lifeos_people.py`, `closeness.py`, `link_people.py`, `rename_speaker.py`; all on the server, LifeOS at
  localhost:8000): names come from the CRM people list (`/api/crm/people`, closest circle then strongest, with the
  Dunbar circle and category) and `/api/crm/config` (`my_person_id`). `speakers.people` maps a speaker name to a LifeOS
  `entity_id`. `python -m pendant_direct.link_people [--dry-run]` links the allowed names and the wearer that have no
  link: the wearer to LifeOS's own "self" person, a name with a surname on a full-name match, a first name only when one
  person with that first name is in circle 2 or closer and at least 1.5x as strong as the next; otherwise it prints the
  name as ambiguous and links nothing. Picking a LifeOS person in the app (`source: "user"` with an `entity_id`) links
  it too, and names the speaker by their full LifeOS name (or the name they are already linked under). The link is
  metadata only: a LifeOS suggestion never creates a voiceprint or an allowed name until the wearer picks it.
  `GET /pendant/feed/people/suggest?q=` feeds every name picker (reassign sheet, Who's this, Voice Scoring "Someone else",
  Record a voice sample; one shared `SuggestionRows` in the app): LifeOS people first with name, circle and category,
  then known voices not among them, then the typed text as a last "Add as new name" row (`new: true`); an empty query
  lists the closest circles; with LifeOS down only the known voices and the typed name come back (`lifeos: false`). The
  People view shows "LifeOS: <full name> · circle N" or "Not linked" and "Change LifeOS person", which opens listing
  the LifeOS matches for the speaker's name (the default list when there are none), with a search below; one tap
  calls `POST /pendant/feed/people/<name>/link {entity_id}`: only the link changes. `python -m pendant_direct.rename_speaker OLD NEW
  [--apply]` renames a speaker everywhere (voiceprint, config, pages.db, Raw note paragraph labels, voice library); it
  is a dry run unless `--apply`, runs under the processor lock, and refuses when OLD and NEW are linked to different
  LifeOS people. The wearer is always read from `speakers.wearer`, so it can be a full name. **Closeness prior**: naming adds
  `closeness.weight * strength/100 + weight_calendar * on_the_calendar_event - weight_age * min(days, 365)/365`
  (`speakers.closeness`) to a person's margin above their own threshold, in whichever `speakers.scoring` mode, and only
  to reorder people who each already clear it (`speakers.match`, `voices.score`). It never lifts a voice over a
  threshold and only applies to enrolled names; with no links or LifeOS unreachable there is no prior.
- **Naming space** (`naming.py`, `embedders.py`): the processor names voices in ECAPA windows (a voice's 1.5 s
  window embeddings averaged, the clustering's own). The offline eval can name in another space, so a different
  model or whole stretches of speech can be measured before anything changes. The models are the WeSpeaker
  ResNet293-LM and ResNet34-LM ONNX exports (VoxCeleb, CC BY 4.0, see THIRD_PARTY_NOTICES.md), run on CPU through
  onnxruntime with WeSpeaker's own Kaldi fbank front end (80 mel bins, 25/10 ms Hamming frames, int16-scaled
  samples, each utterance's mean subtracted). They aren't in the repository: `python -m pendant_direct.embedders
  --fetch wespeaker-resnet293` (or `wespeaker-resnet34`) downloads one into `<data>/models/` and installs it only
  when its SHA-256 matches. Whole-stretch naming (`speakers.Namer`) joins a voice's own consecutive words into
  pieces of up to 20 s (at most 6, the ones with the most speech), embeds each piece as one utterance and averages
  them; a voice with under 4 s of speech gets no naming embedding and stays unnamed, and each scored turn is
  embedded whole for turn rescoring's person scores. Clustering and turn boundaries always use the ECAPA windows.
  `python -m pendant_direct.eval_speakers --spaces [--cache DIR] [--models DIR]` re-embeds the hand-named voices
  whose audio is kept (the voice-library clip, else the conversation clip cut at the voice's snippet) and every
  voice of their conversations in each space, rebuilds the voiceprints from that audio, names the conversations
  jointly and reads each space at the threshold accepting other-person trials as often as the stored ECAPA
  embeddings do at `match_threshold`. Line mode names in a space with `--set embedder=wespeaker-resnet293
  naming=whole match_threshold=T` (`--set voiceprints=rebuilt` rebuilds ECAPA windows' voiceprints the same way,
  the like-for-like control). `--set voiceprints=whole` (in `--spaces` and line mode) builds them from the kept
  voices instead, each embedded in up to 30 pieces of 20 s (or up to 2,400 windows), with the hand-named voices no
  kept voice covers still from their clips; `voiceprints=whole-only` uses the kept voices alone. On the hand-labelled lines no space beats the stored ECAPA voiceprints: every
  rebuilt one names more lines wrongly, because voiceprints rebuilt from short clips are weaker than ones built
  from whole voices, and too few people have kept voices in enough conversations yet for `voiceprints=whole`
  to make up for it, so naming stays in ECAPA windows. On the longest conversation of the eval (1,848 windows)
  the speaker step takes about 17 s in ECAPA windows, 27 s with ECAPA whole stretches, 60 s with ResNet293 whole
  stretches and 84 s with ResNet293 windows, on 8 CPU threads.
- **Context bars** (`context.py`, `speakers.context`, off by default): who is plausibly speaking depends on the
  setting. A calendar event listing someone besides the wearer, or a weekday between `work_start` and `work_end`, is
  work; everything else is personal. A linked person unexpected there needs a higher raw score to be named, in cluster
  naming and turn rescoring: a work contact outside work `work_off` (+0.05), family or a friend in a meeting they
  aren't on `personal_in_meeting` (+0.03). Bars only raise: a barred winner leaves the voice unnamed rather than
  handing it to the runner-up, and with LifeOS or the calendar unreachable there are no bars.
  `python -m pendant_direct.eval_speakers --context` measures them on the hand-labelled voices (from the stored
  calendar matches) at 1x and 2x. It names each conversation's voices together, as the processor does (each name at
  most once per conversation, best pairs first), against voiceprints rebuilt from the hand labels outside that
  conversation; `--independent` scores each voice on its own instead. The rebuilt voiceprints follow
  `min_sample_speech_s` and naming follows `min_samples`, as in line mode. `--set KEY=VALUE` tries a setting (e.g.
  `split_voice_cosine=0.5`) in `--context`, `--naming` and line mode.
- **Settings**: `~/pendant-direct-data/config.json`, defaults in `pendant_direct/config.py`:

  ```json
  {"summarizer": "fireworks",
   "llm": {"usd_per_m_prompt": 0.30, "usd_per_m_completion": 1.20},
   "notable": {"duration_minutes": null, "action_items_min": null, "keywords": []},
   "classify": {"fragment_words": 25, "fragment_low_conf_share": 0.4, "fragment_wpm": 5,
                "fragment_min_audio_s": 120, "model_fragment_words": 80, "media_wearer_max": 0.10,
                "media_absent_labelled": 0.8, "media_unlabelled_sim": 0.4, "conversation_wearer_min": 0.25, "media_wpm": 150, "media_words": 150},
   "speakers": {"enabled": true, "allowed": ["the wearer"], "wearer": "the wearer",
                "cluster_threshold": 0.25, "match_threshold": 0.55, "match_margin": 0.0, "split_voice_cosine": null,
                "min_samples": 2, "min_sample_speech_s": 0,
                "context": {"enabled": false, "work_off": 0.05, "personal_in_meeting": 0.03, "work_start": 9, "work_end": 18},
                "match_threshold_calibrated": 1.5, "scoring": "mean", "new_voice_threshold": 0.5, "daily_budget": 8, "noise_similarity": 0.6,
                "turn_rescore": true, "turn_margin": 0.1, "turn_margin_calibrated": 1.0, "turn_person_floor": 0.4,
                "wearer_features": true, "wearer_p": 0.9, "wearer_strict": 0.65,
                "wearer_strict_calibrated": 2.5, "wearer_model": null}}
  ```

## Day to day

- **Range and network.** Wear a pendant, keep the phone within about 10 m, and keep Tailscale on. Out of range, the pendant records to its own memory and the app catches up when it's back.
- **When notes appear.** A conversation reaches the notes folder about 5–10 minutes after a pause.
- **Keep the app open.** Don't force-quit it: swiping it away stops collection until it's opened again.
- **USB backup.** Charging a pendant on the server drains and clears it as a backup.

## Setup

You need:
- a Limitless Pendant;
- an iPhone with Developer Mode on;
- a Mac with Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen);
- a Linux machine with Python 3.10+ and systemd (user units), plus [uv](https://github.com/astral-sh/uv);
- [Tailscale](https://tailscale.com/) on the phone and the server.

Transcription runs on the CPU. A recent desktop keeps up with a day of talk.

**Server:**

```bash
git clone https://github.com/OWNER/pendant ~/Code/pendant   # OWNER: this repo's owner; && cd ~/Code/pendant && tools/dev-setup.sh
uv venv .venv
uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio  # CPU-only torch
uv pip install -e '.[dev]'
mkdir -p ~/.config/pendant-direct ~/.config/systemd/user ~/pendant-direct-data
umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/pendant-direct/token
cat > ~/pendant-direct-data/config.json <<'JSON'
{"notes_dir": "~/Notes/Pendant/Raw",
 "speakers": {"wearer": "Your Name", "allowed": ["Your Name"], "wearer_emails": ["you@example.com"]}}
JSON
cp server/*.service server/*.timer ~/.config/systemd/user/   # adjust paths in them if your checkout lives elsewhere
systemctl --user daemon-reload
systemctl --user enable --now pendant-ingest pendant-feed pendant-usb pendant-process.timer
tailscale serve --bg --https=8443 --set-path /pendant http://127.0.0.1:9795/pendant
tailscale serve --bg --https=8443 --set-path /pendant/feed http://127.0.0.1:9796/pendant/feed
```

- **USB backup:** your user must be in the `dialout` group.
- **Summaries:** set `FIREWORKS_API_KEY` for the installed units (`systemctl --user edit pendant-process pendant-feed`, an `Environment=` line, then restart them), or use `"summarizer": "heuristic"` to send nothing out. The token in `ios/Secrets.xcconfig` must match `~/.config/pendant-direct/token`.

**iPhone app** (on the Mac):

```bash
cd ios
cp Secrets.xcconfig.example Secrets.xcconfig   # server URL, token, team ID, names
# set PENDANT_BUNDLE_ID (your bundle ID; the app group is group.<it>) and your team ID in Secrets.xcconfig
xcodegen
xcodebuild -project PendantDirect.xcodeproj -scheme PendantDirect \
  -destination 'id=<iPhone UDID>' -derivedDataPath build -allowProvisioningUpdates build
xcrun devicectl device install app --device <UDID> build/Build/Products/Debug-iphoneos/PendantDirect.app
xcrun devicectl device process launch --device <UDID> <your bundle id>   # installing closes the app
```

- **Codesigning:** run `xcodebuild` from a Terminal window in the logged-in session, not over SSH, because codesign needs the unlocked login keychain.
- **Pairing:** in the app, tap **Pendants → Add pendant** with the pendant next to the phone.
- **Pendant already bonded elsewhere:** a pendant bonded to the original app must have its bond removed first; see the USB section of [`docs/protocol.md`](docs/protocol.md).

**Teaching it voices:**
- Add yourself and the people you talk to often to `speakers.allowed`.
- Record a voice sample in the app, or answer the "Who's this?" cards.
- Every answer improves the voiceprints.

## App design

The app is always dark: the night-blue gradient, teal accent and the system font.
`ios/PendantDirect/Core/Palette.swift` holds the colour tokens (background, surfaces, three
text levels, accent, destructive, star, warning, success) and lists which text token sits on
which surface; `PaletteTests` checks every such pair for WCAG AA (4.5:1). `Core/DesignSystem.swift`
turns them into SwiftUI with the type, spacing and radius tokens, the four button styles
(`.primary` filled teal, `.secondary` bordered, `.tertiary` teal text, `.destructive`) and the
`Card`, `MessageCard`, `StatChip`, `Chip`, `Badge`, `SpeakerChip`, `NameField` and `RowLabel`
components. Every button in a screen uses one of the four styles with a size by role, never
sized by its label: `.large` (52 pt, full width, a screen's single main action), `.medium`
(44 pt), `.small` (32 pt with a 44 pt tap area, inline actions in rows and cards), or
`.icon(.standard)` / `.icon(.play)` for icon-only circles (44 and 64 pt). Icons in labels are
fixed per size (20, 17, 14 pt) and long labels wrap to two lines, then truncate. Two or three
buttons side by side go in a `ButtonRow`, which gives each exactly the same width
(`ButtonLayoutTests`). `.plain` wraps a row or chip, and an inline chip that is a button (a speaker name, a line's time stamp)
adds `.minimumTapArea()`: a 44 × 44 pt tap area that overhangs the chip without moving anything; toolbar, swipe, context-menu and dialog
buttons stay system-drawn. The Who's this card, Voice Scoring card and answers, and the conversation
player are views in `Core/VoiceCards.swift`. `PENDANT_SNAPSHOT_DIR=/tmp/snaps swift test`
also writes PNGs of the components (rendered on macOS, so text is a little smaller than
on the phone).

Prefer one-tap selectors. When the app has a likely answer, offer it as one button that saves on
the first tap (the Who's this card's "Alex Rivera?", with the reason underneath), and offer a few
likely choices as a short list of tappable choices. Avoid flows that need several taps or another
screen to give a common answer: no confirmation sheet after a pick, no search where a short list
would do. A rare or destructive action may still ask for a confirmation.

`ios/scripts/screens.sh` runs the screen tour (`UITests/ScreenTourUITests.swift`). The tour
opens every main screen and sheet against the live feed, screenshots it, and exports the
PNGs to `ios/design/screens/<date>/`.

By default it runs on the iOS Simulator ("iPhone 18 Pro", the phone's model), over SSH if you like, since no code signing is needed.
`screens.sh --device` runs it on the phone. Run the device mode from the Mac's GUI Terminal
with the phone unlocked; it relaunches the app afterwards. Device mode:
- allows one run at a time, and one per branch unless `PENDANT_DEVICE_RERUN_REASON` is set;
- stops after `PENDANT_DEVICE_TIMEOUT` (900 s), and never retries;
- logs each run to `~/Library/Logs/pendant-device-tours.log`.

The app icon's source is `ios/design/app-icon.svg`; `ios/design/render_icon.py` renders
`AppIcon.appiconset/icon-1024.png` and the previews in `ios/design/previews/` (run it from a
throwaway venv with `cairosvg` and `pillow`; they are not app dependencies).

## License

MIT; see [`LICENSE`](LICENSE). Code adapted from MAkcanca/pendant-cli is also MIT-licensed; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Developing

This repo is public and is also the author's live checkout, so nothing personal may be tracked.
- **Setup:** install `zsh`, `gitleaks` and `trufflehog`, create your private denylist (below; it must list at least your own name, emails, hostnames and device IDs), commit with a no-reply email (`git config user.email <id>+<user>@users.noreply.github.com`), then run `tools/dev-setup.sh` once per clone. It points git at `tools/git-hooks`, which every worktree shares, and links your agent instructions in.
- **Agent instructions:** `AGENTS.example.md` is the public, generic set. `AGENTS.md` is gitignored and is a link to your private copy (`PENDANT_PRIVATE_AGENTS`, default `~/.config/pendant/AGENTS.md`), or a copy of the example when you keep none. The `post-checkout` hook links it into every new worktree.
- **Before every push:** the `pre-push` hook runs `tools/privacy-check.sh --tree <each pushed tip>`. That runs gitleaks, trufflehog and your private denylist (`PENDANT_PRIVACY_DENYLIST`, default `~/.config/pendant/privacy-denylist.txt`: one regex per line for your names, emails, hosts and device IDs) over the tracked tree, checks every new commit's message, author and added lines against the denylist, and blocks the push on a hit. Without a denylist every push is blocked: a contributor without private values to protect can use `PENDANT_PRIVACY_SKIP=1`.
- **What stays local:** config, secrets, recordings, databases, review screenshots and tour output are all gitignored (see `.gitignore`).
- **Issues and PRs are public too:**
  - use fictional examples;
  - never attach screenshots of the app (it always shows the live feed); describe UI changes in words;
  - check text by eye, since no tool can see it.

## Tests

```bash
.venv/bin/python -m pytest -q        # Python: protocol decode, ingest, process, feed, USB drain, Ogg writer
cd ios && swift test                 # Swift (on the Mac): protobuf, fragments, commands, ack rules, glance status, voice card flow, colour contrast, component snapshots
```

## Where things are

| Path | What |
|---|---|
| `ios/PendantDirect/Core/` | Protocol and ack logic, the Voice Scoring card flow (`VoiceDeck.swift`), the design system (`Palette.swift`, `DesignSystem.swift`, `VoiceCards.swift`); also the `PendantCore` Swift package for tests |
| `ios/design/` | App icon source (`app-icon.svg`), its render script and icon previews; screen-tour output goes to `ios/design/screens/` (gitignored) |
| `ios/PendantDirect/PendantManager.swift` | Bluetooth connection, drain/live modes, ack-after-fsync, state restoration |
| `ios/PendantDirect/CaptureStore.swift`, `Uploader.swift` | On-phone spool and background upload |
| `ios/PendantDirect/LiveActivityController.swift`, `ios/PendantWidgets/` | Lock-screen Live Activity, Dynamic Island and home-screen widget; state shared through app group `group.$(PENDANT_BUNDLE_ID)` (`Core/PendantStatus.swift`) |
| `ios/PendantDirect/ContentView.swift`, `Feed.swift`, `PeopleView.swift`, `PendantOrb.swift` | UI and feed client |
| `ios/PendantDirect/VoiceScoringView.swift`, `Speakers.swift` | Voice Scoring screen and the Today "Who's this?" card |
| `ios/PendantDirect/VoiceSampleView.swift`, `Core/VoiceSample.swift` | Record a voice sample: the sentences, the flow's states and the feed requests (`Core`, tested) and the screen |
| `pendant_direct/process.py` | Spool → SQLite (`~/pendant-direct-data/pages.db`) → conversations → vault |
| `pendant_direct/classify.py`, `backfill_kind.py` | Kind and importance rules; the one-off backfill |
| `pendant_direct/transcribe.py` | faster-whisper and the daily-file writer |
| `pendant_direct/speakers.py`, `voices.py` | Voiceprints and diarization; Voice Scoring candidates and answers |
| `pendant_direct/enroll.py` | Record a voice sample: the enrollment table, the waiting -> added/rejected state machine, the window exclusion |
| `pendant_direct/trash.py` | Delete, restore, the 7-day trash and tombstones |
| `pendant_direct/retention.py` | The daily audio retention pass and its `--dry-run` |
| `pendant_direct/voice_library.py` | The permanent per-person voice clip library and its backfill |
| `pendant_direct/voice_audio.py` | The kept whole voices of hand-named voices, their sync and backfill |
| `pendant_direct/embedders.py`, `naming.py` | WeSpeaker ONNX speaker models (fetch with checksum, fbank front end) and the naming space the eval can name voices in |
| `pendant_direct/sample_hygiene.py` | Lists, takes out (`--apply`) and restores voiceprint samples from voices under `speakers.min_sample_speech_s` |
| `pendant_direct/calibration.py`, `eval_speakers.py` | Top-k + s-norm scores and their cohort; `python -m pendant_direct.eval_speakers [--data DIR]` replays the wearer's hand answers leave-one-out for all three scoring modes side by side (counts and accuracies only, changes nothing; with under 30 voices it says so); `--naming` and `--context` name each conversation's voices together as the processor does (`--independent`: each voice on its own); `--lines [--notes DIR] [--day D] [--cache DIR]` replays the speaker step on conversations with hand-labelled lines and reports line accuracy before and after turn rescoring; `--fit-wearer` fits the wearer model (`wearer.py`); `--turns` reports blips, long paragraphs and voices per conversation, as written and replayed, optionally with `--set` overrides |
| `pendant_direct/usb/` | Zephyr shell driver, USB drain, plug-in watcher |
| `pendant_direct/{decode,flash_page,crypto,oggopus}.py`, `proto/` | Vendored from MAkcanca/pendant-cli (MIT) |
| `docs/protocol.md` | What we know about the pendant's BLE and USB protocols |
| `~/pendant-direct-data/` | Spool, database, clips, keys, `trash/` (outside git) |
