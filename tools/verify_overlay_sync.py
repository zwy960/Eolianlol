"""真机验证 ASCII 副本的"占用容错"：不会再有 WinError 32 卡死。

它模拟两种真实占用，各跑一遍：

  A. 资源管理器/实时防护持有**目录句柄**（不带 FILE_SHARE_DELETE）
     —— 旧实现 `copytree → 目录改名` 立刻抛 WinError 32；
        新实现逐文件 `os.replace`，应当照常成功。
  B. 扫描器独占持有**某个 WAD 文件**
     —— 该文件替换会失败，重试耗尽后应当自动退化成
        `<ascii_dir>-<毫秒>-<pid>` 独立副本目录，流程继续。

用法（需本机已装游戏，仅构建不注入，不会碰游戏进程）：
    .venv\\Scripts\\python.exe tools\\verify_overlay_sync.py
    .venv\\Scripts\\python.exe tools\\verify_overlay_sync.py --champion aatrox --skin 11
    .venv\\Scripts\\python.exe tools\\verify_overlay_sync.py --skip-build   # 复用已有覆盖
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian import winlock                      # noqa: E402
from eolian.diag import close_log, log          # noqa: E402
from eolian.overlay import SkinSwapper          # noqa: E402
from eolian.settings import load_settings       # noqa: E402

FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
GENERIC_READ = 0x80000000
FILE_LIST_DIRECTORY = 0x1
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateFileW = kernel32.CreateFileW
_CreateFileW.restype = ctypes.c_void_p
_CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                         ctypes.c_void_p]
_CloseHandle = kernel32.CloseHandle
_CloseHandle.restype = ctypes.c_int
_CloseHandle.argtypes = [ctypes.c_void_p]


def hold_directory(path: Path) -> int:
    """模拟资源管理器：持目录句柄，只共享 READ|WRITE（不给 DELETE）。"""
    handle = _CreateFileW(str(path), FILE_LIST_DIRECTORY, FILE_SHARE_READ | FILE_SHARE_WRITE,
                          None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"无法打开目录 {path}")
    return handle


def hold_file(path: Path) -> int:
    """模拟扫描器：独占读一个文件（不给 DELETE 共享）。"""
    handle = _CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ,
                          None, OPEN_EXISTING, 0, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"无法打开文件 {path}")
    return handle


def age_copy(ascii_dir: Path, wad: Path) -> None:
    """把目标副本的 mtime 拨旧一点，强制下一轮重新同步。"""
    target = ascii_dir / wad
    if target.is_file():
        stat = target.stat()
        os.utime(target, (stat.st_atime, stat.st_mtime - 30))


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 ASCII 副本占用容错")
    parser.add_argument("--champion", default="aatrox")
    parser.add_argument("--skin", type=int, default=11)
    parser.add_argument("--skip-build", action="store_true", help="不重新构建，直接用现有覆盖")
    parser.add_argument("--no-hold", action="store_true", help="只跑正常同步，不做占用模拟")
    args = parser.parse_args()

    settings = load_settings()
    print(f"game_dir      = {settings.game_dir}")
    print(f"overlay_ascii = {settings.overlay_ascii_dir}")
    swapper = SkinSwapper()

    if args.skip_build:
        overlay = settings.session_dir / "overlays" / f"{args.champion}-skin{args.skin}"
        if not overlay.is_dir():
            print(f"[FAIL] 没有可复用的覆盖目录: {overlay}")
            return 2
    else:
        started = time.monotonic()
        result = swapper.build_overlay(args.champion, args.skin)
        overlay = result.overlay_root
        print(f"[BUILD] {overlay}  ({time.monotonic() - started:.1f}s, "
              f"{len(result.overlay_built)} wad)")
    wads = sorted(overlay.rglob("*.wad.client"))
    print(f"[WADS ] {len(wads)} 个")

    # --- 基线：正常同步 ---
    ascii_dir = swapper.copy_to_ascii(overlay)
    print(f"[BASE ] -> {ascii_dir}")
    if ascii_dir != settings.overlay_ascii_dir.resolve():
        print("[FAIL] 基线就退化成独立目录了，说明固定目录一开始就被占")
        return 1
    if not (ascii_dir / wads[0].relative_to(overlay)).is_file():
        print("[FAIL] 基线副本缺文件")
        return 1

    if args.no_hold:
        print("[OK] 只跑了基线同步")
        close_log()
        return 0

    failures: list[str] = []

    # --- A：目录句柄被占（旧代码就是死在这里）---
    age_copy(ascii_dir, wads[0].relative_to(overlay))
    handle = hold_directory(ascii_dir)
    try:
        snapshot = winlock.probe(ascii_dir)
        print(f"[A    ] 占用探测: {snapshot.summary()}")
        if not snapshot.busy:
            failures.append("目录句柄没被识别为占用（探测逻辑回归了）")
        result = swapper.copy_to_ascii(overlay)
        if result != settings.overlay_ascii_dir.resolve():
            failures.append(f"目录被占时不该退化目录，实际 -> {result}")
        else:
            print("[A    ] PASS 目录被占，逐文件同步照常完成")
    except OSError as exc:
        failures.append(f"目录被占时仍然失败: {exc}")
    finally:
        _CloseHandle(handle)

    # --- B：单个 WAD 被独占（应当重试后退化到独立副本目录）---
    age_copy(ascii_dir, wads[0].relative_to(overlay))
    wad = wads[0].relative_to(overlay)
    holder = hold_file(ascii_dir / wad)
    try:
        snapshot = winlock.probe(ascii_dir)
        print(f"[B    ] 占用探测: {snapshot.summary()}")
        started = time.monotonic()
        result = swapper.copy_to_ascii(overlay)
        elapsed = time.monotonic() - started
        print(f"[B    ] -> {result}  ({elapsed:.1f}s)")
        if result == settings.overlay_ascii_dir.resolve():
            failures.append("文件被独占时应当退化到独立副本目录，实际仍在固定目录")
        elif not (result / wad).is_file():
            failures.append(f"独立副本目录缺文件: {result}")
        else:
            print("[B    ] PASS 重试后自动切到独立副本目录")
        sibling_stale = [p for p in result.parent.glob(f"{result.name}-*")]
        if sibling_stale:
            failures.append(f"历史副本目录没清掉: {sibling_stale}")
    except OSError as exc:
        failures.append(f"文件被独占时整个流程失败: {exc}")
    finally:
        _CloseHandle(holder)

    # --- C：占用解除后回到固定目录 ---
    try:
        result = swapper.copy_to_ascii(overlay)
        if result == settings.overlay_ascii_dir.resolve():
            print("[C    ] PASS 占用解除后回到固定 ASCII 目录")
        else:
            print(f"[C    ] NOTE 仍在使用独立副本目录: {result}")
    except OSError as exc:
        failures.append(f"占用解除后仍失败: {exc}")

    log("VERIFY", "占用容错验证结束", failures=len(failures))
    close_log()
    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1
    print("[OK] 占用容错全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
