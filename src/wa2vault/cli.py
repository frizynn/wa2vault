"""wa2vault command-line interface.

A Typer app exposing the wa2vault commands:

- ``auth``       -- pair the phone via wacli QR (read-only posture).
- ``sync``       -- refresh the local wacli store.
- ``chats``      -- list chats so the user can find a name/JID for ``pull``.
- ``pull``       -- the main command: export a chat window, transcribe voice
                    notes, and render a Markdown note into the vault.
- ``transcribe`` -- one-off: transcribe a single audio file and print the text.

The CLI loads configuration once (with an optional ``--config`` override),
delegates WhatsApp data access to ``wacli`` via
:class:`~wa2vault.wacli.WacliClient`, and calls into the Phase-2 pipeline in
:mod:`wa2vault.pipeline` for the data-heavy work.

wa2vault never sends WhatsApp messages. Read/query wacli commands run with the
read-only guard (``WACLI_READONLY``); ``auth`` and ``sync`` run with it off,
because they must write the local store (pair the device / mirror messages).
The never-send guarantee holds regardless: wa2vault never invokes ``wacli send``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer

from wa2vault import __version__
from wa2vault.config import Config
from wa2vault.contacts import ContactBook, pretty_phone
from wa2vault.lock import find_store_lock
from wa2vault.wacli import WacliClient, WacliError, _is_placeholder_group

app = typer.Typer(
    name="wa2vault",
    help=(
        "Read-only WhatsApp chat exporter: pull a chat window via wacli, "
        "transcribe voice notes locally, and write Markdown into an Obsidian vault. "
        "Never sends messages."
    ),
    no_args_is_help=True,
    add_completion=False,
)

# Stored on the Typer context so commands can read the resolved config and the
# optional --config path chosen by the user.
_CONFIG_OBJ_KEY = "config"

contact_app = typer.Typer(
    help="Manage local contact names (when WhatsApp's contact names didn't sync).",
    no_args_is_help=True,
)
app.add_typer(contact_app, name="contact")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wa2vault {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            help="Path to the wa2vault TOML config file (defaults to the user config dir).",
            dir_okay=False,
        ),
    ] = None,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the wa2vault version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Load configuration and stash it on the context for subcommands."""
    config = Config.load(config_path)
    ctx.obj = {_CONFIG_OBJ_KEY: config}


def _config(ctx: typer.Context) -> Config:
    """Fetch the resolved :class:`Config` from the Typer context."""
    return ctx.obj[_CONFIG_OBJ_KEY]


def _run_wacli_passthrough(
    config: Config, args: list[str], *, read_only: bool = True
) -> int:
    """Run a wacli subcommand attached to the user's terminal (no capture).

    Used by ``auth`` and ``sync`` where wacli renders a QR code or streams sync
    progress directly to the terminal. ``read_only`` toggles wacli's read-only
    guard (``WACLI_READONLY``); it must be False for ``auth``/``sync``, which
    write the local store (pairing keys / mirrored messages).

    Returns the wacli process exit code.
    """
    env = dict(os.environ)
    if read_only:
        env["WACLI_READONLY"] = "1"
    else:
        env.pop("WACLI_READONLY", None)
    argv = [config.wacli_bin]
    if config.wacli_db is not None:
        argv += ["--store", str(config.wacli_db)]
    argv += args
    try:
        proc = subprocess.run(argv, env=env, check=False)
    except OSError as exc:
        typer.secho(f"Failed to run wacli: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    return proc.returncode


def _abort_if_store_locked(config: Config, *, action: str) -> None:
    """Refuse to start a wacli writer if another instance already holds the lock.

    wacli is a single writer: only one process may sync/pair against the store
    at a time. Starting a second one just races for an exclusive lock it cannot
    get. When another live instance is detected we stop here with a clear error
    instead, so concurrent runs (e.g. two agents) back off cleanly rather than
    thrashing on the lock. See :mod:`wa2vault.lock`.
    """
    held = find_store_lock(config)
    if held is None:
        return
    typer.secho(
        f"error: another wa2vault/wacli instance is already running ({held.describe()}).",
        fg=typer.colors.RED,
        err=True,
    )
    typer.secho(
        f"warning: wacli allows a single writer on the store, so this {action} "
        "will not start (refusing to avoid a store-lock conflict). Wait for the "
        "other run to finish or stop it, then retry.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def auth(ctx: typer.Context) -> None:
    """Pair your phone with wacli (QR), so wa2vault can read your chats.

    Runs ``wacli auth``, which shows a QR code; scan it from your phone under
    Settings -> Linked Devices. wa2vault is READ-ONLY and NEVER sends messages;
    pairing only lets it mirror and read your message history locally.
    """
    config = _config(ctx)
    _abort_if_store_locked(config, action="pairing")
    typer.secho(
        "wa2vault is READ-ONLY: it never sends WhatsApp messages.",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        "Starting wacli pairing. Scan the QR with your phone "
        "(WhatsApp -> Settings -> Linked Devices)."
    )
    code = _run_wacli_passthrough(config, ["auth"], read_only=False)
    raise typer.Exit(code=code)


@app.command()
def sync(
    ctx: typer.Context,
    idle: Annotated[
        int,
        typer.Option(
            "--idle",
            min=5,
            help=(
                "Seconds of inactivity before sync exits (wacli --idle-exit). "
                "Raise it (e.g. --idle 180) to capture more history per run."
            ),
        ),
    ] = 30,
    media: Annotated[
        bool,
        typer.Option(
            "--media/--no-media",
            help="Also download media in the background while syncing.",
        ),
    ] = False,
    history: Annotated[
        bool,
        typer.Option(
            "--history/--no-history",
            help="Also show local history coverage after syncing (best-effort).",
        ),
    ] = False,
) -> None:
    """Refresh the local wacli store (incremental sync until idle).

    Runs ``wacli sync --once`` to pull new messages and exit after ``--idle``
    seconds of inactivity. WhatsApp delivers history in batches, so run this a
    few times (or raise ``--idle``) to accumulate more. ``--media`` downloads
    media during the sync; ``--history`` prints how far back your archive reaches.
    """
    config = _config(ctx)
    _abort_if_store_locked(config, action="sync")
    args = ["sync", "--once", "--idle-exit", f"{idle}s"]
    if media:
        args.append("--download-media")
    code = _run_wacli_passthrough(config, args, read_only=False)
    if code != 0:
        raise typer.Exit(code=code)
    if history:
        # `history coverage` is a pure read, so the read-only guard stays on.
        _run_wacli_passthrough(config, ["history", "coverage"])
    raise typer.Exit(code=0)


@app.command()
def chats(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, help="Maximum number of chats to list."),
    ] = 100,
    refresh_groups: Annotated[
        bool,
        typer.Option(
            "--refresh-groups/--no-refresh-groups",
            help=(
                "Fetch joined groups live before listing, to recover group names "
                "WhatsApp's app-state sync never delivered (slower; hits the "
                "network)."
            ),
        ),
    ] = False,
) -> None:
    """List chats so you can find the exact name/JID to pass to `pull`.

    Calls ``wacli chats list --json`` and prints a clean table of name, type,
    and JID. Group names that are missing from the chat list (a common result of
    WhatsApp app-state sync failing after pairing) are backfilled from wacli's
    group table; pass ``--refresh-groups`` to fetch them live first.
    """
    config = _config(ctx)
    client = WacliClient(config)
    if refresh_groups:
        try:
            client.refresh_groups(timeout=config.sync_timeout)
        except WacliError as exc:
            typer.secho(
                f"warning: could not refresh groups: {exc}",
                fg=typer.colors.YELLOW,
                err=True,
            )
    try:
        rows = client.list_chats(limit=limit)
    except WacliError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not rows:
        typer.echo("No chats found. Run `wa2vault auth` then `wa2vault sync` first.")
        raise typer.Exit(code=0)

    book = ContactBook(config.contacts_file)
    _print_chats_table(rows, book, _safe_group_names(client))


def _safe_group_names(client: WacliClient) -> dict[str, str]:
    """Best-effort group JID -> name map; never raises so `chats` still lists on error."""
    try:
        return client.group_names()
    except WacliError:
        return {}


def _display_name(
    row: dict,
    jid: str,
    raw_name: str,
    book: ContactBook,
    group_names: dict[str, str],
) -> str:
    """Pick the best display name for a chat row.

    For groups, the group table's subject (``group_names``) is authoritative and
    overrides the ``chats list`` name, which is unreliable -- it may be missing,
    echo the JID (a WhatsApp app-state sync failure), or even carry a
    participant's name instead of the subject. A group with neither a real
    chat-list name nor a known subject falls back to ``(unnamed group)``. For DM
    chats whose name echoes the JID/phone, fall back to a saved contact name,
    then to a readable phone. Channels keep their real names untouched.
    """
    if jid.endswith("@g.us"):
        subject = group_names.get(jid)
        if subject:
            return subject
        if _is_placeholder_group(jid, raw_name or None):
            return "(unnamed group)"
        return raw_name or "(unnamed group)"

    if not jid.endswith("@s.whatsapp.net"):
        return raw_name or "(unnamed)"

    phone = pretty_phone(jid)
    name_is_placeholder = not raw_name or raw_name in {jid, phone}
    if name_is_placeholder:
        return book.name_for(jid) or phone
    return raw_name


def _print_chats_table(
    rows: list[dict], book: ContactBook, group_names: dict[str, str]
) -> None:
    """Print chats as an aligned NAME / TYPE / JID table."""

    def field(row: dict, *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value:
                return str(value)
        return ""

    table = [
        (
            _display_name(
                row,
                field(row, "jid", "chat_jid", "id"),
                field(row, "name", "display_name", "subject"),
                book,
                group_names,
            ),
            field(row, "type", "chat_type") or "-",
            field(row, "jid", "chat_jid", "id"),
        )
        for row in rows
    ]

    name_w = max((len(name) for name, _, _ in table), default=4)
    name_w = min(max(name_w, len("NAME")), 40)
    type_w = max((len(t) for _, t, _ in table), default=4)
    type_w = max(type_w, len("TYPE"))

    header = f"{'NAME':<{name_w}}  {'TYPE':<{type_w}}  JID"
    typer.secho(header, bold=True)
    typer.echo("-" * len(header))
    for name, chat_type, jid in table:
        display_name = name if len(name) <= name_w else name[: name_w - 1] + "…"
        typer.echo(f"{display_name:<{name_w}}  {chat_type:<{type_w}}  {jid}")


@app.command()
def pull(
    ctx: typer.Context,
    chat: Annotated[
        str,
        typer.Option("--chat", help="Chat name or JID to pull (see `wa2vault chats`)."),
    ],
    days: Annotated[
        Optional[int],
        typer.Option(
            "--days", min=1, help="Days of history to include (default from config)."
        ),
    ] = None,
    transcribe: Annotated[
        bool,
        typer.Option(
            "--transcribe/--no-transcribe",
            help="Transcribe voice notes (default on).",
        ),
    ] = True,
    media: Annotated[
        bool,
        typer.Option(
            "--media/--no-media",
            help="Download/locate images and voice notes (default on).",
        ),
    ] = True,
) -> None:
    """Pull the last N days of a chat and write a Markdown note into the vault.

    Flow: sync the local store -> export the last N days of the chat from wacli
    -> download/locate media -> transcribe voice notes (ptt/audio) via the
    configured ASR backend, using a per-message transcript cache -> render a
    Markdown note into ``vault_dir/output_subdir``.

    Use ``--no-transcribe`` to skip ASR and ``--no-media`` to skip media.
    """
    config = _config(ctx)
    window_days = days if days is not None else config.default_days

    from wa2vault import pipeline
    from wa2vault.wacli import ChatNotFound, ChatNotUnique

    try:
        result = pipeline.pull_chat(
            config=config,
            chat=chat,
            days=window_days,
            transcribe=transcribe,
            download_media=media,
        )
    except (ChatNotFound, ChatNotUnique) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except WacliError as exc:
        typer.secho(f"wacli error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(str(result), fg=typer.colors.GREEN)
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)


@app.command()
def transcribe(
    ctx: typer.Context,
    audio_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Path to the audio file to transcribe.",
        ),
    ],
    language: Annotated[
        Optional[str],
        typer.Option(
            "--language",
            help="Language hint (ISO-639-1, e.g. 'es'). Defaults to config.",
        ),
    ] = None,
) -> None:
    """Transcribe a single audio file with the configured ASR backend and print the text.

    Wires directly to the transcription contract, so this works as soon as the
    Phase-2 backend is implemented.
    """
    config = _config(ctx)
    lang = language if language is not None else config.language

    from wa2vault.transcribe import get_transcriber

    transcriber = get_transcriber(config)
    try:
        result = transcriber.transcribe(audio_path, language=lang)
    except NotImplementedError as exc:
        typer.secho(
            f"ASR backend '{config.asr_backend}' is not implemented yet: {exc}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(result.text)


@contact_app.command("add")
def contact_add(
    ctx: typer.Context,
    number: Annotated[
        str,
        typer.Argument(help="Phone number (any format) or full JID."),
    ],
    name: Annotated[
        str,
        typer.Argument(help="Friendly name to store for this contact."),
    ],
) -> None:
    """Save a local name for a number/JID, so chats show it instead of digits."""
    config = _config(ctx)
    book = ContactBook(config.contacts_file)
    try:
        jid = book.set(number, name)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f'Saved "{name.strip()}" -> {pretty_phone(jid)}', fg=typer.colors.GREEN)


@contact_app.command("list")
def contact_list(ctx: typer.Context) -> None:
    """List saved contacts as a NAME -> PHONE table."""
    config = _config(ctx)
    book = ContactBook(config.contacts_file)
    entries = book.items()
    if not entries:
        typer.echo("No saved contacts yet.")
        return

    pairs = sorted(
        ((name, pretty_phone(jid)) for jid, name in entries.items()),
        key=lambda pair: pair[0].lower(),
    )
    name_w = max(len(name) for name, _ in pairs)
    name_w = max(name_w, len("NAME"))

    header = f"{'NAME':<{name_w}}  PHONE"
    typer.secho(header, bold=True)
    typer.echo("-" * len(header))
    for name, phone in pairs:
        typer.echo(f"{name:<{name_w}}  {phone}")


@contact_app.command("rm")
def contact_rm(
    ctx: typer.Context,
    query: Annotated[
        str,
        typer.Argument(help="Number, JID, or saved name to remove."),
    ],
) -> None:
    """Remove a saved contact by number/JID or by exact name."""
    config = _config(ctx)
    book = ContactBook(config.contacts_file)
    if book.remove(query):
        typer.secho(f"Removed {query!r}.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No saved contact matched {query!r}.", fg=typer.colors.YELLOW)


__all__ = ["app"]
