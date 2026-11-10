"""Eolian 启动引导：路径探测、首次运行设置与启动清理。

从原 float_window.py 迁移而来，与具体 UI 框架解耦：
目录选择通过 ``dir_picker`` 回调注入（Web UI 传入 pywebview 文件夹对话框实现）。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ..settings import (
    DEFAULT_CONFIG_PATH,
    Settings,
    detect_game_dir,
    load_settings,
    save_settings,
)

DirPicker = Callable[[str, str], "str | None"]


def _default_picker(title: str, initialdir: str) -> str | None:
    """未提供 GUI 对话框时的兜底：提示手动编辑 config.json。"""
    print(f"[设置] {title}")
    print(f"[设置] 当前参考目录: {initialdir}")
    print("[设置] 请编辑 config.json 后重新启动（参考 config.example.json）")
    return None


def prepare_settings(dir_picker: DirPicker | None = None) -> Settings | None:
    """首次运行自动探测；探测失败时通过 dir_picker 让用户选择目录。

    返回 None 表示用户取消或路径仍无效。
    """
    picker = dir_picker or _default_picker
    settings = load_settings()
    changed = not DEFAULT_CONFIG_PATH.is_file() or bool(getattr(sys, "frozen", False))

    if not settings.valid_client:
        selected = picker(
            "选择英雄联盟客户端目录（包含 LeagueClient / Game）",
            str(settings.client_root.parent),
        )
        if not selected:
            return None
        client_root = Path(selected)
        if (client_root / "Game" / "DATA" / "FINAL").is_dir():
            settings = replace(settings, client_root=client_root)
        elif (client_root.parent / "Game" / "DATA" / "FINAL").is_dir():
            client_root = client_root.parent
            settings = replace(settings, client_root=client_root)
        else:
            settings = replace(settings, client_root=client_root)
        settings = replace(settings, game_dir=detect_game_dir(settings.client_root))
        changed = True

    if not settings.valid_game:
        selected = picker(
            "选择英雄联盟 Game 目录（包含 DATA/FINAL）",
            str(settings.client_root / "Game"),
        )
        if not selected:
            return None
        game_dir = Path(selected)
        if (game_dir / "Game" / "DATA" / "FINAL").is_dir():
            game_dir = game_dir / "Game"
        settings = replace(settings, game_dir=game_dir)
        changed = True

    if not settings.valid_game:
        print(f"[设置] 游戏目录无效: {settings.game_dir}")
        return None
    if not settings.valid_builder:
        print(f"[设置] 找不到 eolian-overlay.exe: {settings.learn_overlay}")
        return None
    if changed:
        try:
            save_settings(settings)
        except OSError:
            pass
    return settings


def startup_housekeeping(settings) -> None:
    """启动清理：脱敏上一份日志 + 清理 onefile 残留解包目录。

    必须在任何 ``log()`` 之前跑：diag 一旦打开 diag.log，Windows 上就换不掉它。
    """
    try:
        from ..diag import log, warn
        from ..safe import redact_previous_logs
        from ..safe.footprint import clean_stale_mei

        report = redact_previous_logs(settings.session_dir)
        for finding in report.findings:
            emit = log if finding.level in ("ok", "info") else warn
            emit("SAFE", f"{finding.code} {finding.message}",
                 **{k: str(v)[:120] for k, v in finding.detail.items()})
        removed = clean_stale_mei()
        if removed:
            log("SAFE", "已清理 onefile 残留解包目录", removed=removed)
    except Exception as exc:  # noqa: BLE001 - 启动清理不能挡住 UI
        try:
            from ..diag import warn

            warn("SAFE", "启动清理失败", error=type(exc).__name__)
        except Exception:
            pass
