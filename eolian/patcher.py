"""ltk_patcher_host 驱动（官方补丁器，LTK Patcher License）。

协议实测见 docs/04-patcher-protocol.md：
- ``runoverlay <overlay目录>`` 自动执行 start scan，stdin EOF 即退出
- stdout 输出 ``status <t> <state> <detail>`` 行
- stderr 输出日志（不解析）
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .winproc import hidden_process_kwargs

PATCHER_HOST = Path(__file__).resolve().parent.parent / "vendor" / "ltk_patcher_host.exe"
PATCHER_DLL = PATCHER_HOST.with_name("ltk_patcher_dll.dll")
CSLOL_MOD_TOOLS = Path(__file__).resolve().parent.parent / "vendor" / "cslol" / "mod-tools.exe"
CSLOL_DLL = CSLOL_MOD_TOOLS.with_name("cslol-dll.dll")
EOL_RE = re.compile(rb"SCSLOL_EOL_TIMESTAMP=(\d+)")

# 状态机（来自二进制字符串 + 实测）
STATES = ("injecting", "injected", "waiting", "exited", "failed")


@dataclass
class PatcherEvent:
    state: str
    detail: str
    elapsed_s: float


class PatcherError(RuntimeError):
    pass


def read_patcher_eol(dll_path: str | Path = PATCHER_DLL) -> int | None:
    """读取补丁器 DLL 内嵌的过期时间；旧版没有该标记时返回 None。"""
    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return None
    match = EOL_RE.search(data)
    return int(match.group(1)) if match else None


def _safe_gate(overlay_dir: str | Path) -> None:
    """注入前统一过 safe 门禁（幂等；策略 off 或 safe 缺失时静默跳过）。

    放在 ``start()`` 里而不是只放工厂函数：直接构造 PatcherHost 的脚本
    （tools/live_swap、tools/patcher_diag）同样会被拦一次。
    """
    try:
        from .safe import SafeRefusal, ensure_guarded
    except ImportError:
        return
    try:
        ensure_guarded(overlay_dir)
    except SafeRefusal as exc:
        raise PatcherError(f"safe 门禁拒绝注入: {exc}") from exc


class PatcherHost:
    """驱动 ltk_patcher_host.exe 的 runoverlay 模式。"""

    def __init__(self, overlay_dir: str | Path, patcher_host: str | Path = PATCHER_HOST,
                 elevate: bool = False, debug: bool = False):
        self.overlay_dir = Path(overlay_dir)
        self.patcher_host = Path(patcher_host)
        self.elevate = elevate
        self.debug = debug
        self.proc: subprocess.Popen | None = None
        self._events: list[PatcherEvent] = []
        self._logs: list[str] = []          # stderr 日志行（含 DLL 内部 tracing）
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log_reader: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        from .diag import error, log

        if not self.patcher_host.is_file():
            raise PatcherError(f"补丁器不存在: {self.patcher_host}")
        if not self.overlay_dir.is_dir():
            raise PatcherError(f"覆盖目录不存在: {self.overlay_dir}")
        if not (self.overlay_dir / "DATA" / "FINAL").is_dir():
            raise PatcherError(
                f"覆盖目录缺少 DATA/FINAL（不是有效的 overlay）: {self.overlay_dir}")
        _safe_gate(self.overlay_dir)
        eol = read_patcher_eol(self.patcher_host.with_name("ltk_patcher_dll.dll"))
        if eol is not None and time.time() > eol:
            expired = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(eol))
            error("C0", "补丁器 DLL 已过期", eol=expired,
                  hint="更新 vendor/ltk_patcher_host.exe 和 ltk_patcher_dll.dll")
            raise PatcherError(f"补丁器 DLL 已于 {expired} 过期，请更新 vendor 中的补丁器")
        # 正常模式（对齐 ltk-manager 的 host 驱动）：通过 stdin 行协议配置
        # 注意：不用 runoverlay 兼容模式——它不会发送 config loglevel，
        # 导致 DLL 内部日志被默认级别过滤（实测看不到任何 ltk_patcher_dll:: 日志）。
        args = [str(self.patcher_host)]
        if self.elevate:
            args.append("--elevate")
        if self.debug:
            args.append("--opts")
            args.append("debugpatcher")
        log("C1", "启动补丁器（正常模式）", exe=str(self.patcher_host), elevate=self.elevate,
            overlay=str(self.overlay_dir))
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
        except OSError as exc:
            raise PatcherError(f"补丁器启动失败: {exc}") from exc
        # UAC 拒绝检测：提权子进程若被拒绝会立即退出
        import time as _time

        _time.sleep(1.5)
        if self.proc.poll() is not None:
            rc = self.proc.returncode
            stderr_tail = ""
            if self.proc.stderr:
                try:
                    stderr_tail = self.proc.stderr.read()[:300]
                except OSError:
                    pass
            self.proc = None
            hint = ("UAC 授权被拒绝或提权失败" if self.elevate
                    else "补丁器异常退出")
            error("C2", "补丁器启动后立即退出", returncode=rc,
                  stderr=stderr_tail.strip())
            raise PatcherError(f"补丁器启动失败（{hint}，code={rc}）")
        # 发送配置序列（对齐 ltk-manager protocol.rs）
        # loglevel: 32=Debug / 16=Info
        # flags: 4 = OPT_OUT_AH_V1 —— AH（anti-skinhack）扫描失败降级为警告而非
        #        禁用 overlay（国服 ACE 环境下 WAD 扫描被 c0000229 阻止，必须开）
        log_level = 32 if self.debug else 16
        flags = 4
        prefix = str(self.overlay_dir).rstrip("\\/") + "\\"
        commands = (
            f"config loglevel {log_level}\n"
            f"config flags {flags}\n"
            f"config prefix {prefix}\n"
            "start scan\n"
        )
        log("C2", "发送配置", commands=commands.replace("\n", " | "),
            note="flags=4(OPT_OUT_AH_V1) AH 扫描失败降级为警告")
        try:
            self.proc.stdin.write(commands)
            self.proc.stdin.flush()
        except OSError as exc:
            raise PatcherError(f"补丁器配置发送失败（stdin 已关闭）: {exc}") from exc
        log("C2", "补丁器进程存活", pid=self.proc.pid)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._log_reader = threading.Thread(target=self._read_log_loop, daemon=True)
        self._log_reader.start()

    def stop(self) -> None:
        """优雅退出：先发 stop 命令，再关 stdin 触发 EOF（正常模式协议）。"""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for thread in (self._reader, self._log_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1)
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self.proc = None

    def wait(self, timeout: float = 30.0) -> None:
        if self.proc is None:
            raise PatcherError("补丁器未启动")
        self.proc.wait(timeout=timeout)

    # ---- 事件 ----

    def _read_loop(self) -> None:
        """解析宿主 stdout 行协议：
        - `status <t> <state> <detail>` —— 状态事件
        - `dll <ts> <pid> <tid> <level> <msg>` —— DLL 内部日志（ltk-manager protocol.rs 格式）
        - 其余行保留到 logs 供诊断
        """
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("status "):
                parts = line.split(" ", 3)
                if len(parts) < 3:
                    continue
                elapsed = float(parts[1]) if len(parts) > 1 else 0.0
                state = parts[2]
                detail = parts[3] if len(parts) > 3 else ""
                with self._lock:
                    self._events.append(PatcherEvent(state, detail, elapsed))
            else:
                # dll 日志与其他 stdout 行全部保留（DLL 内部 tracing 走这里）
                with self._lock:
                    self._logs.append(line)

    @property
    def dll_logs(self) -> list[str]:
        """仅 DLL 内部日志（dll <ts> <pid> <tid> <level> <msg>）。"""
        with self._lock:
            return [ln for ln in self._logs if ln.startswith("dll ")]

    def _read_log_loop(self) -> None:
        """读取 stderr 日志（宿主 + DLL tracing 输出）。"""
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            with self._lock:
                self._logs.append(line.rstrip())

    @property
    def logs(self) -> list[str]:
        with self._lock:
            return list(self._logs)

    @property
    def events(self) -> list[PatcherEvent]:
        with self._lock:
            return list(self._events)

    @property
    def last_state(self) -> str | None:
        with self._lock:
            return self._events[-1].state if self._events else None

    def wait_for_state(self, state: str, timeout: float = 60.0) -> PatcherEvent:
        """等待指定状态出现（如 injected）。超时抛 PatcherError。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for event in self._events:
                    if event.state == state:
                        return event
            if self.proc is not None and self.proc.poll() is not None:
                raise PatcherError(f"补丁器提前退出（code={self.proc.returncode}），"
                                   f"最后事件: {self.events[-1] if self.events else '无'}")
            time.sleep(0.05)
        raise PatcherError(f"等待状态 {state} 超时；事件列表: {self.events[-5:]}")

    def __enter__(self) -> "PatcherHost":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class CslolPatcher:
    """驱动 cslol mod-tools 的 runoverlay 模式。

    当前国服客户端已验证 cslol-dll 可以 hook；mod-tools 必须与游戏进程同权限，
    因此调用方需要以管理员身份运行本工具。
    """

    STATUS_STATES = {
        "Waiting for league match to start": "injecting",
        "Found League": "injecting",
        "Wait initialized": "injecting",
        "Scanning": "injecting",
        "Saving": "injecting",
        "Wait patchable": "injecting",
        "Patching": "injecting",
        "Waiting for exit": "injecting",
        "League exited": "exited",
    }

    def __init__(self, overlay_dir: str | Path, mod_tools: str | Path = CSLOL_MOD_TOOLS,
                 debug: bool = False):
        self.overlay_dir = Path(overlay_dir).resolve()
        self.mod_tools = Path(mod_tools).resolve()
        self.debug = debug
        self.proc: subprocess.Popen | None = None
        self._events: list[PatcherEvent] = []
        self._logs: list[str] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log_reader: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._injected = False

    def start(self) -> None:
        from .diag import log
        from .settings import load_settings

        if self.proc is not None and self.proc.poll() is None:
            raise PatcherError("cslol 补丁器已经在运行")
        with self._lock:
            self._events.clear()
            self._logs.clear()
        self._injected = False
        if not self.mod_tools.is_file():
            raise PatcherError(f"cslol mod-tools 不存在: {self.mod_tools}")
        if not self.mod_tools.with_name("cslol-dll.dll").is_file():
            raise PatcherError("cslol-dll.dll 不存在，必须与 mod-tools.exe 放在同一目录")
        if not (self.overlay_dir / "DATA" / "FINAL").is_dir():
            raise PatcherError(f"覆盖目录缺少 DATA/FINAL: {self.overlay_dir}")
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise PatcherError("cslol 注入需要管理员权限，请以管理员身份启动终端")
        except AttributeError:
            pass
        _safe_gate(self.overlay_dir)

        config_file = load_settings().session_dir / "cslol-runtime.config"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("", encoding="utf-8")
        args = [
            str(self.mod_tools),
            "runoverlay",
            str(self.overlay_dir),
            str(config_file),
            "--opts:" + ("debugpatcher" if self.debug else "none"),
        ]
        log("C1", "启动 cslol 补丁器", exe=str(self.mod_tools), overlay=str(self.overlay_dir))
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.mod_tools.parent),
                **hidden_process_kwargs(),
            )
        except OSError as exc:
            raise PatcherError(f"cslol 补丁器启动失败: {exc}") from exc
        time.sleep(1.2)
        if self.proc.poll() is not None:
            rc = self.proc.returncode
            stderr = self.proc.stderr.read()[:500] if self.proc.stderr else ""
            self.proc = None
            raise PatcherError(f"cslol 补丁器启动后立即退出（code={rc}）: {stderr.strip()}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._log_reader = threading.Thread(target=self._read_log_loop, daemon=True)
        self._log_reader.start()
        log("C2", "cslol 补丁器运行中", pid=self.proc.pid)

    def stop(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for thread in (self._reader, self._log_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1)
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self.proc = None

    def _append_event(self, state: str, detail: str) -> None:
        with self._lock:
            if self._events and self._events[-1].state == state:
                return
            self._events.append(PatcherEvent(state, detail, time.monotonic()))
        if state == "injected" and not self._injected:
            self._injected = True
            self._monitor = threading.Thread(target=self._monitor_game_exit, daemon=True)
            self._monitor.start()

    def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw_line in self.proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            with self._lock:
                self._logs.append(line)
            if line.startswith("Status: "):
                status = line[len("Status: "):]
                self._append_event(self.STATUS_STATES.get(status, "injecting"), status)
            elif line.startswith("[DLL] "):
                detail = line[len("[DLL] "):]
                if "redirected wad" in detail.lower():
                    self._append_event("injected", detail)
                elif any(key in detail for key in ("Init in process", "Init done")):
                    self._append_event("injecting", detail)
                elif "error:" in detail.lower():
                    self._append_event("failed", detail)
        if self.proc is not None and self.proc.poll() is not None:
            self._append_event("exited", f"mod-tools exited ({self.proc.returncode})")

    def _read_log_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw_line in self.proc.stderr:
            line = raw_line.rstrip()
            if line:
                with self._lock:
                    self._logs.append(line)
                if "error:" in line.lower():
                    self._append_event("failed", line)

    def _monitor_game_exit(self) -> None:
        """cslol 会持续等待下一局；发现游戏进程退出后通知 UI 结束本次会话。"""
        saw_game = False
        while self.proc is not None and self.proc.poll() is None:
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq League of Legends.exe", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    **hidden_process_kwargs(),
                )
                running = "League of Legends.exe" in result.stdout
            except (OSError, subprocess.TimeoutExpired):
                running = True
            if running:
                saw_game = True
            elif saw_game:
                self._append_event("exited", "game exited")
                return
            time.sleep(2)

    @property
    def logs(self) -> list[str]:
        with self._lock:
            return list(self._logs)

    @property
    def events(self) -> list[PatcherEvent]:
        with self._lock:
            return list(self._events)

    @property
    def last_state(self) -> str | None:
        with self._lock:
            return self._events[-1].state if self._events else None

    def wait_for_state(self, state: str, timeout: float = 60.0) -> PatcherEvent:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for event in self._events:
                    if event.state == state:
                        return event
            if self.proc is not None and self.proc.poll() is not None:
                raise PatcherError(f"cslol 补丁器提前退出（code={self.proc.returncode}）")
            time.sleep(0.05)
        raise PatcherError(f"等待状态 {state} 超时；事件列表: {self.events[-5:]}")

    def __enter__(self) -> "CslolPatcher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def create_patcher(overlay_dir: str | Path, *, debug: bool = False) -> CslolPatcher | PatcherHost:
    """优先 cslol；其组件缺失时回退到 LTK patcher。

    注入前统一过一遍 ``eolian.safe`` 门禁（策略 off 时自动跳过）：
    版本指纹、补丁器 EOL、环境、本机痕迹、覆盖合规，任一硬失败即拒绝注入。
    """
    _safe_gate(overlay_dir)

    if CSLOL_MOD_TOOLS.is_file() and CSLOL_DLL.is_file():
        return CslolPatcher(overlay_dir, debug=debug)
    return PatcherHost(overlay_dir, elevate=True, debug=debug)
