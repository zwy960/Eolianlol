"""统一诊断日志：时间戳 + 环节标记 + 关键值 + 错误提示。

用法：
    from .diag import log, warn, error
    log("B1", "构建开始", champion="ahri", skin=7)   # -> [HH:MM:SS][B1] 构建开始 champion=ahri skin=7

所有输出同时写 session/diag.log（追加），终端与文件双通道排查。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

_LOG_FILE: Path | None = None
_LISTENERS: list[Callable[[str], None]] = []
_LISTENER_LOCK = threading.Lock()
# safe 策略是否要求日志脱敏（懒加载一次；safe 缺失时按"不脱敏"处理）
_REDACT_ENABLED: bool | None = None


def _should_redact() -> bool:
    global _REDACT_ENABLED
    if _REDACT_ENABLED is None:
        try:
            from .safe.policy import load_policy

            _REDACT_ENABLED = bool(load_policy().redact_logs)
        except Exception:
            _REDACT_ENABLED = False
    return _REDACT_ENABLED


def add_listener(callback: Callable[[str], None]) -> None:
    """订阅实时日志，供应用内终端显示。"""
    with _LISTENER_LOCK:
        if callback not in _LISTENERS:
            _LISTENERS.append(callback)


def remove_listener(callback: Callable[[str], None]) -> None:
    with _LISTENER_LOCK:
        try:
            _LISTENERS.remove(callback)
        except ValueError:
            pass


def _init() -> None:
    global _LOG_FILE
    if _LOG_FILE is not None:
        return
    try:
        from .settings import load_settings

        path = load_settings().session_dir / "diag.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = open(path, "a", encoding="utf-8")
    except OSError:
        _LOG_FILE = None


def _emit(level: str, tag: str, msg: str) -> None:
    _init()
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}][{level}][{tag}] {msg}"
    if _should_redact():
        try:
            from .safe.redact import redact_line

            line = redact_line(line)
        except Exception:
            pass
    if sys.stdout is not None:
        print(line, flush=True)
    if _LOG_FILE:
        _LOG_FILE.write(line + "\n")
        _LOG_FILE.flush()
    with _LISTENER_LOCK:
        listeners = tuple(_LISTENERS)
    for listener in listeners:
        try:
            listener(line)
        except Exception:
            # 日志显示异常不能中断构建或注入流程。
            pass


def log(tag: str, msg: str, **kv) -> None:
    extra = " ".join(f"{k}={v}" for k, v in kv.items())
    _emit("INFO", tag, f"{msg} {extra}".rstrip())


def warn(tag: str, msg: str, **kv) -> None:
    extra = " ".join(f"{k}={v}" for k, v in kv.items())
    _emit("WARN", tag, f"{msg} {extra}".rstrip())


def error(tag: str, msg: str, hint: str | None = None, **kv) -> None:
    extra = " ".join(f"{k}={v}" for k, v in kv.items())
    _emit("ERROR", tag, f"{msg} {extra}".rstrip())
    if hint:
        _emit("HINT", tag, f"建议: {hint}")


def section(tag: str, title: str) -> None:
    _emit("====", tag, title)


def close_log() -> None:
    global _LOG_FILE
    if _LOG_FILE is not None:
        _LOG_FILE.close()
        _LOG_FILE = None
