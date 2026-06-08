"""Configuration loading for wa2vault.

Configuration lives in a single TOML file under the platform user-config
directory (via :mod:`platformdirs`), e.g. on Linux::

    ~/.config/wa2vault/config.toml

Values can be overridden, in increasing order of precedence:

    1. Built-in defaults (this module).
    2. The TOML config file.
    3. ``WA2VAULT_*`` environment variables.
    4. CLI flags (applied by the CLI layer after :meth:`Config.load`).

:meth:`Config.load` reads the TOML file, creating it with sane defaults the
first time it is missing, and applies environment overrides.

Only the standard-library :mod:`tomllib` is used for reading; the default file
is written with a tiny purpose-built serializer so wa2vault keeps its
dependency surface minimal (no TOML-writer dependency).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_NAME = "wa2vault"
APP_AUTHOR = "wa2vault"

AsrBackend = Literal["faster-whisper", "nemotron"]


def default_config_path() -> Path:
    """Return the path to the user config TOML file."""
    return Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR)) / "config.toml"


def default_cache_dir() -> Path:
    """Return the default cache/work directory (transcripts + scratch)."""
    return Path(platformdirs.user_cache_dir(APP_NAME, APP_AUTHOR))


def default_vault_dir() -> Path:
    """Return the default Obsidian vault output directory."""
    return Path.home() / "Obsidian" / "wa2vault"


class Config(BaseModel):
    """Resolved wa2vault configuration.

    Field defaults match the documented defaults. Paths are stored as absolute
    :class:`~pathlib.Path` objects.
    """

    model_config = ConfigDict(extra="forbid")

    vault_dir: Path = Field(
        default_factory=default_vault_dir,
        description="Obsidian vault root where exported chat notes are written.",
    )
    output_subdir: str = Field(
        default="Chats",
        description="Subdirectory inside the vault for wa2vault chat notes.",
    )
    wacli_bin: str = Field(
        default="wacli",
        description="wacli executable name or absolute path.",
    )
    wacli_db: Path | None = Field(
        default=None,
        description=(
            "Path to wacli's SQLite store directory. When None, wacli's own "
            "default is used (on Linux the XDG state dir: "
            "~/.local/state/wacli). Set this to point wacli at a custom store."
        ),
    )
    asr_backend: AsrBackend = Field(
        default="faster-whisper",
        description="ASR backend used to transcribe voice notes.",
    )
    asr_model: str = Field(
        default="medium",
        description="ASR model name (e.g. a faster-whisper model size like 'medium').",
    )
    language: str = Field(
        default="es",
        description="Default language hint for transcription (ISO-639-1 code).",
    )
    default_days: int = Field(
        default=30,
        ge=1,
        description="Default number of days of history to pull when --days is omitted.",
    )
    cache_dir: Path = Field(
        default_factory=default_cache_dir,
        description="Directory for the transcript cache and scratch/work files.",
    )

    @field_validator("wacli_db", mode="before")
    @classmethod
    def _empty_db_path_is_none(cls, value: object) -> object:
        """Treat an empty/whitespace ``wacli_db`` value as None.

        The default TOML file serializes ``wacli_db = None`` as an empty string
        (TOML has no null literal); on load that empty string must round-trip
        back to None rather than becoming ``Path("")``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        """Load configuration, creating a default file if missing.

        Args:
            config_path: Explicit path to the TOML config file. Defaults to
                :func:`default_config_path`.

        Returns:
            A fully resolved :class:`Config` with environment overrides applied.
        """
        path = config_path or default_config_path()

        if not path.exists():
            cls._write_default(path)

        with path.open("rb") as fh:
            data = tomllib.load(fh)

        config = cls.model_validate(data)
        config = config._with_env_overrides()
        return config

    def _with_env_overrides(self) -> Config:
        """Return a copy with ``WA2VAULT_*`` environment overrides applied."""
        overrides: dict[str, object] = {}

        env_str = {
            "WA2VAULT_OUTPUT_SUBDIR": "output_subdir",
            "WA2VAULT_WACLI_BIN": "wacli_bin",
            "WA2VAULT_ASR_BACKEND": "asr_backend",
            "WA2VAULT_ASR_MODEL": "asr_model",
            "WA2VAULT_LANGUAGE": "language",
        }
        for env_key, field in env_str.items():
            value = os.environ.get(env_key)
            if value:
                overrides[field] = value

        env_path = {
            "WA2VAULT_VAULT_DIR": "vault_dir",
            "WA2VAULT_WACLI_DB": "wacli_db",
            "WA2VAULT_CACHE_DIR": "cache_dir",
        }
        for env_key, field in env_path.items():
            value = os.environ.get(env_key)
            if value:
                overrides[field] = Path(value).expanduser()

        days = os.environ.get("WA2VAULT_DEFAULT_DAYS")
        if days:
            overrides["default_days"] = int(days)

        if not overrides:
            return self
        return self.model_copy(update=overrides)

    @classmethod
    def _write_default(cls, path: Path) -> None:
        """Write a default TOML config file at ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = cls()
        path.write_text(defaults._to_toml(), encoding="utf-8")

    def _to_toml(self) -> str:
        """Serialize this config to a flat TOML document.

        A tiny serializer is used deliberately: all fields are scalars
        (strings, ints, or paths/None), so a single flat table suffices and we
        avoid adding a TOML-writer dependency.
        """
        lines = [
            "# wa2vault configuration",
            "# Edit values below; missing keys fall back to built-in defaults.",
            "# Environment variables WA2VAULT_* override these at runtime.",
            "",
        ]
        for name, value in self._toml_items():
            lines.append(f"{name} = {value}")
        return "\n".join(lines) + "\n"

    def _toml_items(self) -> list[tuple[str, str]]:
        """Yield ``(key, toml_literal)`` pairs for serialization."""
        items: list[tuple[str, str]] = [
            ("vault_dir", _toml_str(str(self.vault_dir))),
            ("output_subdir", _toml_str(self.output_subdir)),
            ("wacli_bin", _toml_str(self.wacli_bin)),
            (
                "wacli_db",
                _toml_str(str(self.wacli_db)) if self.wacli_db is not None else '""',
            ),
            ("asr_backend", _toml_str(self.asr_backend)),
            ("asr_model", _toml_str(self.asr_model)),
            ("language", _toml_str(self.language)),
            ("default_days", str(self.default_days)),
            ("cache_dir", _toml_str(str(self.cache_dir))),
        ]
        return items


def _toml_str(value: str) -> str:
    """Quote a string as a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


__all__ = [
    "Config",
    "AsrBackend",
    "default_config_path",
    "default_cache_dir",
    "default_vault_dir",
]
