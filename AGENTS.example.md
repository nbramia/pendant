# pendant

> **This repository is PUBLIC.** Everything git-tracked, and every issue, pull request, review
> comment and commit message, is visible to anyone. Never put anything personal into them: no real
> names, transcripts or conversation details, screenshots of the app, device IDs, hostnames, IPs, file
> paths from a real machine, or email addresses. Personal values belong only in gitignored files
> (`config.json`, `pendants.json`, `ios/Secrets.xcconfig`, `AGENTS.md`); examples use the fictional
> fixture people. See "The repo is public" below.


> Public, generic agent instructions. Copy this to `AGENTS.md` (gitignored) and add your own
> machines and habits there; `tools/dev-setup.sh` links a private file in if you keep one
> (`PENDANT_PRIVATE_AGENTS`), else copies this one.

Limitless Pendants → iPhone app → the server → `<notes_dir>/YYYY-MM-DD.md` (the notes folder set in
`~/pendant-direct-data/config.json`). Read `README.md` for the layout and setup and
`docs/protocol.md` before touching protocol code. The wearer has a pendant on all day: never ship a
change that can stop collection without testing it.

## Invariants

- Ack (`DeleteFlashPage`, frees every page <= index) only after the pages are fsync'd on the phone,
  and only through the highest page received in a batch drain. Never because of live-stream pages.
  The rules live in `ios/PendantDirect/Core/AckTracker.swift`; change them only with tests.
- The phone deletes a spooled segment only after the server echoes its SHA-256; the server echoes
  only after an fsync.
- Pages are deduped on (device, page, chunk position, Opus packet hash). Live copies of a page are
  partial and carry a different page timestamp than the drained copy.
- Transcription stays local on the server. No cloud speech-to-text.
- Never commit `ios/Secrets.xcconfig`, `.pmsg` files, recordings, transcripts, screenshots of real
  data or keys. Personal values (names, emails, device IDs, hostnames, vault paths, team IDs) belong
  in `config.json`, `pendants.json` or `ios/Secrets.xcconfig`, never in the repo; tests use the
  fictional people in the existing fixtures.
- Wiping or factory-resetting a pendant needs the wearer's OK first (the USB backup's
  wipe-after-drain is the one standing exception).
- Speaker voiceprints are consent-gated: enroll only names in `speakers.allowed`.

## Working on it

- Python: `.venv/bin/python -m pytest -q`. Swift core: `cd ios && swift test` on the Mac. Tests fake
  the speaker embedder (`speakers.get_embedder`); the real-model test runs with `PENDANT_SLOW=1`.
- iOS builds need the unlocked login keychain for codesigning: run `xcodebuild` in the Mac's GUI
  session, not over SSH.
- Installing an update kills the app and iOS doesn't relaunch it. Relaunch it with
  `xcrun devicectl device process launch --device <UDID> <bundle id>`, or the pendant only buffers.
- Services: `systemctl --user status pendant-ingest pendant-feed pendant-usb pendant-process.timer`;
  `journalctl --user -u pendant-process`.
- Changing the `pages.db` schema: migrate in `process.db()` and keep `done` marks, or the day's
  conversations get written to the notes twice.
- Summaries, speakers and event/highlight writes must never block transcription: on failure, fall
  back (heuristic title, unlabeled text) and still mark chunks done.
- UI changes: iterate on the iOS Simulator (`ios/scripts/screens.sh`); run the on-device pass
  (`screens.sh --device`) once, right before merging. Screenshots stay local
  (`ios/design/screens/`, gitignored).
- Docs describe the current system only; open work lives in issues.

## The repo is public: what may reach GitHub

Everything in the repo, and every issue, PR description, review comment and attachment, is public.
- Never attach or paste screenshots of the app to GitHub. The app always shows the live feed (the
  tour's samples are added on top of it, not instead of it), so every screenshot can show real
  transcripts, names and pendant IDs. Tour and device screenshots stay local in
  `ios/design/screens/` (gitignored), and reviewers read them there; describe UI changes in words.
- Issue and PR text, commit messages and review comments use fictional examples (the fixture people),
  never real transcript lines, names, conversation titles, device IDs, hostnames, IPs or file paths.
- Personal values go in `config.json`, `pendants.json`, `ios/Secrets.xcconfig` or your private
  `AGENTS.md`, never in tracked files.
- The pre-push hook (`tools/dev-setup.sh` installs it) checks the pushed tree with
  `tools/privacy-check.sh` (gitleaks, trufflehog, your private denylist) and every new commit's
  message, author and added lines against the denylist. It can't see issue or PR text, browser
  attachments, or a name your denylist doesn't list. Commit with a no-reply email.
