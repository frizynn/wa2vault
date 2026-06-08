# wa2vault

A personal, **read-only** tool that extracts the last N days of a specific
WhatsApp chat, group, or channel (text, images, and voice notes), transcribes
the voice notes to text **locally** (CPU), and writes a structured Markdown
"message history" note into an [Obsidian](https://obsidian.md) vault so AI
agents can read it.

wa2vault never sends WhatsApp messages. Pair once, then run fast incremental
pulls.

## Architecture

wa2vault is a thin orchestration layer over two pieces:

1. **wacli** (external Go binary — [github.com/openclaw/wacli](https://github.com/openclaw/wacli), MIT, v0.11.0)
   is the WhatsApp data layer. It pairs as a linked WhatsApp Web device (QR),
   mirrors messages into a local **SQLite store with FTS5**, downloads media
   (images + voice notes), and emits **JSON** on every command (`auth`, `sync`,
   `messages`, `chats`, `channels`, `history`, `media`, `doctor`, …). wa2vault
   does **not** reimplement WhatsApp — it shells out to wacli and reads its
   store.

2. **wa2vault** (this Python package) queries wacli for the last N days of a
   chat → transcribes voice notes with a pluggable local ASR backend → renders
   Markdown into the Obsidian vault, with a per-message transcript cache so
   reruns are cheap.

```
phone ──QR──▶ wacli (linked device) ──▶ local SQLite store + media
                                            │
                                            ▼
                              wa2vault: export → transcribe → render
                                            │
                                            ▼
                                   Obsidian vault (Markdown)
```

### wacli store location

wacli keeps its SQLite DB and downloaded media in one **store directory**. By
default this is the platform state directory; on **Linux** that is the XDG
state dir:

```
~/.local/state/wacli
```

This was confirmed by running `wacli doctor --json`, whose `data.store_dir`
field reports the active store path. Override it with the `--store` flag or the
`WACLI_STORE_DIR` environment variable. In wa2vault, set `wacli_db` in the
config to point wacli at a custom store (passed through as `--store`).

## Requirements

- **Python 3.12+** and [**uv**](https://docs.astral.sh/uv/) for environment
  management.
- **ffmpeg** (used to decode WhatsApp Opus voice notes before transcription;
  already present on the target machine).
- **wacli** v0.11.0 binary on `PATH` (install below).

## Install

### 1. wa2vault (Python)

```bash
cd ~/Documents/repos/wa2vault
uv sync
```

Run the CLI with `uv run wa2vault …` (or activate the venv and call
`wa2vault`).

### 2. wacli (WhatsApp data layer)

Download the prebuilt **linux/amd64** release binary and install it to
`~/.local/bin`:

```bash
curl -fsSL -o /tmp/wacli.tar.gz \
  https://github.com/openclaw/wacli/releases/download/v0.11.0/wacli_0.11.0_linux_amd64.tar.gz

# Verify the checksum (from the release's checksums.txt):
echo "8fe8f14694cd439b066db8ced8689cff5653f4aac1904b25a639e1560492ae43  /tmp/wacli.tar.gz" \
  | sha256sum --check

mkdir -p /tmp/wacli && tar -xzf /tmp/wacli.tar.gz -C /tmp/wacli
install -m 0755 /tmp/wacli/wacli ~/.local/bin/wacli
rm -rf /tmp/wacli.tar.gz /tmp/wacli

wacli --version   # -> wacli 0.11.0
```

Make sure `~/.local/bin` is on your `PATH`.

For other platforms, pick the matching asset on the
[releases page](https://github.com/openclaw/wacli/releases). If no prebuilt
binary fits, build from source with a Go 1.23+ toolchain
(`go install` the module path from the repo's `go.mod`) and place the resulting
`wacli` on your `PATH`.

### 3. ffmpeg

Already installed on the target machine. Otherwise: `sudo apt install ffmpeg`.

## Pairing runbook

1. Pair your phone (one-time):

   ```bash
   wa2vault auth
   ```

   A QR code appears in the terminal. On your phone, open **WhatsApp →
   Settings → Linked Devices → Link a Device** and scan it. wa2vault is
   **read-only and never sends messages** — pairing only lets it mirror your
   history locally.

2. Sync the local store:

   ```bash
   wa2vault sync
   ```

3. Find the chat you want:

   ```bash
   wa2vault chats
   ```

4. Pull the last N days into the vault:

   ```bash
   wa2vault pull --chat "My Group" --days 30
   ```

   Flags: `--no-transcribe` (skip voice-note ASR), `--no-media` (skip media).

One-off transcription of a single audio file:

```bash
wa2vault transcribe path/to/voice-note.ogg
```

## Configuration

On first run, wa2vault writes a TOML config to the user config dir (Linux:
`~/.config/wa2vault/config.toml`). Keys:

| Key             | Default                        | Meaning                                               |
| --------------- | ------------------------------ | ----------------------------------------------------- |
| `vault_dir`     | `~/Obsidian/wa2vault`          | Obsidian vault root for output.                       |
| `output_subdir` | `Chats`                        | Subfolder inside the vault for chat notes.            |
| `wacli_bin`     | `wacli`                        | wacli executable name/path.                           |
| `wacli_db`      | *(empty → wacli default)*      | Custom wacli store dir; empty uses `~/.local/state/wacli`. |
| `asr_backend`   | `faster-whisper`               | ASR backend (`faster-whisper` or `nemotron`).         |
| `asr_model`     | `medium`                       | ASR model name/size.                                  |
| `language`      | `es`                           | Default language hint (ISO-639-1).                    |
| `default_days`  | `30`                           | Default `--days` window for `pull`.                   |
| `cache_dir`     | platform cache dir             | Transcript cache + scratch files.                     |

Any key can be overridden at runtime with a `WA2VAULT_*` environment variable
(e.g. `WA2VAULT_LANGUAGE=en`), and `--config` selects an alternate config file.

## ASR backends

- **faster-whisper** (default): Whisper via CTranslate2, CPU with `int8`
  quantization. Decodes voice notes with ffmpeg to 16 kHz mono WAV before
  transcription.
- **nemotron** (optional, future): an `nvidia/nemotron-3.5-asr-streaming-0.6b`
  CPU backend, planned for a later release. Not implemented yet.

## Honest caveats

**1. History is bounded.** A linked device only receives what your phone
pushes. At pairing you get a full sync of up to roughly the **last ~1 year**,
then **incremental** updates from then on. Reaching far-back history on demand
(`wacli history backfill`) is **best-effort** — your phone may not have it, and
**old media can expire** on WhatsApp's CDN, in which case it cannot be
re-downloaded. Pull regularly to keep your local archive complete.

**2. Unofficial client / ToS.** wacli is an unofficial WhatsApp Web client,
which is technically against WhatsApp's Terms of Service. The strict
**read-only, never-send** posture keeps ban risk low (anecdotally under ~2% per
year) but **not zero**. Recommendation: link as a **secondary device** rather
than your primary number, and avoid automated sending entirely (wa2vault never
sends).

## License

MIT — Juan Francisco Lebrero. See [LICENSE](LICENSE).
