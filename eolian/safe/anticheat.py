"""运行环境探测：ACE / Vanguard / 游戏与客户端进程 / 已注入模块 / 调试器。

只做"看清楚"，不做任何对抗动作：不杀反作弊、不卸载驱动、不碰受保护进程。
看清楚之后由 policy 决定放不放行 —— 这是"防封"里成本最低、收益最高的一环
（绝大多数封号事故是环境不对就打了补丁，而不是补丁本身有问题）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import procinfo
from .policy import SafePolicy
from .report import Report

GAME_EXES = ("League of Legends.exe",)
CLIENT_EXES = ("LeagueClientUx.exe", "LeagueClient.exe", "RiotClientServices.exe")

# 国服 ACE（AntiCheatExpert）
ACE_SERVICES = ("AntiCheatExpert Service", "ACE-BASE", "ACE-GAME")
ACE_PROCESSES = ("SGuard64.exe", "SGuardSvc64.exe", "ACE-Tray.exe", "AntiCheatExpert.exe")
ACE_DIRS = ("AntiCheatExpert",)

# 国际服 Vanguard（LoL 本身不用，但同机装了 VALORANT 就会常驻）
VANGUARD_SERVICES = ("vgc", "vgk")
VANGUARD_PROCESSES = ("vgtray.exe", "vgc.exe")

# 我们/同类工具注入后会留在模块表里的名字
INJECTED_MODULES = (
    "cslol-dll.dll",
    "ltk_patcher_dll.dll",
    "ltk_patcher_host.exe",
    "eolian-overlay.dll",
    "pengucore.dll",
)


@dataclass
class EnvSnapshot:
    game: list[procinfo.ProcInfo] = field(default_factory=list)
    client: list[procinfo.ProcInfo] = field(default_factory=list)
    ace_services: dict[str, str] = field(default_factory=dict)
    ace_processes: list[str] = field(default_factory=list)
    vanguard_services: dict[str, str] = field(default_factory=dict)
    vanguard_processes: list[str] = field(default_factory=list)
    game_integrity: str = "unknown"
    client_integrity: str = "unknown"
    self_admin: bool = False
    injected_modules: list[str] = field(default_factory=list)
    debugger_attached: bool = False

    @property
    def ace_active(self) -> bool:
        return "running" in self.ace_services.values() or bool(self.ace_processes)

    @property
    def vanguard_active(self) -> bool:
        return "running" in self.vanguard_services.values() or bool(self.vanguard_processes)

    def to_dict(self) -> dict[str, object]:
        return {
            "game_pids": [p.pid for p in self.game],
            "client_pids": [p.pid for p in self.client],
            "game_integrity": self.game_integrity,
            "client_integrity": self.client_integrity,
            "self_admin": self.self_admin,
            "ace": {"services": self.ace_services, "processes": self.ace_processes},
            "vanguard": {
                "services": self.vanguard_services,
                "processes": self.vanguard_processes,
            },
            "injected_modules": self.injected_modules,
            "debugger_attached": self.debugger_attached,
        }


def _check_remote_debugger(pid: int) -> bool:
    return procinfo.remote_debugger_present(pid)


def probe(game_dir: str | Path | None = None) -> EnvSnapshot:
    snapshot = EnvSnapshot()
    processes = procinfo.list_processes()
    by_name: dict[str, list[procinfo.ProcInfo]] = {}
    for proc in processes:
        by_name.setdefault(proc.name.lower(), []).append(proc)

    game_names = {name.lower() for name in GAME_EXES}
    client_names = {name.lower() for name in CLIENT_EXES}
    for key, items in by_name.items():
        if key in game_names:
            snapshot.game.extend(items)
        elif key in client_names:
            snapshot.client.extend(items)

    snapshot.self_admin = procinfo.is_admin()
    if snapshot.game:
        pid = snapshot.game[0].pid
        snapshot.game_integrity = procinfo.integrity_level(pid)
        snapshot.debugger_attached = _check_remote_debugger(pid)
        modules = {name.lower() for name in procinfo.module_names(pid)}
        snapshot.injected_modules = [
            name for name in INJECTED_MODULES if name.lower() in modules
        ]
    if snapshot.client:
        snapshot.client_integrity = procinfo.integrity_level(snapshot.client[0].pid)

    for service in ACE_SERVICES:
        state = procinfo.service_state(service)
        if state != "absent":
            snapshot.ace_services[service] = state
    for process in ACE_PROCESSES:
        if process.lower() in by_name:
            snapshot.ace_processes.append(process)

    for service in VANGUARD_SERVICES:
        state = procinfo.service_state(service)
        if state != "absent":
            snapshot.vanguard_services[service] = state
    for process in VANGUARD_PROCESSES:
        if process.lower() in by_name:
            snapshot.vanguard_processes.append(process)

    if game_dir:
        root = Path(game_dir)
        for folder in ACE_DIRS:
            if (root / folder).is_dir():
                snapshot.ace_services.setdefault("AntiCheatExpert(目录)", "present")
    return snapshot


def evaluate(snapshot: EnvSnapshot, policy: SafePolicy) -> Report:
    report = Report("运行环境")

    if not snapshot.game:
        report.warn(
            "ENV001",
            "游戏进程未运行，补丁器会一直等待（这不影响注入，只是提示）",
            game_exes=list(GAME_EXES),
        )
    else:
        report.ok(
            "ENV002",
            "游戏进程在线",
            pid=snapshot.game[0].pid,
            integrity=snapshot.game_integrity,
        )
        if policy.require_admin_match and snapshot.game_integrity in ("high", "system", "protected"):
            if not snapshot.self_admin:
                report.block(
                    "ENV003",
                    "游戏以管理员身份运行，而本工具不是 —— 注入必然失败，"
                    "失败的注入会留下半初始化状态",
                    game_integrity=snapshot.game_integrity,
                    self_admin=snapshot.self_admin,
                )
            else:
                report.ok("ENV004", "权限级别匹配（同为管理员）")
        elif snapshot.game_integrity == "protected":
            report.block(
                "ENV005",
                "游戏进程处于受保护完整性级别，注入会触发反作弊而不是换肤",
                integrity=snapshot.game_integrity,
            )

    if policy.require_no_debugger and snapshot.debugger_attached:
        report.block(
            "ENV006",
            "游戏进程正被调试器附加（或存在调试端口），此时注入极易被判定为外挂调试",
        )

    if snapshot.injected_modules:
        report.warn(
            "ENV007",
            "游戏进程里已经存在注入模块（上一次会话未卸载干净？）",
            modules=snapshot.injected_modules,
        )

    if snapshot.ace_active:
        report.info(
            "ENV008",
            "检测到 ACE（腾讯反作弊）在运行 —— 正常现象，但意味着 WAD 扫描是开启的",
            services=snapshot.ace_services,
            processes=snapshot.ace_processes,
        )
    if snapshot.vanguard_active:
        report.info(
            "ENV009",
            "检测到 Vanguard 组件（通常来自同机 VALORANT）",
            services=snapshot.vanguard_services,
            processes=snapshot.vanguard_processes,
        )

    if snapshot.client:
        report.info(
            "ENV010",
            "客户端在线",
            pid=snapshot.client[0].pid,
            integrity=snapshot.client_integrity,
        )
    return report
