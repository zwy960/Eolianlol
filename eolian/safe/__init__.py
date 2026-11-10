"""safe —— 防封守护模块（fail-closed）。

把换肤从"能跑"提升到"跑得像没跑"：注入前后做门禁、合规校验、痕迹清理、
日志脱敏。设计原则只有一条 —— **任何不确定，一律不打补丁**（宁可这局没皮肤，
也不要带着可疑状态进对局）。

模块划分：
    policy     策略（off / balanced / strict）+ safe.json 持久化
    report     Finding / Report / SafeRefusal，统一体检结论
    peinfo     纯标准库 PE 解析（版本指纹、导入表、节表）
    procinfo   进程 / 权限 / 完整性级别 / 服务状态
    anticheat  ACE、Vanguard、游戏进程、已注入模块探测
    version    游戏 build 指纹 + 补丁器 EOL 门禁
    overlay_verify 覆盖 WAD 合规校验（不许新加 chunk、不许动 base-skin）
    footprint  本机痕迹审计与清理（IFEO / AppCompat / 代理 DLL / 残留）
    redact     日志脱敏（基址、偏移、路径、账号、哈希）
    session    SafeGuard：preflight / gate / watchdog / teardown / recover
    cli        python -m eolian.safe <cmd>

对外主入口：
    from eolian.safe import SafeGuard, SafeRefusal, load_policy
    SafeGuard().gate(overlay_root)      # 拦截型门禁
"""

from __future__ import annotations

from .policy import SafePolicy, load_policy, lcu_patch_enabled, policy_path, save_policy
from .report import Finding, Report, SafeRefusal
from .session import (
    SafeGuard,
    ensure_guarded,
    guard_injection,
    is_guarded,
    mark_guarded,
    redact_previous_logs,
)

__all__ = [
    "Finding",
    "Report",
    "SafeGuard",
    "SafePolicy",
    "SafeRefusal",
    "ensure_guarded",
    "guard_injection",
    "is_guarded",
    "lcu_patch_enabled",
    "load_policy",
    "mark_guarded",
    "policy_path",
    "redact_previous_logs",
    "save_policy",
]

__version__ = "0.1.0"
