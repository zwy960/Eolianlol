"""本机痕迹审计与清理。

防封里最容易被忽略的一条：**你留下的一次性痕迹，比注入本身更容易被拉黑**。
典型残留（Yoousn 那类工具留下的形状）：

- ``IFEO\\LeagueClientUx.exe`` 下写着 ``Debugger = rundll32.exe``（客户端一启动就被劫持）
- ``AppCompatFlags\\Layers`` 里多出一条指向 rundll32 / 第三方目录的兼容层
- 游戏目录里多了 ``version.dll`` / ``d3d9.dll`` / ``dwrite.dll``（DLL 侧载）
- ``*.yoousn-backup-*`` / ``*.bak`` 这类"备份"，其实是别人改过你文件的证据
- 上一会话的宿主进程（mod-tools / ltk_patcher_host / ltk-manager）没退出

清理原则：**只删自己产出的东西**。游戏目录只读不写，注册表只读不改；
需要动的地方全部先写 journal，再动，动了能回滚。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import procinfo
from .policy import SafePolicy
from .report import Finding, Report

IS_WINDOWS = sys.platform == "win32"

IFEO_ROOT = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
LAYERS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"

WATCHED_EXES = ("LeagueClientUx.exe", "LeagueClient.exe", "League of Legends.exe")

# 侧载候选：这些名字出现在游戏目录 = 有人往里面放东西了
PROXY_DLLS = (
    "version.dll",
    "d3d9.dll",
    "dwrite.dll",
    "winmm.dll",
    "dinput8.dll",
    "dbghelp.dll",
    "pengucore.dll",
)

# 明显不是"备份"形状的东西
SUSPICIOUS_MARKERS = (".yoousn-backup-", "hook-backup.json", "preferred_mode.txt")

HOST_PROCESSES = ("mod-tools.exe", "ltk_patcher_host.exe", "ltk-manager.exe")


@dataclass
class Site:
    kind: str
    path: str
    detail: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "path": self.path, "detail": dict(self.detail)}


# ---------------------------------------------------------------- registry


def _reg_values(root: int, path: str) -> dict[str, str]:
    if not IS_WINDOWS:
        return {}
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            values: dict[str, str] = {}
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = str(value)
                index += 1
            return values
    except OSError:
        return {}


def _reg_subkeys(root: int, path: str) -> list[str]:
    if not IS_WINDOWS:
        return []
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            names: list[str] = []
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
            return names
    except OSError:
        return []


def scan_ifeo() -> list[Site]:
    """IFEO 劫持：Debugger 值存在 = 该 exe 的启动被转交给别的程序。"""
    if not IS_WINDOWS:
        return []
    import winreg

    sites: list[Site] = []
    for name in _reg_subkeys(winreg.HKEY_LOCAL_MACHINE, IFEO_ROOT):
        if not any(watched.lower() in name.lower() for watched in WATCHED_EXES):
            continue
        values = _reg_values(winreg.HKEY_LOCAL_MACHINE, f"{IFEO_ROOT}\\{name}")
        if "Debugger" in values:
            sites.append(
                Site("ifeo", f"HKLM\\{IFEO_ROOT}\\{name}", {"Debugger": values["Debugger"]})
            )
    # 顺手看一眼 HKCU（少数工具写这里，权限更低但同样生效）
    for name in _reg_subkeys(winreg.HKEY_CURRENT_USER, IFEO_ROOT):
        if not any(watched.lower() in name.lower() for watched in WATCHED_EXES):
            continue
        values = _reg_values(winreg.HKEY_CURRENT_USER, f"{IFEO_ROOT}\\{name}")
        if "Debugger" in values:
            sites.append(
                Site("ifeo", f"HKCU\\{IFEO_ROOT}\\{name}", {"Debugger": values["Debugger"]})
            )
    return sites


def scan_layers() -> list[Site]:
    """兼容层：正常情况下只有 DPI / RUNASADMIN，出现 rundll32 就是侧载套路。"""
    if not IS_WINDOWS:
        return []
    import winreg

    sites: list[Site] = []
    for name, value in _reg_values(winreg.HKEY_CURRENT_USER, LAYERS_KEY).items():
        lowered = (name + " " + value).lower()
        suspicious = "rundll32" in lowered or any(
            token in lowered for token in ("pengu", "cslol", "ltk_patcher", "yoousn", "eolian")
        )
        if suspicious:
            sites.append(Site("compat-layer", name, {"flags": value}))
    return sites


# ---------------------------------------------------------------- filesystem


def scan_game_dir(game_dir: str | Path) -> list[Site]:
    root = Path(game_dir)
    if not root.is_dir():
        return []
    sites: list[Site] = []
    lowered = {item.name.lower(): item for item in root.iterdir() if item.is_file()}
    for name in PROXY_DLLS:
        item = lowered.get(name)
        if item is not None:
            sites.append(
                Site("proxy-dll", str(item), {"size": item.stat().st_size,
                                              "mtime": int(item.stat().st_mtime)})
            )
    for item in root.iterdir():
        if not item.is_file():
            continue
        name = item.name.lower()
        if name.endswith((".bak", ".old", ".orig")) or any(
            marker.lower() in name for marker in SUSPICIOUS_MARKERS
        ):
            sites.append(Site("leftover", str(item), {"size": item.stat().st_size}))
    return sites


def scan_stray_hosts() -> list[procinfo.ProcInfo]:
    return procinfo.find_processes(HOST_PROCESSES)


# ---------------------------------------------------------------- journal


def journal_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / "safe-journal.json"


def _write_journal(session_dir: str | Path, entries: list[dict[str, object]]) -> None:
    path = journal_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_journal(session_dir: str | Path) -> list[dict[str, object]]:
    try:
        data = json.loads(journal_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- audit / clean


def audit(game_dir: str | Path, session_dir: str | Path, policy: SafePolicy) -> Report:
    report = Report("本机痕迹")

    ifeo = scan_ifeo()
    if ifeo:
        level = "block" if policy.block_if_ifeo else "warn"
        report.add(
            level, "FP001",
            "检测到 IFEO Debugger 劫持项（有程序在接管 League 系进程的启动）",
            sites=[site.to_dict() for site in ifeo],
        )
    else:
        report.ok("FP002", "IFEO 干净（没有 Debugger 劫持）")

    layers = scan_layers()
    if layers:
        report.warn(
            "FP003",
            "兼容层里出现了 rundll32 / 第三方目录条目（DLL 侧载常见手法）",
            sites=[site.to_dict() for site in layers],
        )
    else:
        report.ok("FP004", "兼容层没有可疑条目")

    game_sites = scan_game_dir(game_dir)
    proxy = [site for site in game_sites if site.kind == "proxy-dll"]
    leftovers = [site for site in game_sites if site.kind == "leftover"]
    if proxy:
        level = "block" if policy.block_if_proxy_dll else "warn"
        report.add(
            level, "FP005",
            "游戏目录里存在侧载型 DLL（原版游戏不带这些文件）",
            sites=[site.to_dict() for site in proxy],
        )
    else:
        report.ok("FP006", "游戏目录没有侧载型 DLL")

    if leftovers:
        report.warn(
            "FP007",
            "游戏目录里存在备份/残留文件",
            sites=[site.to_dict() for site in leftovers],
        )
    else:
        report.ok("FP008", "游戏目录没有备份残留")

    hosts = scan_stray_hosts()
    if hosts:
        level = "block" if policy.block_if_stray_host else "warn"
        report.add(
            level, "FP009",
            "上一会话的补丁宿主进程仍在运行（会持续挂着 hook）",
            processes=[{"pid": proc.pid, "name": proc.name} for proc in hosts],
        )
    else:
        report.ok("FP010", "没有残留的补丁宿主进程")

    pending = [entry for entry in _read_journal(session_dir) if entry.get("state") == "pending"]
    if pending:
        report.warn(
            "FP011",
            "存在未完成的清理任务（上次异常退出）—— 运行 `eolian-safe recover`",
            count=len(pending),
        )
    else:
        report.ok("FP012", "没有未完成的清理任务")
    return report


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def clean(
    game_dir: str | Path,
    session_dir: str | Path,
    policy: SafePolicy,
    *,
    dry_run: bool = False,
    include_ascii: bool = True,
    ascii_dir: str | Path | None = None,
) -> Report:
    """清理自己产出的痕迹。游戏目录只读，注册表不动。"""
    report = Report("痕迹清理")
    entries: list[dict[str, object]] = []
    now = int(time.time())

    for proc in scan_stray_hosts():
        report.info("CL001", "结束残留宿主进程", pid=proc.pid, name=proc.name)
        if not dry_run:
            if policy.kill_host_on_exit:
                ok = procinfo.kill_process(proc.pid)
                report.add("ok" if ok else "warn", "CL002",
                           "宿主进程已结束" if ok else "宿主进程结束失败",
                           pid=proc.pid, name=proc.name)
            else:
                report.warn("CL003", "策略禁止结束进程，跳过", pid=proc.pid)

    session = Path(session_dir)
    candidates: list[Path] = []
    if session.is_dir():
        candidates.extend(session.glob("mods/.*"))          # 构建 staging 残留
        candidates.extend(session.glob("overlays/.*"))
        candidates.extend(session.glob(".*.staging-*"))
        candidates.extend(session.glob(".*.backup"))
        runtime_config = session / "cslol-runtime.config"
        if runtime_config.is_file():
            candidates.append(runtime_config)

    if include_ascii and ascii_dir is not None:
        ascii_path = Path(ascii_dir)
        if ascii_path.is_dir():
            candidates.append(ascii_path)
        # 固定 ASCII 目录被占死时，换肤会退化成 <name>-<毫秒>-<pid> 的独立副本
        # 目录；收尾必须一并清掉，否则一局一个、几百 MB 地攒着。
        prefix = f"{ascii_path.name}-"
        try:
            for item in ascii_path.parent.iterdir():
                if not item.is_dir() or not item.name.startswith(prefix):
                    continue
                major, _, minor = item.name[len(prefix):].partition("-")
                if major.isdigit() and minor.isdigit():
                    candidates.append(item)
        except OSError:
            pass

    for path in candidates:
        if not path.exists():
            continue
        entries.append({"path": str(path), "kind": "delete", "at": now, "state": "pending"})
        report.info("CL004", "计划删除", path=str(path))
        if dry_run:
            continue
        _remove(path)
        entries[-1]["state"] = "done"

    if not dry_run:
        _write_journal(session, entries)
    report.add(
        "ok" if all(entry["state"] != "pending" for entry in entries) else "warn",
        "CL005",
        "清理完成（dry-run 时只列出不删除）" if not dry_run else "dry-run：未改动任何文件",
        removed=len(entries),
        dry_run=dry_run,
    )
    return report


def recover(session_dir: str | Path) -> Report:
    """重放上次未完成的清理。"""
    report = Report("崩溃恢复")
    entries = _read_journal(session_dir)
    pending = [entry for entry in entries if entry.get("state") == "pending"]
    if not pending:
        report.ok("RC001", "没有待恢复的清理任务")
        return report
    for entry in pending:
        path = Path(str(entry.get("path", "")))
        if path.exists():
            _remove(path)
            report.ok("RC002", "已补删", path=str(path))
        else:
            report.info("RC003", "目标已不存在", path=str(path))
        entry["state"] = "done"
    _write_journal(session_dir, entries)
    return report


def clean_stale_mei(max_age_hours: float = 24.0, *,
                    root: str | Path | None = None,
                    dry_run: bool = False) -> int:
    """清理 PyInstaller onefile 遗留的 ``_MEIxxxx`` 解包目录。

    onefile 每次启动都会在 %TEMP% 解一份（内含 vendor/cslol 组件），
    正常退出会删；被强杀/崩溃时残留。这里只清"够旧"的，避免动到
    正在运行的另一个实例。
    """
    temp_root = Path(root) if root else Path(tempfile.gettempdir())
    current = ""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        try:
            current = str(Path(meipass).resolve()).lower()
        except OSError:
            current = str(meipass).lower()
    removed = 0
    now = time.time()
    for item in temp_root.glob("_MEI*"):
        if not item.is_dir():
            continue
        try:
            if current and str(item.resolve()).lower() == current:
                continue
            age_hours = (now - item.stat().st_mtime) / 3600.0
        except OSError:
            continue
        if age_hours < max_age_hours:
            continue
        if dry_run:
            removed += 1
            continue
        shutil.rmtree(item, ignore_errors=True)
        if not item.exists():
            removed += 1
    return removed


def as_finding(level: str, code: str, message: str, **detail: object) -> Finding:
    """给 CLI/UI 复用的小工具。"""
    return Finding(level, code, message, dict(detail))
