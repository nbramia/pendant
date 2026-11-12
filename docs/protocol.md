# Limitless Pendant protocol notes

What this project relies on, verified on firmware 1.1.4 and 1.1.11. Field
numbers refer to `proto/server.proto` and `proto/flash_page.proto`.
`BasedHardware/omi` (`app/lib/services/devices/connectors/limitless_connection.dart`
and `app/ios/Runner/Limitless/`) is the reference implementation.

## BLE

- Service `632DE001-604C-446B-A80F-7963E950F3FB`; write `632DE002`; notify `632DE003`.
- Every write and notification is a fragment `{1: msg_index, 2: seq, 3: num_fragments, 4: payload}`.
  Reassemble per `msg_index`. Payloads are `ServerCommandMsg` (to pendant) and
  `PendantAllMsg` (from pendant).
- Each command carries `RequestData` in field 30 `{1: request_id, 2: do_not_ack=false}`;
  the pendant answers with a `PendantAllMsg` holding only `response_data` (field 6).
- iOS pairs on first access to the encrypted characteristics. The pendant is LE-only.
  It keeps one bond; a pendant bonded elsewhere must have its bond removed first (see USB).
- The pendant advertises while not charging. With `allow_recording_on_usb: false` it
  doesn't advertise while plugged in.
- AccessorySetupKit's picker does not find the pendant (the 128-bit UUID is likely only in
  the scan response), so the app scans with plain CoreBluetooth.

## Session

1. Subscribe to `632DE003`.
2. `SetCurrentTime` (field 6, ms since epoch). Without a valid clock, page timestamps are
   pendant uptime; resend on every connect.
3. `GetDeviceInfo` (14) → `DeviceInfoMsg`: device ID (field 1, shown as 16 hex digits),
   firmware (3), storage instance (5; changes on wipe), battery (12).
4. `GetDeviceStatus` (21) → `DeviceStatus.storage_state` (field 5):
   oldest (1), newest (2, int32; `-1`/`0xFFFFFFFF` = empty), free (4), total (5) pages.
   The pendant also pushes status unprompted every few seconds.
5. `DownloadFlashPages` (8) `{1: batch, 2: realtime}`:
   - `{0,1}` live stream; `{1,0}` drain stored pages; `{0,0}` record to flash only.
   - Live pages are partial copies of the page being recorded, with a different page
     timestamp than the sealed copy a drain later sends. The Opus packets are identical.
   - The newest page is still open; a drain delivers it only once sealed.
   - Omi re-sends the live command periodically; the pendant can stop streaming otherwise.

## Pages and acks

- Audio arrives as `StorageBufferMsg` (field 2): ingest type (1: 1 live, 2 batch,
  3 debug logs), index (5), flash page bytes (6). proto3 omits zero values: a missing
  index means page 0.
- `DeleteFlashPage` (7) `{1: older_than_or_equal_to_index}` frees every page up to that
  index. The pendant then reports `oldest = index + 1`.
- A drain resumes from the pendant's own cursor, not from `oldest`. Ack through the
  highest page received in a drain, after it is fsync'd (Omi does the same, every 25
  pages). Never ack because of live pages: that could free pages not yet drained.
- Live-only clients never free flash. A pendant that fills (`free → 0`) stops recording
  and streaming.

## FlashPage

`FlashPage.absolute_timestamp_ms` (1) is when the page was sealed. Each page holds
chunks; audio chunks carry Opus at 16 kHz mono, 20 ms per packet (one packet per
chunk). App-paired pendants record plaintext. A pendant once provisioned with our
own server key over USB (`crypto gen_keys`) encrypts with AES-GCM until reset; its
key file lives in `~/pendant-direct-data/keys/` (see `pendant_direct/keys.py`).

## USB (Zephyr shell)

Plugging a pendant in exposes `/dev/ttyACM0` (USB `2fe3:0110`), a Zephyr shell at
115200 baud ending each response with `uart:~$`. The user must be in `dialout`.

| Command | Use |
|---|---|
| `storage status` | `@KEY value` lines: device ID, storage instance, OLDEST, NEWEST, FREE_PAGES |
| `ftl read_sector <n>` | Hex dump of one 4096-byte sector. Capture page `i` is sector `768 + i % 95232` |
| `storage clear` | Wipe capture storage |
| `settings list` / `settings delete bt/keys/<addr>` (also `bt/sc/…`, `bt/ccc/…`) | Inspect / remove a BLE bond; reboot after |
| `kernel reboot cold` | Reboot |
| `config list` | Pendant settings (LED brightness, `allow_recording_on_usb`, …) |

Sector layout: FTL header, u32-LE FlashPage length at `0x38` (`0xFFFFFFFF` = erased),
FlashPage protobuf at `0x3C`. The hex dump is the only read path, so USB drains run at
about 1.8 s per page (roughly real time); baud rate settings have no effect.
