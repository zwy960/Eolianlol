"""``python -m eolian.safe`` 命令行：体检 / 门禁 / 校验 / 清理 / 脱敏。

退出码：0 = 通过，2 = 被拦截（方便脚本 gate 用法）。

    python -m eolian.safe audit                 # 全量体检
    python -m eolian.safe gate --overlay <dir>  # 只跑门禁，拦截则退出码 2
    python -m eolian.safe verify --overlay <dir>
    python -m eolian.safe clean --apply         # 真正清理（默认 dry-run）
    python -m eolian.safe status
    python -m eolian.safe recover
    python -m eolian.safe redact --file session/diag.log
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import footprint, overlay_verify, version
from .policy import SafePolicy, load_policy, policy_path, save_policy
from .redact import sanitize_file
from .report import Report, SafeRefusal
from .session import SafeGuard


def _emit(report: Report, as_json: bool, verbose: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.format(verbose=verbose))


def _resolve_overlay(raw: str | None, guard: SafeGuard) -> Path:
    if raw:
        return Path(raw).expanduser()
    ascii_dir = guard.settings.overlay_ascii_dir
    if ascii_dir.is_dir() and any(ascii_dir.rglob("*.wad.client")):
        return ascii_dir
    # ASCII 目录被占死时换肤会退化成 <name>-<毫秒>-<pid> 的独立副本目录
    pattern = re.compile(rf"^{re.escape(ascii_dir.name)}-\d+-\d+$")
    try:
        siblings = [item for item in ascii_dir.parent.iterdir()
                    if item.is_dir() and pattern.match(item.name)]
    except OSError:
        siblings = []
    if siblings:
        latest = max(siblings, key=lambda item: item.stat().st_mtime)
        return latest
    overlays = guard.settings.session_dir / "overlays"
    if overlays.is_dir():
        candidates = sorted(
            (path for path in overlays.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return ascii_dir


def cmd_audit(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    overlay = _resolve_overlay(getattr(args, "overlay", None), guard)
    if not overlay.is_dir():
        report = Report("safe 体检")
        report.warn("CLI001", "没有可用的覆盖目录，跳过覆盖校验", overlay=str(overlay))
        report.merge(
            version.check_build(
                version.read_build(guard.settings.game_dir),
                guard.policy,
                guard.settings.game_dir,
            )
        )
        report.merge(
            footprint.audit(guard.settings.game_dir, guard.settings.session_dir, guard.policy)
        )
        _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
        return 0 if report.passed else 2
    report = guard.preflight(overlay)
    _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
    return 0 if report.passed else 2


def cmd_gate(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    overlay = _resolve_overlay(getattr(args, "overlay", None), guard)
    try:
        report = guard.gate(overlay)
    except SafeRefusal as exc:
        report = exc.report or Report("safe 门禁")
        _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    overlay = _resolve_overlay(getattr(args, "overlay", None), guard)
    report = overlay_verify.verify(overlay, guard.settings.game_dir, guard.policy)
    _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
    return 0 if report.passed else 2


def cmd_clean(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    report = footprint.clean(
        guard.settings.game_dir,
        guard.settings.session_dir,
        guard.policy,
        dry_run=not getattr(args, "apply", False),
        include_ascii=True,
        ascii_dir=guard.settings.overlay_ascii_dir,
    )
    _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    report = guard.recover()
    _emit(report, getattr(args, "json", False), getattr(args, "verbose", False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    guard = SafeGuard(policy=_policy_from_args(args))
    payload = guard.status().to_dict()
    payload["policy_path"] = str(policy_path())
    payload["game_dir"] = str(guard.settings.game_dir)
    payload["session_dir"] = str(guard.settings.session_dir)
    payload["overlay_ascii_dir"] = str(guard.settings.overlay_ascii_dir)
    payload["patchers"] = [str(path) for path in _patcher_paths()]
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("== safe 状态 ==")
        for key, value in payload.items():
            print(f"{key:>18}: {value}")
    return 0


def cmd_redact(args: argparse.Namespace) -> int:
    if getattr(args, "file", None):
        targets = [Path(args.file)]
    else:
        guard = SafeGuard(policy=_policy_from_args(args))
        targets = [guard.settings.session_dir / "diag.log"]
    for target in targets:
        result = sanitize_file(target, keep_backup=getattr(args, "keep_backup", False))
        state = "已脱敏" if result.changed else ("失败" if result.error else "无需改动")
        print(f"{state}: {result.path} lines={result.lines} {result.error or ''}".strip())
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    mode = getattr(args, "set_mode", None)
    if mode:
        policy = SafePolicy.preset(mode)
        path = save_policy(policy)
        print(f"已写入 {path}（mode={policy.mode}）")
        return 0

    if getattr(args, "add_build", None) is not None:
        from ..settings import load_settings

        policy = load_policy()
        build = version.read_build(load_settings().game_dir)
        if not args.add_build:
            # 缺省登记"当前游戏"：同时记时间戳，之后同 build 被改写才会拦
            if build is None:
                print("读不到游戏主程序，无法登记指纹", file=sys.stderr)
                return 1
            policy = version.register_build(policy, build)
            fingerprint = build.fingerprint
        else:
            fingerprint = args.add_build
            if fingerprint in policy.known_builds:
                print(f"已登记: {fingerprint}")
                return 0
            policy = policy.replaced(
                known_builds=tuple(list(policy.known_builds) + [fingerprint]))
        path = save_policy(policy)
        print(f"已登记 build {fingerprint} → {path}（共 {len(policy.known_builds)} 个）")
        return 0

    payload = load_policy().to_dict()
    payload["_path"] = str(policy_path())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _patcher_paths() -> tuple[Path, ...]:
    from .session import PATCHER_DLLS

    return PATCHER_DLLS


def _policy_from_args(args: argparse.Namespace) -> SafePolicy:
    mode = getattr(args, "mode", None)
    known = _load_known_builds()
    if mode:
        return SafePolicy.preset(mode, known_builds=tuple(known))
    policy = load_policy()
    return policy.replaced(known_builds=tuple(known)) if known else policy


def _load_known_builds() -> list[str]:
    try:
        data = json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    known = data.get("known_builds") or []
    return [str(item) for item in known] if isinstance(known, list) else []


def _common_flags() -> argparse.ArgumentParser:
    """全局开关：放在主命令前后都认，且不覆盖已解析的值。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="显示全部条目")
    common.add_argument("--mode", choices=("off", "balanced", "strict"),
                        default=argparse.SUPPRESS, help="临时覆盖策略档位")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_flags()
    parser = argparse.ArgumentParser(
        prog="eolian-safe",
        parents=[common],
        description="Eolian 防封守护：体检 / 门禁 / 覆盖校验 / 痕迹清理 / 日志脱敏",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", parents=[common], help="全量体检")
    audit.add_argument("--overlay", help="覆盖目录（默认取 ASCII 目录或最新构建）")
    audit.set_defaults(func=cmd_audit)

    gate = sub.add_parser("gate", parents=[common], help="只跑门禁；被拦截退出码 2")
    gate.add_argument("--overlay", help="覆盖目录")
    gate.set_defaults(func=cmd_gate)

    verify = sub.add_parser("verify", parents=[common], help="只校验覆盖 WAD 合规性")
    verify.add_argument("--overlay", help="覆盖目录")
    verify.set_defaults(func=cmd_verify)

    clean = sub.add_parser("clean", parents=[common], help="清理本工具留下的痕迹（默认 dry-run）")
    clean.add_argument("--apply", action="store_true", help="真正执行删除")
    clean.set_defaults(func=cmd_clean)

    recover = sub.add_parser("recover", parents=[common], help="重放上次未完成的清理")
    recover.set_defaults(func=cmd_recover)

    status = sub.add_parser("status", parents=[common], help="查看 safe 状态")
    status.set_defaults(func=cmd_status)

    redact = sub.add_parser("redact", parents=[common], help="日志脱敏")
    redact.add_argument("--file", help="目标文件（默认 session/diag.log）")
    redact.add_argument("--keep-backup", action="store_true", help="保留 .raw 原件")
    redact.set_defaults(func=cmd_redact)

    policy = sub.add_parser("policy", parents=[common], help="查看/切换策略/登记 build")
    policy.add_argument("--set-mode", choices=("off", "balanced", "strict"))
    policy.add_argument("--add-build", nargs="?", const="",
                        help="把 build 指纹登记进 known_builds（缺省取当前游戏）")
    policy.set_defaults(func=cmd_policy)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # 控制台/管道编码不一致时优先保证中文可读
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SafeRefusal as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
