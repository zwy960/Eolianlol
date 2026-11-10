"""SafeGuard：把上面所有检查串成"注入前一道门、注入后一条狗"。

    guard = SafeGuard()
    guard.gate(overlay_root)          # 不过就抛 SafeRefusal，一个字节都不注入
    ...注入...
    guard.start_watchdog()            # 游戏退出 → 自动收尾
    guard.teardown("game-exit")       # 也可以手动收
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..diag import error, log, warn
from ..settings import Settings, load_settings
from . import anticheat, footprint, overlay_verify, procinfo, version
from .policy import SafePolicy, load_policy, save_policy
from .redact import sanitize_file
from .report import Report, SafeRefusal

VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor"
PATCHER_DLLS = (
    VENDOR_ROOT / "cslol" / "cslol-dll.dll",
    VENDOR_ROOT / "ltk_patcher_dll.dll",
    VENDOR_ROOT / "ltk_patcher_host.exe",
)


@dataclass
class SafeStatus:
    enabled: bool
    mode: str
    last_passed: bool | None = None
    last_blocks: int = 0
    last_warns: int = 0
    watchdog: bool = False
    teardown_at: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "last_passed": self.last_passed,
            "last_blocks": self.last_blocks,
            "last_warns": self.last_warns,
            "watchdog": self.watchdog,
            "teardown_at": self.teardown_at,
            "notes": self.notes,
        }


class SafeGuard:
    def __init__(self, settings: Settings | None = None, policy: SafePolicy | None = None):
        self.settings = settings or load_settings()
        self.policy = policy or load_policy()
        self._report: Report | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._teardown_at: float | None = None

    # ---------------- 体检 ----------------

    def preflight(self, overlay_root: str | Path,
                  game_dir: str | Path | None = None) -> Report:
        game = Path(game_dir) if game_dir else self.settings.game_dir
        report = Report("safe 注入前体检")
        report.info(
            "SAFE001",
            "策略",
            mode=self.policy.mode,
            enabled=self.policy.enabled,
            game_dir=str(game),
            overlay=str(overlay_root),
        )

        build = version.read_build(game)
        report.merge(version.check_build(build, self.policy, game))
        if build is not None and any(
            f.code in ("VER008", "VER009") for f in report.findings
        ):
            # 官方更新（PE 时间戳前进）→ 自动登记，避免每次小版本都要手动 --add-build
            self.policy = version.register_build(self.policy, build)
            try:
                path = save_policy(self.policy)
                log("SAFE", "已自动登记新 build", fingerprint=build.fingerprint,
                    policy=str(path))
            except OSError as exc:
                report.warn("SAFE008", "新 build 登记失败（本次仍放行）", error=str(exc))
        report.merge(version.check_patcher_eol(list(PATCHER_DLLS), self.policy))

        snapshot = anticheat.probe(game)
        report.merge(anticheat.evaluate(snapshot, self.policy))
        report.info("SAFE002", "环境快照", **snapshot.to_dict())

        report.merge(footprint.audit(game, self.settings.session_dir, self.policy))
        report.merge(overlay_verify.verify(overlay_root, game, self.policy))
        if self.policy.enabled:
            self.policy.apply_to(report, "strict")
        else:
            # off 档：只观察不裁决，避免"关掉 safe 反而什么都跑不了"
            for finding in report.findings:
                if finding.level == "block":
                    finding.level = "warn"
                    finding.detail.setdefault("downgraded", "mode=off")
            report.info("SAFE007", "策略为 off：所有拦截项已降级为告警")
        return report

    def gate(self, overlay_root: str | Path,
             game_dir: str | Path | None = None) -> Report:
        """拦截型门禁：不过就抛 SafeRefusal（调用方不要再往下走）。"""
        if not self.policy.enabled:
            report = Report("safe(off)")
            report.info("SAFE003", "策略为 off：不做任何拦截")
            self._remember(report)
            return report

        report = self.preflight(overlay_root, game_dir)
        self._remember(report)
        if not report.passed:
            error("SAFE", "门禁拦截，放弃本次注入",
                  blocks=len(report.blocking), mode=self.policy.mode,
                  hint="查看 `python -m eolian.safe audit` 的完整结论")
            report.raise_if_blocking("safe 门禁")
        if report.warnings:
            warn("SAFE", "门禁放行但有告警", warns=len(report.warnings))
        else:
            log("SAFE", "门禁通过")
        return report

    # ---------------- 会话 ----------------

    def start_watchdog(self, poll_seconds: float = 2.0, idle_timeout: float = 900.0) -> None:
        """后台等游戏退出，然后自动收尾（只在真正注入过之后调用）。

        ``idle_timeout``：一直没等到游戏进程就自己退出，避免留下空转线程。
        """
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._stop.clear()
        self._watchdog = threading.Thread(
            target=self._watch, args=(poll_seconds, idle_timeout),
            daemon=True, name="safe-watchdog",
        )
        self._watchdog.start()
        log("SAFE", "看门狗已启动", interval=poll_seconds, idle=idle_timeout)

    def _watch(self, poll_seconds: float, idle_timeout: float) -> None:
        saw_game = False
        started = time.monotonic()
        while not self._stop.wait(poll_seconds):
            running = bool(procinfo.find_processes(anticheat.GAME_EXES))
            if running:
                saw_game = True
                continue
            if saw_game:
                self.teardown("game-exit")
                return
            if time.monotonic() - started > idle_timeout:
                log("SAFE", "看门狗空闲超时退出", idle=idle_timeout)
                return

    def stop_watchdog(self) -> None:
        self._stop.set()
        thread = self._watchdog
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._watchdog = None

    def teardown(self, reason: str = "manual") -> Report:
        """收尾：杀残留宿主、删临时目录、脱敏日志、写 journal。"""
        with self._lock:
            report = Report(f"safe 收尾({reason})")
            if self.settings.session_dir.is_dir():
                report.merge(
                    footprint.clean(
                        self.settings.game_dir,
                        self.settings.session_dir,
                        self.policy,
                        dry_run=False,
                        include_ascii=self.policy.cleanup_on_exit,
                        ascii_dir=self.settings.overlay_ascii_dir,
                    )
                )
            if self.policy.redact_logs:
                diag_log = self.settings.session_dir / "diag.log"
                result = sanitize_file(diag_log)
                if result.error:
                    # 日志正被 diag 打开时这里通常失败：交给下次启动的
                    # redact_previous_logs() 处理，这里只记一条明确的记录。
                    report.warn("SAFE004", "运行中日志被占用，脱敏推迟到下次启动",
                                path=str(diag_log), error=result.error)
                elif result.changed:
                    report.ok("SAFE005", "日志已脱敏", path=str(diag_log), lines=result.lines)
                else:
                    report.info("SAFE006", "日志无需脱敏", path=str(diag_log))
            self._teardown_at = time.time()
            log("SAFE", "收尾完成", reason=reason, findings=len(report.findings))
            return report

    def recover(self) -> Report:
        return footprint.recover(self.settings.session_dir)

    def post_injection_check(self) -> Report:
        """注入成功后的二次环境校验（此时游戏进程才真正存在）。

        注入前那次体检通常发生在"游戏还没启动"的时刻，权限/调试器/残留模块
        这几项拿不到真实数据；这里补上，异常就地停补丁器。
        """
        snapshot = anticheat.probe(self.settings.game_dir)
        report = anticheat.evaluate(snapshot, self.policy)
        if self.policy.enabled:
            self.policy.apply_to(report, "strict")
        return report

    # ---------------- 状态 ----------------

    def _remember(self, report: Report) -> None:
        self._report = report

    def status(self) -> SafeStatus:
        report = self._report
        return SafeStatus(
            enabled=self.policy.enabled,
            mode=self.policy.mode,
            last_passed=None if report is None else report.passed,
            last_blocks=0 if report is None else len(report.blocking),
            last_warns=0 if report is None else len(report.warnings),
            watchdog=bool(self._watchdog and self._watchdog.is_alive()),
            teardown_at=self._teardown_at,
        )


_ACTIVE: SafeGuard | None = None
_GUARDED: set[str] = set()

# 日志超过这个大小先轮转，再把轮转后的那份脱敏
MAX_LOG_BYTES = 8 * 1024 * 1024


def redact_previous_logs(session_dir: str | Path | None = None) -> Report:
    """启动时处理**上一份**日志：超限轮转 + 脱敏。

    必须在任何 ``log()`` 之前调用 —— ``diag`` 一旦以追加方式打开 diag.log，
    Windows 上就换不掉这个文件（之前的 teardown 脱敏就是因为这个静默失败）。
    """
    report = Report("日志脱敏（上次运行）")
    base = Path(session_dir) if session_dir else load_settings().session_dir
    targets = (base / "diag.log", base / "floater-console.log")
    for path in targets:
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_LOG_BYTES:
            rotated = path.with_suffix(path.suffix + ".old")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.replace(rotated)
                path = rotated
                report.ok("RDT000", "日志超限已轮转", path=str(path))
            except OSError as exc:
                report.warn("RDT001", "日志轮转失败（继续尝试脱敏）",
                            path=str(path), error=str(exc))
        result = sanitize_file(path)
        if result.error:
            report.warn("RDT002", "脱敏失败", path=str(path), error=result.error)
        elif result.changed:
            report.ok("RDT003", "已脱敏", path=str(path), lines=result.lines)
        else:
            report.info("RDT004", "无需脱敏", path=str(path))
    if not report.findings:
        report.info("RDT005", "没有可处理的历史日志")
    return report


def _key(overlay_root: str | Path) -> str:
    try:
        return str(Path(overlay_root).resolve()).lower()
    except OSError:
        return str(overlay_root).lower()


def is_guarded(overlay_root: str | Path) -> bool:
    """该覆盖目录是否已经过门禁（同一进程内只体检一次）。"""
    return _key(overlay_root) in _GUARDED


def mark_guarded(overlay_root: str | Path) -> None:
    """标记为已过门禁 —— 只用于"内容与已体检目录完全一致"的副本（如 ASCII 拷贝）。"""
    _GUARDED.add(_key(overlay_root))


def ensure_guarded(overlay_root: str | Path) -> Report | None:
    """幂等门禁：已过则直接返回 None，未过则体检（失败抛 SafeRefusal）。

    补丁器的 ``start()`` 里调用它 —— 这样无论调用方走 create_patcher、直接
    构造 PatcherHost，还是第三方脚本，注入点本身都会被拦一次。
    """
    if is_guarded(overlay_root):
        return None
    report = guard_injection(overlay_root)
    return report


def guard_injection(overlay_root: str | Path,
                    game_dir: str | Path | None = None) -> Report | None:
    """给 ``patcher`` 用的单点门禁。

    策略为 off / 组件缺失时静默返回 None —— 绝不让 safe 自己成为"注入失败原因"。
    """
    global _ACTIVE
    try:
        guard = SafeGuard()
    except Exception as exc:  # noqa: BLE001 - 体检不能成为故障源
        warn("SAFE", "safe 初始化失败，按策略放行", error=type(exc).__name__)
        return None
    try:
        report = guard.gate(overlay_root, game_dir)
        _GUARDED.add(_key(overlay_root))
        # 一次会话只留一条看门狗（create_patcher/start 可能被重复调用）；off 档不起
        if guard.policy.enabled and (
            _ACTIVE is None or _ACTIVE._watchdog is None or not _ACTIVE._watchdog.is_alive()
        ):
            _ACTIVE = guard
            guard.start_watchdog()
        return report
    except SafeRefusal:
        raise
