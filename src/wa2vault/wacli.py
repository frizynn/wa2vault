"""Thin client around the external ``wacli`` binary.

wa2vault never talks to WhatsApp directly; it shells out to ``wacli``
(github.com/openclaw/wacli), which mirrors a linked WhatsApp Web device into a
local SQLite store and exposes JSON output on every command.

This module centralizes process invocation so the rest of wa2vault deals with
parsed JSON, not subprocess plumbing. Every invocation runs with wacli's
read-only guard enabled (``WACLI_READONLY=1``), enforcing wa2vault's
never-send posture: wacli will reject any command that would write to WhatsApp
or mutate the local store.

wacli store location
--------------------
wacli stores its SQLite DB and downloaded media in a single "store directory".
By default this is the platform state dir; on Linux that is the XDG state dir,
``~/.local/state/wacli`` (confirmed via ``wacli doctor --json`` ->
``data.store_dir``). It can be overridden with the ``--store`` flag or the
``WACLI_STORE_DIR`` environment variable; wa2vault threads ``Config.wacli_db``
through as ``--store`` when set.

Only :meth:`WacliClient.list_chats` is implemented in Phase 1 (it backs the
fully-working ``chats`` CLI command). The remaining methods used by the
``pull`` pipeline (sync, message export, media download) are Phase-2 work and
are intentionally left as documented stubs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from wa2vault.config import Config


class WacliError(RuntimeError):
    """Raised when a wacli invocation fails or its output cannot be parsed."""


class WacliClient:
    """Run ``wacli`` subcommands and parse their JSON output.

    Args:
        config: Resolved wa2vault configuration. Provides the wacli binary
            name/path and, optionally, a custom store directory.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Process plumbing
    # ------------------------------------------------------------------ #
    def _base_args(self) -> list[str]:
        """Build the leading argv shared by every invocation (binary + globals)."""
        args = [self.config.wacli_bin, "--read-only", "--json"]
        if self.config.wacli_db is not None:
            args += ["--store", str(self.config.wacli_db)]
        return args

    def _env(self) -> dict[str, str]:
        """Environment for wacli, forcing the read-only guard on."""
        env = dict(os.environ)
        env["WACLI_READONLY"] = "1"
        return env

    def ensure_available(self) -> None:
        """Raise :class:`WacliError` if the wacli binary cannot be found."""
        if shutil.which(self.config.wacli_bin) is None and not os.path.exists(
            self.config.wacli_bin
        ):
            raise WacliError(
                f"wacli binary {self.config.wacli_bin!r} not found on PATH. "
                "Install it (see the wa2vault README) or set 'wacli_bin' in the config."
            )

    def run_json(self, *args: str, timeout: float | None = None) -> Any:
        """Run a wacli subcommand and return its parsed JSON payload.

        wacli wraps successful JSON output as
        ``{"success": true, "data": ..., "error": null}``; this method returns
        the ``data`` field. On a non-zero exit or an error envelope it raises
        :class:`WacliError`.

        Args:
            *args: Subcommand and flags to append after the base argv, e.g.
                ``"chats", "list", "--limit", "500"``.
            timeout: Optional timeout in seconds.

        Returns:
            The decoded ``data`` field of the wacli JSON envelope.
        """
        self.ensure_available()
        argv = self._base_args() + list(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WacliError(f"Failed to run wacli: {exc}") from exc

        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WacliError(
                f"wacli {' '.join(args)} exited with {proc.returncode}: {detail}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise WacliError(
                f"Could not parse wacli JSON output for {' '.join(args)}: {exc}"
            ) from exc

        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success", False):
                raise WacliError(f"wacli reported an error: {payload.get('error')!r}")
            return payload.get("data")
        return payload

    # ------------------------------------------------------------------ #
    # Implemented commands
    # ------------------------------------------------------------------ #
    def list_chats(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the chat list via ``wacli chats list --json``.

        Args:
            limit: Maximum number of chats to return.

        Returns:
            A list of chat dicts as emitted by wacli (shape passed through
            verbatim; the CLI layer selects the fields it displays).
        """
        data = self.run_json("chats", "list", "--limit", str(limit))
        if data is None:
            # wacli returns a null `data` field when there are no chats
            # (e.g. before authentication / sync).
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("chats"), list):
            return data["chats"]
        raise WacliError(
            f"Unexpected 'chats list' payload shape: {type(data).__name__}"
        )


__all__ = ["WacliClient", "WacliError"]
