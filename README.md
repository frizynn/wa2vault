# wa2vault

Local-first, read-only WhatsApp archiving for Markdown vaults.

wa2vault reads a linked account through
[wacli](https://github.com/openclaw/wacli), keeps a cumulative local archive,
optionally transcribes voice notes on the same machine, and writes deterministic
Markdown plus media for Obsidian or similar tools. It never exposes a command
that sends WhatsApp messages.

> [!WARNING]
> wacli uses the unofficial WhatsApp Web multidevice protocol. This may violate
> WhatsApp's terms or put a linked account at risk. Chats contain other people's
> personal data; make sure your collection, transcription, retention, and backup
> practices are lawful and appropriate. This project is provided without
> warranty under the MIT License.

## Why wa2vault

- **Personal/work isolation:** named profiles select one wacli account or store
  and isolate contacts, caches, archive state, notes, and media.
- **Cumulative output:** recent windows are upserted into SQLite before the full
  known timeline is rendered, so a shorter pull cannot truncate older history.
- **Stable identity:** profile, chat JID, and message ID prevent collisions when
  chats share or change display names.
- **Local processing:** faster-whisper transcription runs locally and is cached
  by every input that can change the result.
- **Defensive output:** writes are atomic, subprocesses are bounded, generated
  files are private where supported, and vaults inside Git worktrees are refused
  by default.
- **Untrusted-content boundary:** chat text and transcripts are rendered as data,
  never as instructions for an agent.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on `PATH`
- wacli `>=0.17.1,<0.18` on `PATH`

Install a supported wacli release from its official release page and verify its
published checksum. wa2vault intentionally targets a narrow wacli minor version
because its JSON output is an external contract.

## Quick start

Install wa2vault in an isolated environment:

```bash
uv tool install "wa2vault @ git+https://github.com/frizynn/wa2vault.git"
wa2vault --help
```

Create the platform-default config on first use, then edit it and define one
block per account:

```toml
vault_dir = "/path/to/private/notes"
state_dir = "/path/to/private/wa2vault-state"
cache_dir = "/path/to/private/wa2vault-cache"
allow_git_vault = false

[profiles.personal]
wacli_account = "personal"

[profiles.work]
wacli_account = "work"
```

`wacli_account` and `wacli_db` are mutually exclusive within a resolved
profile. Keep configuration and every generated data directory outside this
source repository.

Pair and pull using an explicit profile:

```bash
wa2vault --profile personal auth
wa2vault --profile personal sync
wa2vault --profile personal chats
wa2vault --profile personal pull --chat "Example Group" --days 30
```

Repeat with `--profile work` for the other isolated account. Pairing requires a
person to scan the QR code. Do not run two writers against the same wacli store.

## Documentation

The documentation is organized by responsibility and maintained with the code:

- [Documentation index](docs/index.md) — choose the right guide.
- [Configuration reference](docs/configuration.md) — profiles, fields,
  precedence, environment variables, and paths.
- [Operations guide](docs/operations.md) — pairing, sync, pull, backups,
  recovery, migration, and troubleshooting.
- [Architecture](docs/architecture.md) — component boundaries, data flow,
  invariants, and failure behavior.
- [wacli contract](docs/internals/wacli-contract.md) — supported commands and
  JSON fields for maintainers.
- [Security policy](SECURITY.md) — private reporting and operational security.
- [Contributing](CONTRIBUTING.md) — local checks and privacy-safe fixtures.
- [Changelog](CHANGELOG.md) — release-facing behavior and migration notes.
- [ADR 0001](docs/adr/0001-local-profile-isolated-archive.md) — why the durable
  archive is local, cumulative, and profile-isolated.

## Development

```bash
git clone https://github.com/frizynn/wa2vault.git
cd wa2vault
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov=wa2vault --cov-fail-under=80
uv build
```

Tests use synthetic identities and temporary state. A real WhatsApp account is
not required and must never be used to create fixtures.

## Limitations

A linked device only exposes history delivered by WhatsApp. Old media may have
expired upstream, and a cumulative archive is not proof of completeness. Local
`faster-whisper` is the supported transcription backend.

## License

MIT. See [LICENSE](LICENSE).
