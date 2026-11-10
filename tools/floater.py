"""模式一悬浮窗入口：python -m tools.floater"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def _load_ui_main():
    """优先加载 Web UI（pywebview）；不可用时回退旧 Tk 界面。"""
    try:
        import webview  # noqa: F401 - 探测 pywebview 是否可用

        from eolian.ui.web_window import main

        return main, "webview"
    except ImportError:
        from eolian.ui.float_window import main

        return main, "tkinter-fallback"


UI_MAIN, UI_BACKEND = _load_ui_main()


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return True


def relaunch_as_admin() -> bool:
    import ctypes
    import subprocess

    frozen = bool(getattr(sys, "frozen", False))
    target = sys.executable
    parameters = "" if frozen else subprocess.list2cmdline(["-m", "tools.floater"])
    working_dir = Path(sys.executable).resolve().parent if frozen else PROJECT_ROOT
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", target, parameters, str(working_dir), 1
    )
    return result > 32


def ensure_console_log() -> object | None:
    """窗口模式没有控制台时，把 stdout/stderr 接到用户日志。"""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        from eolian.settings import APP_DATA_DIR

        path = APP_DATA_DIR / "session" / "floater-console.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return stream


if __name__ == "__main__":
    console_log = ensure_console_log()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    import os

    if (
        not is_admin()
        and os.environ.get("EOLIAN_NO_ELEVATE", "").strip() != "1"
    ):
        if relaunch_as_admin():
            raise SystemExit(0)
        print("需要管理员权限，UAC 授权被拒绝", file=sys.stderr)
        raise SystemExit(1)
    try:
        exit_code = UI_MAIN()
    finally:
        from eolian.diag import close_log

        close_log()
        if console_log is not None:
            console_log.close()
    raise SystemExit(exit_code)
