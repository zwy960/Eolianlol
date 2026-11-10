"""版本门禁：游戏 build 指纹 + 补丁器 EOL。

换肤崩在被识别上，最常见的原因不是 hook，而是 **游戏更新了、偏移没变、补丁打歪**。
补丁器（cslol / ltk）内部是硬编码偏移，一旦对不上就会在半初始化状态里反复重试。
这里的做法：把"当前 build 指纹"当钥匙，只有见过（或明确新加进 known_builds）
的 build 才允许注入。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .peinfo import PEError, PEImage
from .policy import SafePolicy
from .report import Report

GAME_EXE_NAME = "League of Legends.exe"
CLIENT_EXE_NAME = "LeagueClientUx.exe"

# 补丁器 DLL 内嵌的过期标记（cslol / ltk 同款）
EOL_PATTERNS = (
    re.compile(rb"CSLOL_EOL_TIMESTAMP=(\d+)"),
    re.compile(rb"LTK_EOL_TIMESTAMP=(\d+)"),
)

# 默认已知 build：留空 = 只认 safe.json 里登记的 build（首次运行会提示登记）
DEFAULT_KNOWN_BUILDS: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildInfo:
    path: Path
    fingerprint: str
    timestamp: int
    size_of_image: int
    entry_point: int

    def describe(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "fingerprint": self.fingerprint,
            "timestamp": self.timestamp,
            "size_of_image": self.size_of_image,
            "entry_point": self.entry_point,
        }


def read_build(game_dir: str | Path) -> BuildInfo | None:
    exe = Path(game_dir) / GAME_EXE_NAME
    if not exe.is_file():
        return None
    try:
        image = PEImage(exe)
    except PEError:
        return None
    return BuildInfo(
        path=exe,
        fingerprint=image.fingerprint(),
        timestamp=image.timestamp,
        size_of_image=image.size_of_image,
        entry_point=image.entry_point,
    )


def register_build(policy: SafePolicy, build: BuildInfo) -> SafePolicy:
    """把 build 指纹写进白名单，并记住它的 PE 时间戳（用于判断"是否只是正常更新"）。"""
    known = list(policy.known_builds)
    if build.fingerprint not in known:
        known.append(build.fingerprint)
    return policy.replaced(
        known_builds=tuple(known),
        last_build_timestamp=max(policy.last_build_timestamp, build.timestamp),
    )


def check_build(build: BuildInfo | None, policy: SafePolicy, game_dir: str | Path) -> Report:
    report = Report("版本门禁")
    if build is None:
        report.block(
            "VER001",
            "找不到或读不出游戏主程序，无法确认版本（拒绝在未知版本上打补丁）",
            game_dir=str(game_dir),
            expected=str(Path(game_dir) / GAME_EXE_NAME),
        )
        return report

    known = tuple(policy.known_builds) or DEFAULT_KNOWN_BUILDS
    report.info(
        "VER002",
        "游戏 build 指纹",
        fingerprint=build.fingerprint,
        built_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(build.timestamp)),
        image_size=f"0x{build.size_of_image:X}",
    )
    if not known:
        if policy.allow_unknown_build:
            report.warn(
                "VER003",
                "known_builds 为空：本次放行但不校验偏移，建议把该指纹登记进 safe.json",
                fingerprint=build.fingerprint,
            )
        elif policy.auto_accept_newer_build:
            # 首次运行（另一台机器/刚装）：白名单还是空的，自动把当前 build 登记进去，
            # 否则新用户第一次换肤一定被拦，而打包版没有 CLI 去手动登记。
            report.warn(
                "VER009",
                "首次运行：自动登记当前 build 后放行",
                fingerprint=build.fingerprint,
                built_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(build.timestamp)),
            )
        else:
            report.block(
                "VER004",
                "known_builds 为空且策略不允许未知版本 —— 先把指纹登记再注入",
                fingerprint=build.fingerprint,
                hint="在 safe.json 的 known_builds 里加入该值，或临时 --mode off",
            )
    elif build.fingerprint not in known:
        if policy.allow_unknown_build:
            report.warn(
                "VER005",
                "未登记的 build 指纹（策略允许未知版本）",
                fingerprint=build.fingerprint,
                known=list(known),
            )
        elif policy.auto_accept_newer_build and policy.last_build_timestamp > 0 \
                and build.timestamp > policy.last_build_timestamp:
            report.warn(
                "VER008",
                "游戏已更新到更新的 build，自动登记后放行（本次注入若异常请回滚白名单）",
                fingerprint=build.fingerprint,
                built_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(build.timestamp)),
                previous_build_at=time.strftime(
                    "%Y-%m-%d %H:%M UTC", time.gmtime(policy.last_build_timestamp)),
            )
        else:
            report.block(
                "VER006",
                "build 指纹变了但没有更新迹象（可能是文件被改写）—— 拒绝注入",
                fingerprint=build.fingerprint,
                known=list(known),
                built_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(build.timestamp)),
                hint="确认是官方更新后执行 eolian-safe policy --add-build",
            )
    else:
        report.ok("VER007", "build 指纹已登记", fingerprint=build.fingerprint)
    return report


def read_eol(dll_path: str | Path) -> int | None:
    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return None
    for pattern in EOL_PATTERNS:
        match = pattern.search(data)
        if match:
            return int(match.group(1))
    return None


def check_patcher_eol(dll_paths: list[Path] | tuple[Path, ...], policy: SafePolicy) -> Report:
    report = Report("补丁器有效期")
    seen = 0
    for dll in dll_paths:
        if not Path(dll).is_file():
            continue
        eol = read_eol(dll)
        if eol is None:
            report.info("EOL001", "补丁器 DLL 没有内嵌过期标记", dll=Path(dll).name)
            continue
        seen += 1
        now = time.time()
        margin = policy.eol_margin_days * 86400
        deadline = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(eol))
        if now > eol:
            report.block(
                "EOL002",
                "补丁器已过期（会静默拒绝工作，或半初始化后退出）",
                dll=Path(dll).name,
                expired_at=deadline,
            )
        elif now + margin > eol:
            report.warn(
                "EOL003",
                f"补丁器将在 {policy.eol_margin_days} 天内过期",
                dll=Path(dll).name,
                expired_at=deadline,
            )
        else:
            report.ok("EOL004", "补丁器在有效期内", dll=Path(dll).name, expired_at=deadline)
    if not seen:
        report.info("EOL005", "没有可检查的补丁器 DLL")
    return report
