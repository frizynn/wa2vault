# Configuration reference

[Project home](../README.md) · [Documentation](index.md) ·
[Operations](operations.md) · [Architecture](architecture.md)

wa2vault loads one TOML file, selects one named profile, applies environment
overrides, and validates the complete result before running a command.

## Location and precedence

The default file is `config.toml` in the operating system's user configuration
directory for `wa2vault`. Pass `--config FILE` before the command to use another
file. The default file is created when a non-help command first loads it.

Precedence, from lowest to highest:

1. built-in defaults;
2. top-level TOML values;
3. the selected `[profiles.NAME]` block;
4. `WA2VAULT_*` environment variables;
5. CLI `--profile`, which selects the profile instead of
   `WA2VAULT_PROFILE` or the top-level `profile` value.

Environment variables other than `WA2VAULT_PROFILE` still override fields in
the profile selected by the CLI.

## Recommended two-profile configuration

```toml
vault_dir = "/path/to/private/notes"
output_subdir = "Chats"
wacli_bin = "wacli"
state_dir = "/path/to/private/wa2vault-state"
cache_dir = "/path/to/private/wa2vault-cache"
contacts_file = "/path/to/private/wa2vault-config/contacts.json"

asr_backend = "faster-whisper"
asr_model = "medium"
language = "es"
default_days = 30

sync_timeout = 300.0
command_timeout = 120.0
media_timeout = 180.0
ffmpeg_timeout = 120.0
allow_git_vault = false

[profiles.personal]
wacli_account = "personal"

[profiles.work]
wacli_account = "work"
language = "en"
```

Select a configured profile on every operational command:

```bash
wa2vault --profile personal chats
wa2vault --profile work pull --chat "Example Group" --days 14
```

An unknown profile fails before wacli is invoked. A non-default profile always
gets its own derived state, cache, contacts, note, and media namespace.

## Field reference

| TOML field | Default | Environment variable | Profile override | Meaning |
| --- | --- | --- | --- | --- |
| `profile` | `default` | `WA2VAULT_PROFILE` | No | Profile selected when the CLI omits `--profile`. |
| `vault_dir` | `~/Obsidian/wa2vault` | `WA2VAULT_VAULT_DIR` | Yes | Root for rendered notes and copied media. |
| `output_subdir` | `Chats` | `WA2VAULT_OUTPUT_SUBDIR` | Yes | Relative directory inside `vault_dir`; absolute paths and `..` are rejected. |
| `wacli_bin` | `wacli` | `WA2VAULT_WACLI_BIN` | No | Executable name or absolute path. |
| `wacli_account` | unset | `WA2VAULT_WACLI_ACCOUNT` | Yes | Named wacli account passed as `--account`. |
| `wacli_db` | unset | `WA2VAULT_WACLI_DB` | Yes | Explicit store passed as `--store`. |
| `asr_backend` | `faster-whisper` | `WA2VAULT_ASR_BACKEND` | Yes | Local transcription backend. |
| `asr_model` | `medium` | `WA2VAULT_ASR_MODEL` | Yes | faster-whisper model name or local model path. |
| `language` | `es` | `WA2VAULT_LANGUAGE` | Yes | ISO-639-1 transcription hint. |
| `default_days` | `30` | `WA2VAULT_DEFAULT_DAYS` | Yes | Pull window when `--days` is omitted; must be positive. |
| `sync_timeout` | `300.0` | `WA2VAULT_SYNC_TIMEOUT` | Yes | Best-effort sync bound in seconds; blank/`none` uses `command_timeout`. |
| `command_timeout` | `120.0` | `WA2VAULT_COMMAND_TIMEOUT` | Yes | Bound for ordinary non-interactive wacli commands. |
| `media_timeout` | `180.0` | `WA2VAULT_MEDIA_TIMEOUT` | Yes | Bound for one media download. |
| `ffmpeg_timeout` | `120.0` | `WA2VAULT_FFMPEG_TIMEOUT` | Yes | Bound for decoding one audio file. |
| `cache_dir` | platform cache directory | `WA2VAULT_CACHE_DIR` | Yes | Disposable transcript and downloaded-media cache root. |
| `state_dir` | platform state directory | `WA2VAULT_STATE_DIR` | Yes | Durable cumulative archive root. |
| `contacts_file` | platform config directory | `WA2VAULT_CONTACTS_FILE` | Yes | Local JID-to-display-name alias file. |
| `allow_git_vault` | `false` | `WA2VAULT_ALLOW_GIT_VAULT` | Yes | Explicit opt-in to write private output inside a Git worktree. |

`wacli_account` and `wacli_db` are mutually exclusive. Setting both in TOML or
the environment is an error. If a profile defines one, it replaces the
top-level source. If it defines neither, it inherits the top-level source or
wacli's default.

Boolean environment values `1`, `true`, `yes`, and `on` enable
`allow_git_vault`; other values disable it. Numeric overrides are parsed and
then validated with the same positive-value constraints as TOML.

## Generated paths

`<profile-key>` contains a readable slug plus a stable hash. `<chat-key>`
contains a display-name slug plus a hash of profile and JID. Full JIDs are not
placed in filenames.

```text
<vault_dir>/<output_subdir>/<profile-key>/<chat-key>.md
<vault_dir>/<output_subdir>/_media/<profile-key>/<chat-key>/<message-hash>--<name>
<state_dir>/profiles/<profile-key>/vaults/<vault-hash>/archive.sqlite3
<cache_dir>/profiles/<profile-key>/transcripts.sqlite3
<cache_dir>/profiles/<profile-key>/media/<chat-key>/...
```

The legacy `default` profile keeps the top-level state, cache, and contacts
locations for compatibility. Named profiles use the `profiles/<profile-key>`
namespace. Archive state also includes a vault hash so two destination vaults
cannot accidentally share one accumulated timeline.

Do not place any of these roots in the wa2vault source checkout. See the
[operations guide](operations.md) for backup and migration behavior.
