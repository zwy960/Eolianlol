"""Eolian Web UI（pywebview + Edge WebView2 渲染）。

架构：
    eolian/ui/web/  —— 前端（HTML/CSS/JS，白色主题）
    web_window.py   —— 桥接层：pywebview JsApi 封装 catalog / swapper / gameflow / diag

后端模块（catalog、overlay、patcher、safe、lcu…）零改动；
前端逻辑（轮询、换肤流程编排）从 float_window.py 迁移至此，旧 Tk 界面保留作回退。

用法：
    python -m tools.floater
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..catalog import SkinCatalog
from ..diag import add_listener, remove_listener
from ..endpoints import Gameflow
from ..lcu import RiotClient, default_client_root, discover
from ..overlay import SkinSwapper
from ..settings import Settings

WEB_DIR = Path(__file__).resolve().parent / "web"


class _LogBuffer:
    """diag 日志环形缓冲：前端按游标增量拉取。"""

    def __init__(self, maxlen: int = 3000) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = 0

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line.rstrip())
            self._counter += 1

    def since(self, cursor: int) -> tuple[list[str], int]:
        with self._lock:
            total = self._counter
            if cursor >= total:
                return [], total
            skip = max(0, total - len(self._lines) - cursor)  # 缓冲截断时跳过丢失段
            lines = list(self._lines)[len(self._lines) - (total - cursor) + skip:]
            return lines, total

    def clear(self) -> int:
        with self._lock:
            self._lines.clear()
            return self._counter


class WebApi:
    """暴露给前端 JS 的桥接 API（pywebview js_api）。"""

    def __init__(self, catalog: SkinCatalog, swapper: SkinSwapper,
                 gameflow: Gameflow | None):
        self.catalog = catalog
        self.swapper = swapper
        self.gameflow = gameflow
        self.logs = _LogBuffer()

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = False
        # 当前补丁器实例；构建完成后 _busy 立刻放开，靠这两个字段判断还能不能再换
        self._patcher_host = None
        self._injected = False
        self._catalog_refreshing = False
        self._catalog_version = 0
        # 本地换肤覆盖：注入成功后"当前皮肤"显示换上的皮肤
        self._active_swap_skin_id = 0
        self._active_swap_champion_id = 0
        # 快照字段（前端 poll 读取）
        self.connected = False
        self.phase = ""
        self.champion_id = 0
        self.current_skin_id = 0
        self.build_text = ""
        self.inject_text = ""
        self.status_text = "等待客户端连接"
        self.status_level = "muted"
        self._listener = self.logs.append
        add_listener(self._listener)

    # ---- 内部状态更新 ----

    def _set_status(self, text: str, level: str = "muted") -> None:
        with self._lock:
            self.status_text = text
            self.status_level = level
        from ..diag import log

        log("UI", text)

    def _set_panel(self, build: str | None = None, inject: str | None = None) -> None:
        with self._lock:
            if build is not None:
                self.build_text = build
            if inject is not None:
                self.inject_text = inject

    def _apply_state_fields(self, state) -> None:
        with self._lock:
            self.connected = True
            self.phase = state.phase or ""
            champion_id = state.champion_id if state.in_champ_select else 0
            # 英雄变了（离开选人/换英雄）就清掉本地换肤覆盖
            if champion_id != self._active_swap_champion_id:
                self._active_swap_skin_id = 0
                self._active_swap_champion_id = 0
            self.champion_id = champion_id
            self.current_skin_id = state.selected_skin_id

    # ---- 轮询线程 ----

    _REDISCOVER_AFTER_FAILURES = 20  # 连续失败约 10s 后重新探测 lockfile（防凭据过期）

    def poll_loop(self) -> None:
        """持续轮询客户端状态；只在真正可用时标记"已连接"。

        连接策略：拿到 gameflow 后反复调用 champ_select_state()，
        失败不立刻丢弃（避免客户端短暂无响应时状态来回闪烁）；
        连续失败超过阈值才重新探测 lockfile。
        """
        client: RiotClient | None = None
        last_key = None
        last_connected = None
        fail_count = 0
        while not self._stop.is_set():
            # 第一阶段：确保有客户端连接凭据
            if client is None:
                try:
                    lockfile = discover(default_client_root())
                    client = RiotClient(lockfile)
                    self.gameflow = Gameflow(client)
                except Exception:
                    if last_connected is not False:
                        self._set_status("等待客户端连接…")
                        with self._lock:
                            self.connected = False
                            self.champion_id = 0
                        last_connected = False
                    self._stop.wait(2)
                    continue
            # 第二阶段：验证连接真正可用（能成功拉到状态才算连接）
            try:
                state = self.gameflow.champ_select_state()
            except Exception:
                fail_count += 1
                if last_connected is not False:
                    self._set_status("等待客户端连接…")
                    with self._lock:
                        self.connected = False
                        self.champion_id = 0
                    last_connected = False
                    last_key = None
                if fail_count >= self._REDISCOVER_AFTER_FAILURES:
                    client = None  # 凭据可能过期（客户端重启），重新探测
                    fail_count = 0
                self._stop.wait(0.5)
                continue
            fail_count = 0
            if last_connected is not True:
                self._set_status("客户端已连接，等待进入英雄选择", "ok")
                from ..diag import log

                log("UI", "已连接客户端")
                last_connected = True
            key = (state.phase, state.champion_id, state.selected_skin_id)
            if key != last_key:
                last_key = key
                from ..diag import log

                log("UI", "状态变化", phase=state.phase,
                    champion=state.champion_id, skin=state.selected_skin_id)
            self._apply_state_fields(state)
            self._stop.wait(0.5)

    def catalog_autocheck(self) -> None:
        """启动时若目录缓存缺失/过期则静默刷新（客户端没开就跳过）。"""
        from ..catalog import catalog_age_days, refresh_from_lcu
        from ..diag import log

        age = catalog_age_days()
        if age is not None and age < 3:
            return
        try:
            info = refresh_from_lcu()
        except Exception as exc:  # noqa: BLE001
            log("CATALOG", "启动自动刷新跳过", reason=str(exc)[:80])
            return
        self.catalog.reload()
        with self._lock:
            self._catalog_version += 1
        log("CATALOG", "启动自动刷新完成", skins=info.get("skins_count"), age_days=age)

    # ---- 换肤流程（编排逻辑与 float_window._swap_worker 一致）----

    @staticmethod
    def _lcu_patch_enabled() -> bool:
        try:
            from ..safe import lcu_patch_enabled

            return lcu_patch_enabled()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _post_injection_check():
        try:
            from ..safe import SafeGuard

            return SafeGuard().post_injection_check()
        except Exception as exc:  # noqa: BLE001
            from ..diag import warn

            warn("SAFE", "注入后校验不可用", error=type(exc).__name__)
            return None

    def _overlay_mode_text(self) -> str:
        if self.champion_id <= 0:
            return "本地覆盖：等待进入选人"
        if self._lcu_patch_enabled():
            return "本地覆盖：已开启客户端皮肤同步"
        skin_num = self.current_skin_id % 1000
        if skin_num == 0:
            return "本地覆盖：当前为基础皮肤，换肤可生效"
        return f"本地覆盖：当前为皮肤{skin_num}，改回基础皮肤才生效"

    def swap(self, skin_id: int) -> dict:
        """前端点击"开始换肤"：校验后启动后台线程。"""
        if self._patcher_host is not None and self._injected:
            return {"ok": False,
                    "message": "本局已经注入过了，换肤要等这一局结束（下一局重新选）"}
        if self._busy:
            return {"ok": False, "message": "已有换肤任务进行中"}
        if self.champion_id <= 0:
            return {"ok": False, "message": "未进入选人阶段，无法换肤"}
        champion = self.catalog.champion(self.champion_id)
        skin = self.catalog.skin(int(skin_id))
        if champion is None or skin is None:
            return {"ok": False, "message": "皮肤目录中没有该皮肤"}
        if skin.is_base or skin.skin_num == 0:
            return {"ok": False, "message": "基础皮肤无需换肤，请选择其他皮肤"}
        if (not self._lcu_patch_enabled()
                and (self.current_skin_id % 1000) != 0):
            return {
                "ok": False,
                "message": (f"客户端当前选的是皮肤{self.current_skin_id % 1000}，"
                            "本地覆盖只对基础皮肤生效：请先切回基础皮肤再点换肤"),
            }
        self._busy = True
        self._set_panel("构建：准备中", "注入：等待")
        self._set_status(f"构建 {skin.name} …", "info")
        threading.Thread(
            target=self._swap_worker, args=(champion.alias.lower(), skin),
            daemon=True, name="swap-worker",
        ).start()
        return {"ok": True, "message": ""}

    def _stop_patcher(self) -> None:
        """停掉当前补丁器（换皮肤/取消构建时用）。"""
        host = self._patcher_host
        self._patcher_host = None
        self._injected = False
        if host is None:
            return
        try:
            host.stop()
        except Exception:  # noqa: BLE001 - 旧实例停不掉也不能挡住新的一次
            pass

    def cancel_swap(self) -> dict:
        """前端"取消本次构建"：停掉还没注入的补丁器，让用户重新选皮肤。"""
        from ..diag import log

        if self._patcher_host is None:
            return {"ok": False, "message": "当前没有待取消的构建"}
        if self._injected:
            return {"ok": False, "message": "本局已经注入，无法取消；等这一局结束再换"}
        log("SWAP", "用户取消本次构建")
        threading.Thread(target=self._cancel_worker, daemon=True,
                         name="cancel-build").start()
        return {"ok": True, "message": "正在取消…"}

    def _cancel_worker(self) -> None:
        self._stop_patcher()
        self._busy = False
        self._set_panel("构建：已取消", "注入：未启动")
        self._set_status("已取消本次构建，可以重新选择皮肤再点【开始换肤】", "muted")

    def _swap_worker(self, champion_alias: str, skin) -> None:
        from ..diag import error, log, warn

        try:
            # 0) 换皮肤前先收掉上一轮的补丁器（构建完还没进游戏时允许改主意）
            self._stop_patcher()
            # 1) 构建覆盖 WAD
            t0 = time.monotonic()
            log("SWAP", "开始构建", champion=champion_alias, skin_num=skin.skin_num)
            result = self.swapper.build_overlay(champion_alias, skin.skin_num)
            log("SWAP", "构建完成", elapsed_s=round(time.monotonic() - t0, 1),
                overlay=str(result.overlay_root))
            self._set_panel("构建：完成", "注入：准备")
            self._set_status(f"构建完成 {time.monotonic() - t0:.0f}s，启动补丁器…", "info")
            # 2) 防封门禁
            self._set_panel("构建：完成", "注入：安全检查")
            self._set_status("安全检查中：版本、环境、覆盖合规…", "info")
            from ..safe import SafeRefusal, guard_injection, mark_guarded

            try:
                gate = guard_injection(result.overlay_root)
            except SafeRefusal as exc:
                blocked = exc.report.blocking if exc.report else []
                for finding in blocked:
                    detail = " ".join(f"{k}={v}" for k, v in finding.detail.items())
                    error("SAFE", f"{finding.code} {finding.message}", detail=detail[:200])
                reason = exc.report.short_reason() if exc.report else str(exc)
                self._set_panel("构建：完成", "注入：已拦截")
                self._set_status(f"安全检查未通过：{reason}", "danger")
                return
            if gate is not None and gate.warnings:
                for finding in gate.warnings:
                    warn("SAFE", f"{finding.code} {finding.message}")
                self._set_status(f"安全检查通过（{len(gate.warnings)} 条告警，详见日志）")
            else:
                self._set_status("安全检查通过")
            # 3) ASCII 路径副本 + 启动补丁器
            overlay_ascii = self.swapper.copy_to_ascii(result.overlay_root)
            mark_guarded(overlay_ascii)
            from ..patcher import create_patcher

            debug_patcher = os.environ.get("EOLIAN_DEBUG_PATCHER", "").strip() == "1"
            host = create_patcher(overlay_ascii, debug=debug_patcher)
            host.start()
            self._patcher_host = host
            self._injected = False
            # 构建完成后立刻放开忙碌标记：前端轮询会把按钮恢复成"开始换肤"
            self._busy = False
            log("SWAP", "补丁器已启动，等待游戏进程",
                pid=host.proc.pid if host.proc else None)
            self._set_panel("构建：完成", "注入：等待游戏")
            self._set_status("补丁器就位！进游戏前还能再换别的皮肤", "ok")
            # 4) LCU 同步（默认关闭）
            if self.gameflow is not None and self._lcu_patch_enabled():
                try:
                    if self.gameflow.select_skin(skin.id, force=True):
                        log("SWAP", "LCU 已同步皮肤", skin_id=skin.id)
                        self._set_status(f"已选择 {skin.name}，补丁器待命", "ok")
                except Exception as exc:
                    log("SWAP", "LCU 同步失败（不影响游戏内换肤）", error=str(exc))
            # 5) 监视补丁器直到本局结束
            last_state = ""
            seen_logs = 0
            while host.proc is not None and host.proc.poll() is None:
                state = host.last_state
                if state and state != last_state:
                    last_state = state
                    detail = host.events[-1].detail if host.events else ""
                    log("PATCHER", f"状态: {state} {detail}")
                    if state == "injected":
                        self._injected = True
                        post = self._post_injection_check()
                        if post is not None and not post.passed:
                            for finding in post.blocking:
                                error("SAFE", f"{finding.code} {finding.message}")
                            self._set_panel("构建：完成", "注入：已拦截")
                            self._set_status(
                                f"注入后校验未通过，已停止：{post.short_reason()}", "danger")
                            break
                        with self._lock:
                            # 注入成功：界面"当前皮肤"显示换上的皮肤
                            self._active_swap_skin_id = skin.id
                            self._active_swap_champion_id = self.champion_id
                        self._set_panel("构建：完成", "注入：成功")
                        self._set_status(f"注入成功！游戏内应为 {skin.name}", "ok")
                    elif state == "failed":
                        self._set_panel("构建：完成", "注入：失败")
                        self._set_status(f"注入失败: {detail}", "danger")
                        error("PATCHER", "注入失败", detail=detail,
                              hint="游戏以管理员运行时补丁器必须 --elevate（已默认）")
                    elif state == "exited":
                        self._set_status("本局结束——下局请重新选择皮肤")
                        log("SWAP", "游戏退出（dll detached），停止补丁器")
                        break
                    else:
                        self._set_status(f"补丁器: {state} {detail}", "info")
                for line in host.logs[seen_logs:]:
                    if line.startswith("dll "):
                        log("DLL", line[4:])
                    else:
                        log("HOST", line)
                seen_logs = len(host.logs)
                time.sleep(1)
            host.stop()
            log("SWAP", "补丁器已停止")
            if self._patcher_host is host:
                self._patcher_host = None
                self._injected = False
                self._set_status("补丁器已停止（本局结束）")
        except Exception as exc:
            self._set_panel("构建：失败", "注入：失败")
            error("SWAP", "换肤流程失败", error=str(exc),
                  hint="按环节标记定位；详见 session/diag.log")
            self._set_status(f"换肤失败: {exc}", "danger")
        finally:
            self._busy = False

    # ---- 目录刷新 ----

    def refresh_catalog(self) -> dict:
        if self._catalog_refreshing:
            return {"ok": False, "message": "刷新进行中"}
        from ..diag import log

        self._catalog_refreshing = True
        self._set_status("正在从客户端拉取皮肤目录…", "info")
        log("CATALOG", "手动刷新皮肤目录")

        def worker() -> None:
            try:
                from ..catalog import refresh_from_lcu

                info = refresh_from_lcu()
                source = self.catalog.reload()
                with self._lock:
                    self._catalog_version += 1
                count = info.get("skins_count")
                log("CATALOG", "目录已更新", skins=count, source=source)
                self._set_status(f"皮肤目录已更新（{count} 款皮肤，来源 {source}）", "ok")
            except Exception as exc:  # noqa: BLE001
                from ..diag import log as _log

                _log("CATALOG", "目录刷新失败", error=str(exc))
                self._set_status(f"目录刷新失败：{exc}", "danger")
            finally:
                self._catalog_refreshing = False

        threading.Thread(target=worker, daemon=True, name="catalog-refresh").start()
        return {"ok": True, "message": ""}

    # ---- 前端 API ----

    def bootstrap(self) -> dict:
        return {
            "catalog_version": self._catalog_version,
            "status_text": self.status_text,
            "status_level": self.status_level,
        }

    def poll(self) -> dict:
        with self._lock:
            champion = self.catalog.champion(self.champion_id) if self.champion_id > 0 else None
            # 本地换肤生效后，"当前皮肤"显示换上的皮肤（LCU 仍停留在基础皮肤）
            display_skin_id = self.current_skin_id
            if (self._active_swap_skin_id
                    and self._active_swap_champion_id == self.champion_id):
                display_skin_id = self._active_swap_skin_id
            current = self.catalog.skin(display_skin_id)
            return {
                "connected": self.connected,
                "phase": self.phase,
                "in_champ_select": self.champion_id > 0 or bool(self.phase),
                "champion_id": self.champion_id,
                "champion_name": champion.name if champion else "",
                "current_skin_id": display_skin_id,
                "current_skin_name": current.name if current else "",
                "skins_count": (
                    len(self.catalog.skins_of(self.champion_id))
                    if self.champion_id > 0 else 0
                ),
                "busy": self._busy,
                "can_cancel": (self._patcher_host is not None and not self._injected),
                "build_text": self.build_text,
                "inject_text": self.inject_text,
                "status_text": self.status_text,
                "status_level": self.status_level,
                "catalog_version": self._catalog_version,
                "catalog_refreshing": self._catalog_refreshing,
                "overlay_mode": self._overlay_mode_text(),
            }

    def get_skins(self, champion_id: int) -> dict:
        skins = self.catalog.skins_of(int(champion_id))
        return {
            "skins": [
                {
                    "id": s.id,
                    "skin_num": s.skin_num,
                    "name": s.name,
                    "is_base": s.is_base,
                }
                for s in skins
            ]
        }

    def get_logs(self, cursor: int) -> dict:
        lines, nxt = self.logs.since(int(cursor))
        return {"lines": lines, "next": nxt}

    def clear_logs(self) -> int:
        return self.logs.clear()

    def open_log_folder(self) -> dict:
        try:
            os.startfile(self.swapper.session_dir)  # type: ignore[attr-defined]
            return {"ok": True, "message": ""}
        except (AttributeError, OSError) as exc:
            return {"ok": False, "message": str(exc)}

    def shutdown(self) -> None:
        self._stop.set()
        remove_listener(self._listener)


def main() -> int:
    import webview

    # 首次运行路径探测/启动清理复用旧 UI 的逻辑（仅对话框与日志脱敏，非后端）
    from .float_window import _startup_housekeeping, prepare_settings

    settings = prepare_settings()
    if settings is None:
        return 2
    _startup_housekeeping(settings)
    settings = _ensure_builder(settings)

    catalog = SkinCatalog()
    swapper = SkinSwapper(
        game_dir=settings.game_dir,
        session_dir=settings.session_dir,
        learn_overlay=settings.learn_overlay,
    )
    gameflow = None
    try:
        lockfile = discover(settings.client_root)
        gameflow = Gameflow(RiotClient(lockfile))
    except Exception:
        pass

    api = WebApi(catalog, swapper, gameflow)
    window = webview.create_window(
        "Eolian - 英雄联盟换肤助手",
        url=str(WEB_DIR / "index.html"),
        js_api=api,
        width=1180,
        height=740,
        min_size=(1000, 620),
        background_color="#0a0f1a",
    )

    threading.Thread(target=api.poll_loop, daemon=True, name="poll-loop").start()
    threading.Thread(target=api.catalog_autocheck, daemon=True).start()

    try:
        webview.start()
    finally:
        api.shutdown()
    return 0


def _ensure_builder(settings: Settings) -> Settings:
    """WebView 窗口模式下构建器缺失时报错到日志而不是弹原生框。"""
    from ..diag import error

    if not settings.valid_builder:
        error("SETUP", "缺少构建器 eolian-overlay.exe", path=str(settings.learn_overlay))
    if not settings.valid_game:
        error("SETUP", "游戏目录无效", game_dir=str(settings.game_dir))
    return settings


if __name__ == "__main__":
    raise SystemExit(main())
