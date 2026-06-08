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
from wa2vault.wacli import WacliClient, WacliError

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


@app.command()
def auth(ctx: typer.Context) -> None:
    """Pair your phone with wacli (QR), so wa2vault can read your chats.

    Runs ``wacli auth``, which shows a QR code; scan it from your phone under
    Settings -> Linked Devices. wa2vault is READ-ONLY and NEVER sends messages;
    pairing only lets it mirror and read your message history locally.
    """
    config = _config(ctx)
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
) -> None:
    """List chats so you can find the exact name/JID to pass to `pull`.

    Calls ``wacli chats list --json`` and prints a clean table of
    name, type, and JID.
    """
    config = _config(ctx)
    client = WacliClient(config)
    try:
        rows = client.list_chats(limit=limit)
    except WacliError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not rows:
        typer.echo("No chats found. Run `wa2vault auth` then `wa2vault sync` first.")
        raise typer.Exit(code=0)

    _print_chats_table(rows)


def _print_chats_table(rows: list[dict]) -> None:
    """Print chats as an aligned NAME / TYPE / JID table."""

    def field(row: dict, *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value:
                return str(value)
        return ""

    table = [
        (
            field(row, "name", "display_name", "subject") or "(unnamed)",
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


__all__ = ["app"]
