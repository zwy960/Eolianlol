"""项目路径配置与自动探测。

优先级：环境变量 > config.json > 常见安装目录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
APP_DATA_DIR = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
) / "Eolian"
DEFAULT_CONFIG_PATH = APP_DATA_DIR / "config.json"
DEFAULT_SESSION_DIR = APP_DATA_DIR / "session"

CLIENT_CANDIDATES = (
    Path(r"E:\WeGameApps\英雄联盟"),
    Path(r"E:\Program Files (x86)\英雄联盟(26)"),
    Path(r"C:\Riot Games\League of Legends"),
)
GAME_CANDIDATES = (
    Path(r"E:\WeGameApps\英雄联盟\Game"),
    Path(r"E:\Program Files (x86)\英雄联盟(26)\Game"),
    Path(r"C:\Riot Games\League of Legends\Game"),
)
OVERLAY_CANDIDATES = (
    RESOURCE_ROOT / "resources" / "eolian-overlay.exe",
    Path(r"C:\eolian-target\release\eolian-overlay.exe"),
    SOURCE_ROOT / "eolian-overlay" / "target" / "release" / "eolian-overlay.exe",
)


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SOURCE_ROOT / path
    return path.resolve()


def _read_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _looks_like_client(path: Path) -> bool:
    return path.is_dir() and (
        (path / "lockfile").is_file()
        or (path / "LeagueClient" / "lockfile").is_file()
        or (path / "LeagueClient").is_dir()
    )


def _looks_like_game(path: Path) -> bool:
    return path.is_dir() and (path / "DATA" / "FINAL").is_dir()


def detect_client_root() -> Path:
    override = _env("EOLIAN_CLIENT_DIR", "EOLIAN_LEARN_CLIENT_DIR")
    if override:
        return _resolve(override)
    for candidate in CLIENT_CANDIDATES:
        if _looks_like_client(candidate):
            return candidate
    return CLIENT_CANDIDATES[0]


def detect_game_dir(client_root: Path | None = None) -> Path:
    override = _env("EOLIAN_GAME_DIR")
    if override:
        return _resolve(override)
    if client_root is not None and _looks_like_game(client_root / "Game"):
        return _resolve(client_root / "Game")
    for candidate in GAME_CANDIDATES:
        if _looks_like_game(candidate):
            return candidate
    return GAME_CANDIDATES[0]


def detect_learn_overlay() -> Path:
    override = _env("EOLIAN_LEARN_OVERLAY")
    if override:
        return _resolve(override)
    bundled = RESOURCE_ROOT / "resources" / "eolian-overlay.exe"
    if getattr(sys, "frozen", False) and bundled.is_file():
        return bundled
    for candidate in OVERLAY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return OVERLAY_CANDIDATES[0]


@dataclass(frozen=True)
class Settings:
    client_root: Path
    game_dir: Path
    session_dir: Path
    learn_overlay: Path
    overlay_ascii_dir: Path

    @property
    def valid_client(self) -> bool:
        return _looks_like_client(self.client_root)

    @property
    def valid_game(self) -> bool:
        return _looks_like_game(self.game_dir)

    @property
    def valid_builder(self) -> bool:
        return self.learn_overlay.is_file()


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = _resolve(config_path or _env("EOLIAN_CONFIG") or DEFAULT_CONFIG_PATH)
    config = _read_config(path)

    client_value = (
        _env("EOLIAN_CLIENT_DIR", "EOLIAN_LEARN_CLIENT_DIR")
        or config.get("client_dir")
        or ""
    )
    client_root = _resolve(client_value) if client_value else detect_client_root()

    game_value = _env("EOLIAN_GAME_DIR") or config.get("game_dir") or ""
    game_dir = _resolve(game_value) if game_value else detect_game_dir(client_root)

    session_value = (
        _env("EOLIAN_SESSION_DIR")
        or config.get("session_dir")
        or str(DEFAULT_SESSION_DIR)
    )
    session_dir = _resolve(session_value)

    overlay_value = _env("EOLIAN_LEARN_OVERLAY") or config.get("learn_overlay") or ""
    if getattr(sys, "frozen", False) and (
        RESOURCE_ROOT / "resources" / "eolian-overlay.exe"
    ).is_file():
        learn_overlay = RESOURCE_ROOT / "resources" / "eolian-overlay.exe"
    else:
        learn_overlay = _resolve(overlay_value) if overlay_value else detect_learn_overlay()

    ascii_value = (
        _env("EOLIAN_OVERLAY_ASCII_DIR")
        or config.get("overlay_ascii_dir")
        or r"C:\eolian-overlay-current"
    )
    overlay_ascii_dir = _resolve(ascii_value)
    return Settings(
        client_root=client_root,
        game_dir=game_dir,
        session_dir=session_dir,
        learn_overlay=learn_overlay,
        overlay_ascii_dir=overlay_ascii_dir,
    )


def save_settings(settings: Settings, config_path: str | Path | None = None) -> Path:
    path = _resolve(config_path or _env("EOLIAN_CONFIG") or DEFAULT_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    learn_overlay = settings.learn_overlay
    try:
        learn_overlay.relative_to(RESOURCE_ROOT)
        persisted_overlay = ""
    except ValueError:
        persisted_overlay = str(learn_overlay)
    payload = {
        "client_dir": str(settings.client_root),
        "game_dir": str(settings.game_dir),
        "session_dir": str(settings.session_dir),
        "learn_overlay": persisted_overlay,
        "overlay_ascii_dir": str(settings.overlay_ascii_dir),
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return path
