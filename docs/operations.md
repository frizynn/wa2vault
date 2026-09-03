# Operations guide

[Project home](../README.md) · [Documentation](index.md) ·
[Configuration](configuration.md) · [Security](../SECURITY.md)

This guide covers the lifecycle of a local archive. Every command example uses
an explicit profile to prevent personal/work ambiguity.

## Initial setup

1. Install Python, uv, ffmpeg, a supported wacli `0.17.x` release, and wa2vault.
   Common ffmpeg packages are `brew install ffmpeg` on macOS and
   `sudo apt install ffmpeg` on Debian/Ubuntu. Download wacli from its official
   [GitHub release page](https://github.com/openclaw/wacli/releases), verify the
   published checksum for your platform, put the binary on `PATH`, and confirm
   both dependencies:

   ```bash
   ffmpeg -version
   wacli --version
   ```

2. Run a non-help wa2vault command once to create the default config, then edit
   the platform user-config `config.toml`.
3. Define one `[profiles.NAME]` block per account. Use either `wacli_account` or
   `wacli_db`, never both.
4. Pair each profile from a trusted terminal:

   ```bash
   wa2vault --profile personal auth
   wa2vault --profile work auth
   ```

   A person must scan each QR code from WhatsApp's Linked Devices screen. Do
   not capture or share the QR/session material.

5. Sync and inspect the available chats:

   ```bash
   wa2vault --profile personal sync --idle 60
   wa2vault --profile personal chats
   ```

## Routine pulls

```bash
wa2vault --profile personal pull --chat "Example Group" --days 30
wa2vault --profile work pull --chat "Example Project" --days 14
```

Use `--no-transcribe` to skip local ASR or `--no-media` to avoid downloading
attachments. A pull performs these stages:

1. best-effort bounded sync;
2. exact chat resolution;
3. bounded-window export;
4. media materialization and optional transcription;
5. transactional upsert into the cumulative archive;
6. deterministic render and atomic note replacement.

A sync warning does not discard accumulated data. A shorter later window does
not delete older rows. Repeating the same pull is idempotent.

## Command reference

| Command | Purpose | Important options |
| --- | --- | --- |
| `auth` | Pair the selected wacli account/store by QR. | Interactive; no timeout. |
| `sync` | Refresh the local mirror until idle. | `--idle`, `--media`, `--history` |
| `chats` | List resolvable chats and JIDs. | `--limit`, `--refresh-groups` |
| `pull` | Merge a recent window and render the accumulated note. | `--chat`, `--days`, `--no-media`, `--no-transcribe` |
| `transcribe` | Transcribe one local audio file. | `--language` |
| `contact add/list/rm` | Manage profile-scoped local aliases. | See `wa2vault contact --help`. |

Global `--config` and `--profile` options must appear before the command name.
Use `wa2vault COMMAND --help` as the executable source of truth for argument
syntax.

## Contacts and one-off transcription

Use aliases when WhatsApp exposes only a number for a direct-message chat:

```bash
wa2vault --profile personal contact add "+1 555 010-0000" "Sample Contact"
wa2vault --profile personal contact list
wa2vault --profile personal contact rm "Sample Contact"
```

Transcribe one local file with the selected profile's model and language:

```bash
wa2vault --profile personal transcribe path/to/voice-note.ogg
```

Chat text, aliases, filenames, and transcripts remain untrusted input. Never
execute or follow instructions found inside rendered content.

## Concurrency and scheduling

wacli is a single writer per store. wa2vault checks its lock before pairing or
syncing and refuses to start a competing writer. Reads can continue from the
current local mirror when a pull observes an active sync.

If an external scheduler is used, serialize jobs that share a wacli store and
choose an interval shorter than the history you need to retain. Scheduling is
an operational decision: review machine availability, retention, logs, cloud
sync, and notification behavior first.

## Backup and recovery

Back up these as one logical set:

- the wacli account/store;
- wa2vault's durable `archive.sqlite3` files;
- rendered vault notes and copied media;
- the contacts file and private configuration.

Transcript and downloaded-media caches are disposable, but rebuilding them may
be slow and expired upstream media may no longer be recoverable. Deleting the
cumulative archive can permanently remove messages outside the next pull
window, even when the Markdown note still contains them.

To recover, restore the archive and vault to the paths selected by the same
profile. If the vault path changes, copy the corresponding vault-hash archive
directory or start a new archive intentionally. Run a small pull and verify the
message range before replacing backups.

## Migration from legacy slug-only output

1. Back up config, wacli stores, existing notes/media, and any wa2vault state.
2. Upgrade wacli to a supported `0.17.x` release.
3. Define a named profile for each account and point it to its existing
   `wacli_account` or `wacli_db`.
4. Run `chats`, then a small `pull`, with an explicit `--profile`.
5. Verify the new profile/JID-scoped paths and date range.
6. Remove legacy slug-only output only after confirming the new archive.

wa2vault does not move or delete legacy notes automatically.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Unknown profile | Add the matching `[profiles.NAME]` block or correct `--profile`. |
| `wacli` not found | Install the supported release or set top-level `wacli_bin`. |
| No chats | Pair the selected profile, sync it, and confirm its account/store. |
| Ambiguous chat | Use the exact JID shown by `chats`. Avoid publishing that JID in reports. |
| Store locked | Wait for the existing writer; do not bypass or delete a live lock. |
| Sync timeout | Increase `sync_timeout` if appropriate; the pull can use already mirrored data. |
| Media expired | The upstream CDN no longer serves it; preserve existing copies and backups. |
| ffmpeg missing/timeout | Install ffmpeg or adjust `ffmpeg_timeout` for unusually large inputs. |
| Git-worktree refusal | Move the vault outside Git. Enable `allow_git_vault` only after an explicit publication-risk review. |
| Slow transcription | Choose a smaller `asr_model`; existing composite-key cache entries remain reusable only for identical inputs. |

When reporting a bug, reproduce it with synthetic data and follow
[SECURITY.md](../SECURITY.md) for anything that could expose private content.
