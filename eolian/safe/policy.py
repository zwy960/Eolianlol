"""safe 策略：off / balanced / strict 三档，落盘 ``%LOCALAPPDATA%\\Eolian\\safe.json``。

策略只做一件事：把"什么情况下允许打补丁"写成可读的布尔/阈值。
所有阈值都朝"宁可失效"的方向取默认值 —— 皮肤没了可以下一局再来，
被记一次可疑行为不会退回来。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .report import Report

ENV_POLICY = "EOLIAN_SAFE_POLICY"
ENV_POLICY_JSON = "EOLIAN_SAFE_JSON"

# 三档预设：写死在代码里，避免"用户改了 json 反而更危险"
PRESETS: dict[str, dict[str, Any]] = {
    # 完全关闭：只保留 CLI 的体检能力，注入路径不做任何拦截
    "off": {
        "enabled": False,
    },
    # 默认档：拦硬伤（未知版本、覆盖越界、IFEO/代理 DLL 痕迹、补丁器过期）
    "balanced": {
        "enabled": True,
        "allow_unknown_build": False,
        "eol_margin_days": 3,
        "require_admin_match": True,
        "require_no_debugger": True,
        "block_if_ifeo": True,
        "block_if_proxy_dll": True,
        "block_if_stray_host": False,
        "require_overlay_chunk_subset": True,
        "max_replaced_chunk_ratio": 0.25,
        "forbid_base_skin_change": True,
        "strict_overlay_paths": True,
        "cleanup_on_exit": True,
        "kill_host_on_exit": True,
        "redact_logs": True,
        "lcu_patch_skin": False,
    },
    # 严格档：连"看起来不对"也拦（新增依赖、比例偏高、残留宿主进程）
    "strict": {
        "enabled": True,
        "allow_unknown_build": False,
        "eol_margin_days": 7,
        "require_admin_match": True,
        "require_no_debugger": True,
        "block_if_ifeo": True,
        "block_if_proxy_dll": True,
        "block_if_stray_host": True,
        "require_overlay_chunk_subset": True,
        "max_replaced_chunk_ratio": 0.05,
        "forbid_base_skin_change": True,
        "strict_overlay_paths": True,
        "cleanup_on_exit": True,
        "kill_host_on_exit": True,
        "redact_logs": True,
        "upgrade_warnings": True,
        "lcu_patch_skin": False,
    },
}

DEFAULT_MODE = "balanced"

# 覆盖 WAD 里允许出现的文件后缀（不允许多带任何可执行载荷）
ALLOWED_OVERLAY_SUFFIXES = (".wad.client",)


@dataclass(frozen=True)
class SafePolicy:
    mode: str = DEFAULT_MODE
    enabled: bool = True

    # --- 版本门禁 ---
    allow_unknown_build: bool = False
    known_builds: tuple[str, ...] = ()
    eol_margin_days: int = 3
    # 游戏正常更新（PE 时间戳前进了）时自动登记新 build，避免每次小版本都要手动 --add-build；
    # 同一 build 的文件被改写（时间戳没前进但指纹变了）仍然拦截。
    auto_accept_newer_build: bool = True
    last_build_timestamp: int = 0

    # 选人阶段是否 PATCH LCU，把 selectedSkinId 改成目标皮肤。
    # 默认 False：这个 id 会同步给服务端（换成未拥有的皮肤 = 越权声明）；
    # 关掉后只走本地 overlay，服务端看到的就是你自己选的那个皮肤。
    lcu_patch_skin: bool = False

    # --- 环境门禁 ---
    require_admin_match: bool = True
    require_no_debugger: bool = True
    block_if_ifeo: bool = True
    block_if_proxy_dll: bool = True
    block_if_stray_host: bool = False

    # --- 覆盖合规 ---
    require_overlay_chunk_subset: bool = True
    max_replaced_chunk_ratio: float = 0.25
    forbid_base_skin_change: bool = True
    strict_overlay_paths: bool = True
    allowed_overlay_suffixes: tuple[str, ...] = ALLOWED_OVERLAY_SUFFIXES

    # --- 收尾 ---
    cleanup_on_exit: bool = True
    kill_host_on_exit: bool = True
    redact_logs: bool = True

    # --- 杂项 ---
    upgrade_warnings: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- 构造 ----

    @classmethod
    def preset(cls, mode: str = DEFAULT_MODE, **overrides: Any) -> "SafePolicy":
        mode = (mode or DEFAULT_MODE).strip().lower()
        if mode not in PRESETS:
            raise ValueError(f"未知 safe 模式: {mode!r}（可选 {', '.join(PRESETS)}）")
        base = dict(PRESETS[mode])
        base["mode"] = mode
        base.update({k: v for k, v in overrides.items() if v is not None})
        # JSON 里是 list，dataclass 里是 tuple：这里统一，避免比较/序列化不一致
        for key in ("known_builds", "allowed_overlay_suffixes"):
            if key in base and base[key] is not None:
                base[key] = tuple(str(item) for item in base[key])
        return cls(**{k: v for k, v in base.items() if k in _FIELD_NAMES})

    def with_mode(self, mode: str) -> "SafePolicy":
        return SafePolicy.preset(mode)

    def replaced(self, **changes: Any) -> "SafePolicy":
        return replace(self, **changes)

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, tuple):
                value = list(value)
            out[item.name] = value
        return out

    def apply_to(self, report: Report, finding: str) -> None:
        """策略收尾：strict 档把所有 warn 升级为 block。"""
        if not (self.upgrade_warnings or self.mode == "strict"):
            return
        for item in report.findings:
            if item.level == "warn":
                item.level = "block"
                item.detail.setdefault("upgraded_by", finding)


_FIELD_NAMES = {item.name for item in fields(SafePolicy)}


def policy_path() -> Path:
    raw = os.environ.get(ENV_POLICY_JSON, "").strip()
    if raw:
        return Path(raw).expanduser()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Eolian" / "safe.json"


def load_policy(path: str | Path | None = None) -> SafePolicy:
    """优先级：EOLIAN_SAFE_POLICY(模式名) > safe.json > 默认 balanced。"""
    target = Path(path) if path else policy_path()
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}

    mode = (
        os.environ.get(ENV_POLICY, "").strip()
        or str(data.get("mode", DEFAULT_MODE))
        or DEFAULT_MODE
    ).lower()
    overrides = {k: v for k, v in data.items() if k != "mode"}
    try:
        return SafePolicy.preset(mode, **overrides)
    except (ValueError, TypeError):
        # 配置写坏了也不能把工具变成"永不拦截"：回退默认档
        return SafePolicy.preset(DEFAULT_MODE)


def save_policy(policy: SafePolicy, path: str | Path | None = None) -> Path:
    target = Path(path) if path else policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = policy.to_dict()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(target)
    return target


def lcu_patch_enabled(path: str | Path | None = None) -> bool:
    """是否允许 PATCH LCU 的 selectedSkinId（默认否）。

    这是"服务端可见层"的总开关：关掉后只做本地 overlay，
    客户端选人/加载页与模型会不一致，但账号状态与手动游玩完全一致。
    """
    try:
        return bool(load_policy(path).lcu_patch_skin)
    except (OSError, ValueError, TypeError):
        return False
