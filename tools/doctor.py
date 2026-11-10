"""运行环境自检：python -m tools.doctor"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eolian.lcu import LcuError, discover
from eolian.modgen import find_binhashes
from eolian.patcher import (
    CSLOL_DLL,
    CSLOL_MOD_TOOLS,
    read_patcher_eol,
)
from eolian.settings import load_settings


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    settings = load_settings()
    critical_ok = True

    def report(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        nonlocal critical_ok
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if critical and not ok:
            critical_ok = False

    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        elevated = True
    report("管理员权限", elevated, "已提权" if elevated else "请以管理员身份运行")
    report("客户端目录", settings.valid_client, str(settings.client_root))
    report("游戏目录", settings.valid_game, str(settings.game_dir))
    report("覆盖构建器", settings.valid_builder, str(settings.learn_overlay))

    try:
        lockfile = discover(settings.client_root)
        report("LCU 连接", True, f"端口 {lockfile.port}")
    except LcuError as exc:
        report("LCU 连接", False, str(exc))

    try:
        binhashes = find_binhashes()
        report("binhashes 哈希表", True, str(binhashes))
    except FileNotFoundError as exc:
        report("binhashes 哈希表", False, str(exc))

    report(
        "cslol mod-tools",
        CSLOL_MOD_TOOLS.is_file(),
        str(CSLOL_MOD_TOOLS),
    )
    report("cslol DLL", CSLOL_DLL.is_file(), str(CSLOL_DLL))
    eol = read_patcher_eol()
    if eol:
        import time
        report(
            "LTK 补丁器有效期",
            time.time() <= eol,
            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(eol)),
            critical=False,
        )

    if settings.valid_builder:
        try:
            proc = subprocess.run(
                [str(settings.learn_overlay), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            report(
                "构建器可执行",
                proc.returncode == 0,
                (proc.stdout or proc.stderr).strip() or f"code={proc.returncode}",
            )
        except OSError as exc:
            report("构建器可执行", False, str(exc))

    wad = settings.game_dir / "DATA" / "FINAL" / "Champions" / "Aatrox.wad.client"
    report("游戏 WAD", wad.is_file(), str(wad), critical=False)
    print("\n结论:", "环境可用" if critical_ok else "存在问题，请按 FAIL 项处理")
    return 0 if critical_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
