# wacli adapter contract

[Project home](../../README.md) · [Documentation](../index.md) ·
[Architecture](../architecture.md)

This maintainer document records the external contract implemented by
`src/wa2vault/wacli.py`. wa2vault supports wacli `>=0.17.1,<0.18`; revalidate
this page and the contract tests before widening that range.

## Process policy

`WacliClient` is the single owner of executable discovery, global arguments,
account/store selection, environment guards, and timeouts.

- JSON reads use `--json --read-only` and `WACLI_READONLY=1`.
- `sync` and `groups refresh` omit read-only because they update the local
  mirror; the client still exposes no send or presence operation.
- `wacli_account` becomes `--account NAME`.
- `wacli_db` becomes `--store PATH`.
- Account and store are mutually exclusive before process execution.
- Non-interactive commands are bounded. Interactive `auth` has no timeout.
- Media downloads in read-only mode always provide an explicit cache output.

## Commands used

```text
auth
sync --once [--idle-exit Ns] [--download-media]
chats list --limit N
groups list --limit N
groups refresh
history coverage
messages export --chat JID --after RFC3339 [--before RFC3339] [--limit N]
media download --chat JID --id MESSAGE_ID --output PATH
```

The CLI uses plain terminal output for interactive/auth, sync progress, and
history coverage. Programmatic reads use wacli's JSON envelope:

```json
{"success": true, "data": {}, "error": null}
```

`run_json` returns `data`. A nonzero exit, invalid JSON, or an unsuccessful
envelope becomes `WacliError`.

## Chat fields consumed

Chat list objects are expected to expose these snake_case fields:

```text
jid, kind, name
```

`kind` values `dm`, `group`, and `newsletter` normalize to `dm`, `group`, and
`channel`. Unknown kinds fall back to the JID suffix. Group list rows may use
PascalCase; the adapter reads `JID` and `Name` defensively to recover a group
subject missing from the chat list.

## Message fields consumed

wacli's Go store currently serializes most message fields with PascalCase. The
adapter accepts the documented casing plus known compatibility aliases:

```text
MsgID
ChatJID, ChatName
SenderJID, SenderName
Timestamp
FromMe
Text, DisplayText
MediaCaption, MediaType, MimeType, Filename, LocalPath
QuotedMsgID / quoted_msg_id
```

Rows without a message ID, chat JID, or valid timestamp are ignored. Time
windows are sent to wacli and reapplied in Python for exact inclusive/exclusive
behavior. For media messages, only `MediaCaption` is user text; wacli's
synthetic `DisplayText` placeholders are not archived as captions.

## Media behavior

An existing nonempty cache target is reused. Otherwise the adapter chooses a
suffix from the original filename, MIME type, then message kind. HTTP status
404 or 410 in wacli's media error is treated as expired upstream media; other
failures remain unavailable without being mislabeled as expiry.

The downloaded target is isolated by profile and stable chat identity. Copied
vault filenames also include message identity and a sanitized original name.

## Compatibility test checklist

When updating wacli:

1. inspect upstream help and release notes;
2. verify global argument order and account/store semantics;
3. capture only synthetic or fully redacted payload shapes;
4. update parser fixtures for changed fields/envelopes;
5. run the complete test matrix without a real account;
6. perform any live verification privately and never commit its output;
7. update the supported range in README, SECURITY, and this page together.
