"""Windows 文件/目录占用：识别、探测、退避重试。

为什么需要这一层：

Windows 对**目录改名**的限制非常窄 —— 只要目录本身有一个不带
``FILE_SHARE_DELETE`` 的句柄（资源管理器窗口、杀软实时扫描刚落盘的
WAD、同步/索引服务、某个进程的 cwd），``os.rename`` 立刻返回
``ERROR_SHARING_VIOLATION``(32)「另一个程序正在使用此文件」。
目录里的**文件**被独占打开则是 ``ERROR_ACCESS_DENIED``(5)「拒绝访问」。
两者都是"过几十毫秒扫描完就没了"的竞态，所以这里只做三件事：

1. ``is_busy(exc)``   —— 判断这个 OSError 值不值得重试；
2. ``probe(path)``    —— 自己用全共享方式打开一次，区分"目录被占"、
                         "里面某个文件被占"、"根本没被占"；
3. ``retry_os(fn)``   —— 指数退避重试。

注意：Restart Manager（``RmGetList``）对目录句柄返回空列表，实测查不到
占用者，所以不采用；要指名到进程请用 Sysinternals ``handle64.exe``。

命令行自检：``python -m eolian.winlock C:\\eolian-overlay-current``
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Win32 常量
DELETE = 0x00010000                  # 改名/删除所需的访问权限
FILE_SHARE_ALL = 0x00000007          # READ | WRITE | DELETE
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000   # 打开目录必须带

ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_HANDLE_DISK_FULL = 39

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: 值得等一会儿再试的错误（占用类）
BUSY_WINERRORS = frozenset({ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION,
                            ERROR_LOCK_VIOLATION})


def is_busy(exc: BaseException) -> bool:
    """这个异常是不是"被别的进程占着，等会儿就行"那一类。"""
    if isinstance(exc, PermissionError):
        if os.name != "nt":
            return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in BUSY_WINERRORS
    # 非 Windows / 未带 winerror：按 errno 宽松判断
    return getattr(exc, "errno", None) in (11, 13, 16)


# ---------------------------------------------------------------- 打开探测

if os.name == "nt":  # pragma: no cover - 仅在 Windows 上可用
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = ctypes.c_void_p
    _CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = ctypes.c_int
    _CloseHandle.argtypes = [ctypes.c_void_p]

    def _try_open(path: Path) -> int:
        """以"改名/删除"所需的权限试开 path（目录也行）。返回 0 表示可以动，否则 WinError。

        关键在 ``DELETE`` 访问权：改名一个对象需要 DELETE 权限，而别的进程若
        打开时没有共享 DELETE，这里就会被拒（32/5）。用 GENERIC_READ 试开会
        误判成"没被占"——读权限是共享的，改名权限不是。
        """
        handle = _CreateFileW(
            str(path), DELETE, FILE_SHARE_ALL, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            return ctypes.get_last_error() or 1
        _CloseHandle(handle)
        return 0

else:  # pragma: no cover - 非 Windows 走 Python 的 open
    def _try_open(path: Path) -> int:
        try:
            if Path(path).is_dir():
                os.listdir(path)
            else:
                with open(path, "rb"):
                    pass
        except PermissionError:
            return ERROR_ACCESS_DENIED
        except OSError:
            return ERROR_SHARING_VIOLATION
        return 0


@dataclass
class Occupancy:
    """目录/文件的占用快照。"""

    busy: bool = False
    files_busy: bool = False
    detail: str = ""
    blocked_files: list[str] = field(default_factory=list)
    winerror: int = 0

    def summary(self) -> str:
        if not self.busy:
            return "未被占用"
        where = "目录句柄" if not self.files_busy else "文件句柄"
        text = f"{where}被占用(winerror={self.winerror})"
        if self.blocked_files:
            text += f" 文件示例={[Path(p).name for p in self.blocked_files[:2]]}"
        return text


def _iter_files(root: Path, limit: int) -> list[Path]:
    found: list[Path] = []
    try:
        for item in root.rglob("*"):
            if item.is_file():
                found.append(item)
                if len(found) >= limit:
                    break
    except OSError:
        pass
    return found


def probe(path: str | Path, *, sample_files: int = 32) -> Occupancy:
    """探测 path 现在能不能被改名/替换。

    两件事分开查：
      * 目录本身能不能拿到 DELETE 权限 —— 拿到就能整体改名；
      * 里面的文件能不能拿到 DELETE 权限 —— 拿到才能逐文件替换。
    目录能改名不代表文件能替换（扫描器常常只占着某个文件），所以这里两个都
    测，``files_busy`` 指出被挡的到底是哪一层。
    """
    target = Path(path)
    try:
        exists = target.exists()
    except OSError:
        exists = False
    if not exists:
        return Occupancy(False, False, "路径不存在", [])

    code = _try_open(target)
    blocked: list[str] = []
    if target.is_dir():
        for child in _iter_files(target, sample_files):
            if _try_open(child):
                blocked.append(str(child))
                if len(blocked) >= 3:
                    break
    if blocked:
        detail = "目录里有文件被独占打开" if code == 0 else "目录句柄和文件都被占用"
        return Occupancy(True, True, detail, blocked, code or ERROR_ACCESS_DENIED)
    if code != 0:
        return Occupancy(True, False, "目录句柄被占用", [], code)
    return Occupancy(False, False, "未被占用", [])


# ---------------------------------------------------------------- 退避重试


def retry_os(callback, *, attempts: int = 6, base_delay: float = 0.2,
             max_delay: float = 2.0, what: str = "文件操作",
             on_retry=None):
    """对占用类 OSError 做指数退避重试，其它异常直接抛。

    总等待上限 ≈ base_delay * (2**(attempts-1) - 1) 秒（默认约 6 秒）。
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return callback()
        except OSError as exc:
            if attempt >= attempts or not is_busy(exc):
                raise
            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:  # noqa: BLE001 - 日志失败不能打断流程
                    pass
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    raise AssertionError("unreachable")


def wait_until_free(path: str | Path, *, attempts: int = 6,
                    base_delay: float = 0.2, max_delay: float = 2.0) -> Occupancy:
    """主动等 path 空出来（不执行任何操作），返回最后一次探测结果。"""
    delay = base_delay
    snapshot = probe(path)
    for _ in range(max(attempts - 1, 0)):
        if not snapshot.busy:
            return snapshot
        time.sleep(delay)
        delay = min(delay * 2, max_delay)
        snapshot = probe(path)
    return snapshot


# ---------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__.strip())
        return 1
    ok = True
    for raw in args:
        target = Path(raw)
        snapshot = probe(target)
        print(f"{target}")
        print(f"  exists : {target.exists()}")
        print(f"  busy   : {snapshot.busy}  {snapshot.detail}")
        if snapshot.blocked_files:
            for item in snapshot.blocked_files:
                print(f"  locked : {item}")
        if snapshot.busy:
            ok = False
            print("  提示   : 关掉资源管理器里指向该目录的窗口 / 暂停实时防护扫描 /"
                  " 用 handle64.exe <路径> 指名进程")
    return 0 if ok else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
