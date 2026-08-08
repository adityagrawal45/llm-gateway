"""
Loads config/config.yaml into a validated GatewayConfig and watches the file
for changes, hot-swapping the in-memory config without restarting the process.

Usage:
    loader = ConfigLoader(path="config/config.yaml")
    loader.load()                      # initial synchronous load, raises on bad config
    await loader.start_watching()      # background task, call at app startup
    ...
    loader.current                     # always the latest validated GatewayConfig
    await loader.stop_watching()       # call at app shutdown
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from pydantic import ValidationError
from watchfiles import Change, awatch

from llm_gateway.config.schema import GatewayConfig

logger = logging.getLogger("llm_gateway.config")

ReloadCallback = Callable[[GatewayConfig], Awaitable[None] | None]


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or fails schema validation."""


class ConfigLoader:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._config: GatewayConfig | None = None
        self._lock = threading.RLock()
        self._watch_task: asyncio.Task[None] | None = None
        self._on_reload: list[ReloadCallback] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def current(self) -> GatewayConfig:
        """The latest successfully validated config. Raises if load() was never called."""
        with self._lock:
            if self._config is None:
                raise ConfigError(
                    f"config has not been loaded yet; call load() first ({self._path})"
                )
            return self._config

    def on_reload(self, callback: ReloadCallback) -> None:
        """Register a callback invoked with the new GatewayConfig after every successful reload."""
        self._on_reload.append(callback)

    def load(self) -> GatewayConfig:
        """Synchronously (re)load and validate the config file. Raises ConfigError on failure."""
        if not self._path.exists():
            raise ConfigError(f"config file not found: {self._path}")

        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self._path}: {exc}") from exc

        try:
            config = GatewayConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"config validation failed for {self._path}:\n{exc}") from exc

        with self._lock:
            self._config = config

        logger.info("config loaded: %s (%d team(s))", self._path, len(config.teams))
        return config

    async def start_watching(self) -> None:
        """Start a background task that reloads on file changes. Bad reloads are logged, not raised,
        so a typo'd edit doesn't take down a running gateway -- the last-good config stays active."""
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch_loop(), name="config-watch")

    async def stop_watching(self) -> None:
        if self._watch_task is None:
            return
        self._watch_task.cancel()
        try:
            await self._watch_task
        except asyncio.CancelledError:
            pass
        self._watch_task = None

    async def _watch_loop(self) -> None:
        logger.info("watching %s for changes", self._path)
        async for changes in awatch(self._path.parent):
            relevant = any(
                Path(changed_path) == self._path
                for _change_type, changed_path in changes
                if _change_type in (Change.added, Change.modified)
            )
            if not relevant:
                continue

            try:
                config = self.load()
            except ConfigError as exc:
                logger.error("config reload failed, keeping previous config: %s", exc)
                continue

            for callback in self._on_reload:
                result = callback(config)
                if asyncio.iscoroutine(result):
                    await result


_loader: ConfigLoader | None = None


def get_config_loader(path: str | Path | None = None) -> ConfigLoader:
    """Process-wide singleton accessor, mainly for use in FastAPI dependencies."""
    global _loader
    if _loader is None:
        if path is None:
            raise ConfigError("get_config_loader() called before initialization with a path")
        _loader = ConfigLoader(path)
    return _loader
