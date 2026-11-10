"""Eolian 换肤助手桌面窗口。

界面：
- 原生系统标题栏，可最小化、缩放和从任务栏恢复
- 深色桌面布局：运行状态、皮肤列表、已选皮肤详情
- 应用内实时终端，便于查看构建与注入日志

流程（选人阶段操作）：
    选皮肤 → 构建覆盖 WAD（增量缓存）→ 提权补丁器就位 → 游戏启动时注入
    → 游戏内生效；真实对局同时 PATCH LCU 同步客户端 UI

用法：
    python -m tools.floater
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..catalog import Champion, Skin, SkinCatalog
from ..diag import add_listener, remove_listener
from ..endpoints import ChampSelectState, Gameflow, PhasePoller
from ..lcu import RiotClient, default_client_root
from ..overlay import SkinSwapper
from ..settings import (
    DEFAULT_CONFIG_PATH,
    RESOURCE_ROOT,
    Settings,
    detect_game_dir,
    load_settings,
    save_settings,
)

WINDOW_BG = "#0c1015"
PANEL_BG = "#131a22"
PANEL_ALT = "#1a2330"
BORDER = "#273241"
ACCENT = "#e0b15b"
TEXT = "#f1f5f9"
MUTED = "#8b98a9"
DANGER = "#ef7468"
OK = "#5fd0a0"
INFO = "#70b7ff"

WINDOW_W = 1160
WINDOW_H = 640


class SlimScrollbar(tk.Canvas):
    """与应用配色一致的垂直滚动条。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        command,
        trough_color: str,
        thumb_color: str,
        width: int = 10,
    ) -> None:
        super().__init__(
            master,
            width=width,
            bg=trough_color,
            borderwidth=0,
            highlightthickness=0,
            cursor="arrow",
        )
        self._command = command
        self._thumb_color = thumb_color
        self._first = 0.0
        self._last = 1.0
        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)

    def set(self, first: str, last: str) -> None:
        self._first = float(first)
        self._last = float(last)
        self._draw()

    def _draw(self) -> None:
        self.delete("thumb")
        height = max(1, self.winfo_height())
        width = max(1, self.winfo_width())
        start = int(height * self._first)
        end = int(height * self._last)
        if end - start < 24:
            end = min(height, start + 24)
            start = max(0, end - 24)
        self.create_rectangle(
            width - 7,
            start + 2,
            width - 2,
            max(start + 3, end - 2),
            fill=self._thumb_color,
            outline="",
            tags="thumb",
        )

    def _jump(self, event) -> None:
        height = max(1, self.winfo_height())
        fraction = max(0.0, min(1.0, event.y / height))
        self._command("moveto", fraction)


class SkinFloater:
    def __init__(self, catalog: SkinCatalog, swapper: SkinSwapper,
                 gameflow: Gameflow | None):
        self.catalog = catalog
        self.swapper = swapper
        self.gameflow = gameflow
        self.champion_id = 0
        self.current_skin_id = 0
        self._busy = False
        self._skins_by_id: dict[str, Skin] = {}
        self._terminal_lines: deque[str] = deque(maxlen=3000)
        self._terminal_window: tk.Toplevel | None = None
        self._terminal_text: tk.Text | None = None
        self._poll_stop: threading.Event | None = None
        # 当前正在等待/已注入的补丁器；用于"再换一次皮肤"时先停旧的
        self._patcher_host = None
        self._injected = False
        # 跨线程 UI 更新：后台线程只往队列投递，主线程 after 轮询消费（tkinter 线程安全模式）
        self._ui_queue: queue.Queue = queue.Queue()
        self._terminal_listener = lambda line: self._ui_queue.put(("log", line))
        add_listener(self._terminal_listener)

        self.root = tk.Tk()
        self._log_safe_status()
        self.root.title("Eolian - 英雄联盟换肤助手")
        icon_path = RESOURCE_ROOT / "packaging" / "app.ico"
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.root.overrideredirect(False)
        self.root.resizable(True, True)
        self.root.minsize(980, 600)
        self.root.configure(bg=WINDOW_BG)
        self._build_ui()
        self._place_default()
        self._apply_native_window_style(self.root)
        self._ui_queue.put(("log", "[floater] Eolian 已启动，等待客户端连接"))

    @staticmethod
    def _log_safe_status() -> None:
        """启动自检：只写日志，不在界面上暴露防封状态。"""
        from ..diag import log, warn

        try:
            from ..safe import SafeGuard, policy_path

            status = SafeGuard().status()
        except Exception as exc:  # noqa: BLE001 - 自检失败不能挡住 UI
            warn("SAFE", "防封守护不可用（未打包或导入失败）", error=type(exc).__name__)
            return
        if not status.enabled:
            log("SAFE", "防封守护已关闭", mode=status.mode, policy=str(policy_path()))
            return
        log("SAFE", "防封守护启用", mode=status.mode, policy=str(policy_path()))

    # ---- UI ----

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Eolian.Treeview",
            background=PANEL_BG,
            fieldbackground=PANEL_BG,
            foreground=TEXT,
            borderwidth=0,
            rowheight=34,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Eolian.Treeview",
            background=[("selected", "#344052")],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Eolian.Treeview.Heading",
            background=PANEL_ALT,
            foreground=MUTED,
            borderwidth=0,
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        style.map(
            "Eolian.Treeview.Heading",
            background=[("active", PANEL_ALT)],
        )
        style.configure(
            "Eolian.Vertical.TScrollbar",
            background="#394556",
            troughcolor=PANEL_BG,
            arrowcolor=MUTED,
            bordercolor=PANEL_BG,
            lightcolor="#394556",
            darkcolor="#1e2733",
            borderwidth=0,
            arrowsize=14,
        )
        style.configure(
            "Eolian.Horizontal.TScrollbar",
            background="#394556",
            troughcolor=PANEL_BG,
            arrowcolor=MUTED,
            bordercolor=PANEL_BG,
            lightcolor="#394556",
            darkcolor="#1e2733",
            borderwidth=0,
            arrowsize=14,
        )
        style.configure(
            "Eolian.Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor="#202832",
            borderwidth=0,
        )

        # 应用头部（系统标题栏负责移动、最小化和关闭）
        header = tk.Frame(self.root, bg=WINDOW_BG, height=72)
        self.header = header
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(header, bg=ACCENT, width=4).pack(side="left", fill="y")
        brand = tk.Frame(header, bg=WINDOW_BG)
        brand.pack(side="left", padx=20, pady=12)
        tk.Label(
            brand,
            text="EOLIAN",
            bg=WINDOW_BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="英雄联盟换肤助手  ·  SKIN COMPANION",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")

        self.terminal_btn = tk.Button(
            header,
            text="终端",
            command=self._toggle_terminal,
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground="#263345",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
            padx=18,
            pady=7,
        )
        self.terminal_btn.pack(side="right", padx=(8, 20), pady=20)
        self.root.bind("<F12>", lambda _: self._toggle_terminal())

        # 皮肤目录刷新：从本地客户端现拉 skins.json / champion-summary.json，
        # 解决"新皮肤上线但包内快照还没有"的问题。
        self.catalog_btn = tk.Button(
            header,
            text="刷新目录",
            command=self._refresh_catalog,
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground="#263345",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
            padx=14,
            pady=7,
        )
        self.catalog_btn.pack(side="right", padx=(8, 0), pady=20)

        footer = tk.Frame(self.root, bg=PANEL_BG, height=46)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.progress = ttk.Progressbar(
            footer,
            mode="indeterminate",
            style="Eolian.Horizontal.TProgressbar",
        )
        self.progress.pack(side="top", fill="x")
        self.status_var = tk.StringVar(value="等待客户端连接")
        self.status_label = tk.Label(
            footer,
            textvariable=self.status_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=14)

        body = tk.Frame(self.root, bg=WINDOW_BG)
        body.pack(fill="both", expand=True)

        # 左侧运行状态
        sidebar = tk.Frame(
            body,
            bg=PANEL_BG,
            width=238,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        sidebar.pack(side="left", fill="y", padx=(18, 0), pady=18)
        sidebar.pack_propagate(False)

        tk.Label(
            sidebar,
            text="运行状态",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(20, 10))

        connection_row = tk.Frame(sidebar, bg=PANEL_BG)
        connection_row.pack(fill="x", padx=18, pady=4)
        self.connection_dot = tk.Label(
            connection_row,
            text="●",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        )
        self.connection_dot.pack(side="left")
        self.connection_var = tk.StringVar(value="等待客户端")
        tk.Label(
            connection_row,
            textvariable=self.connection_var,
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(7, 0))

        self.phase_var = tk.StringVar(value="阶段：未知")
        tk.Label(
            sidebar,
            textvariable=self.phase_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=18, pady=(2, 18))

        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=18)

        tk.Label(
            sidebar,
            text="当前英雄",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=18, pady=(20, 6))
        self.champion_var = tk.StringVar(value="未选择")
        tk.Label(
            sidebar,
            textvariable=self.champion_var,
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
            justify="left",
            wraplength=200,
        ).pack(anchor="w", padx=18)

        tk.Label(
            sidebar,
            text="当前皮肤",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=18, pady=(20, 6))
        self.skin_info_var = tk.StringVar(value="未进入选人阶段")
        tk.Label(
            sidebar,
            textvariable=self.skin_info_var,
            bg=PANEL_BG,
            fg=ACCENT,
            font=("Microsoft YaHei UI", 10),
            anchor="w",
            justify="left",
            wraplength=200,
        ).pack(anchor="w", padx=18)

        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=18, pady=22)
        self.skin_count_var = tk.StringVar(value="0 款可用皮肤")
        tk.Label(
            sidebar,
            textvariable=self.skin_count_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=18)
        tk.Label(
            sidebar,
            text="EOLIAN 1.0",
            bg=PANEL_BG,
            fg="#596575",
            font=("Segoe UI", 7),
        ).pack(side="bottom", anchor="w", padx=18, pady=16)

        # 主内容
        content = tk.Frame(body, bg=WINDOW_BG)
        content.pack(side="left", fill="both", expand=True, padx=18, pady=18)

        toolbar = tk.Frame(content, bg=WINDOW_BG)
        toolbar.pack(fill="x", pady=(0, 10))
        tk.Label(
            toolbar,
            text="皮肤库",
            bg=WINDOW_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        self.search_var = tk.StringVar()
        search = tk.Entry(
            toolbar,
            textvariable=self.search_var,
            bg=PANEL_ALT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            width=26,
        )
        search.pack(side="right", ipady=7, padx=(10, 0))
        search.bind("<KeyRelease>", lambda _: self._refresh_skin_rows())
        tk.Label(
            toolbar,
            text="筛选",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right")

        content_grid = tk.Frame(content, bg=WINDOW_BG)
        content_grid.pack(fill="both", expand=True)
        content_grid.grid_columnconfigure(0, weight=1)
        content_grid.grid_rowconfigure(0, weight=1)

        list_frame = tk.Frame(
            content_grid,
            bg=PANEL_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        self.skin_tree = ttk.Treeview(
            list_frame,
            columns=("index", "name", "skin", "state"),
            show="headings",
            style="Eolian.Treeview",
            selectmode="browse",
        )
        self.skin_tree.heading("index", text="#")
        self.skin_tree.heading("name", text="皮肤")
        self.skin_tree.heading("skin", text="编号")
        self.skin_tree.heading("state", text="状态")
        self.skin_tree.column("index", width=42, minwidth=42, anchor="center", stretch=False)
        self.skin_tree.column("name", width=340, minwidth=220)
        self.skin_tree.column("skin", width=74, minwidth=64, anchor="center", stretch=False)
        self.skin_tree.column("state", width=76, minwidth=64, anchor="center", stretch=False)
        self.skin_tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        scroll = SlimScrollbar(
            list_frame,
            command=self.skin_tree.yview,
            trough_color=PANEL_BG,
            thumb_color="#3a4658",
        )
        scroll.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self.skin_tree.configure(yscrollcommand=scroll.set)
        self.skin_tree.bind("<<TreeviewSelect>>", lambda _: self._update_selection_detail())
        self.skin_tree.bind("<Double-Button-1>", lambda _: self._do_swap())
        self.skin_tree.tag_configure("base", foreground=MUTED)
        self.skin_tree.tag_configure("current", foreground=ACCENT)

        detail = tk.Frame(
            content_grid,
            bg=PANEL_BG,
            width=280,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        detail.grid(row=0, column=1, sticky="ns")
        detail.grid_propagate(False)
        tk.Label(
            detail,
            text="已选皮肤",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w", padx=20, pady=(20, 10))
        self.selected_name_var = tk.StringVar(value="未选择")
        tk.Label(
            detail,
            textvariable=self.selected_name_var,
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
            justify="left",
            wraplength=240,
        ).pack(fill="x", padx=20)
        self.selected_meta_var = tk.StringVar(value="从左侧列表选择一个皮肤")
        tk.Label(
            detail,
            textvariable=self.selected_meta_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            justify="left",
            wraplength=240,
        ).pack(fill="x", padx=20, pady=(6, 22))

        tk.Frame(detail, bg=BORDER, height=1).pack(fill="x", padx=20)
        self.build_state_var = tk.StringVar(value="构建：等待操作")
        self.inject_state_var = tk.StringVar(value="注入：未启动")
        for variable in (self.build_state_var, self.inject_state_var):
            tk.Label(
                detail,
                textvariable=variable,
                bg=PANEL_BG,
                fg=MUTED,
                font=("Microsoft YaHei UI", 8),
                anchor="w",
            ).pack(fill="x", padx=20, pady=(14, 0))

        # 本地覆盖能否生效：只有"当前皮肤 = 基础皮肤"时 overlay 才会被游戏加载
        self.overlay_mode_var = tk.StringVar(value="本地覆盖：等待进入选人")
        self.overlay_mode_label = tk.Label(
            detail,
            textvariable=self.overlay_mode_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.overlay_mode_label.pack(fill="x", padx=20, pady=(6, 0))

        self.swap_btn = tk.Button(
            detail,
            text="开始换肤",
            command=self._do_swap,
            bg=ACCENT,
            fg="#11151a",
            activebackground="#e2bd72",
            activeforeground="#11151a",
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 10, "bold"),
            state="disabled",
            cursor="hand2",
            pady=10,
        )
        self.swap_btn.pack(side="bottom", fill="x", padx=20, pady=(0, 20))

        # 构建完成后如果还想换别的皮肤：先取消这次构建，再重新点【开始换肤】
        self.cancel_btn = tk.Button(
            detail,
            text="取消本次构建",
            command=self._cancel_build,
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground="#263345",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
            state="disabled",
            cursor="hand2",
            pady=7,
        )
        self.cancel_btn.pack(side="bottom", fill="x", padx=20, pady=(0, 8))

    def _apply_native_window_style(self, window: tk.Misc) -> None:
        """启用 Windows 11 深色原生标题栏，保留系统最小化行为。"""
        try:
            import ctypes

            window.update_idletasks()
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                result = dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
                if result == 0:
                    break

            def colorref(value: str) -> int:
                red, green, blue = (
                    int(value[index:index + 2], 16) for index in (1, 3, 5)
                )
                return red | (green << 8) | (blue << 16)

            for attribute, value in (
                (35, WINDOW_BG),
                (36, TEXT),
                (34, BORDER),
            ):
                color = ctypes.c_int(colorref(value))
                dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(color),
                    ctypes.sizeof(color),
                )
        except (AttributeError, OSError, tk.TclError):
            pass

    def _place_default(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(20, (screen_w - WINDOW_W) // 2)
        y = max(20, (screen_h - WINDOW_H) // 3)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    # ---- 状态更新（后台线程只投递队列，主线程消费） ----

    def on_state(self, state: ChampSelectState) -> None:
        self._ui_queue.put(("state", state))

    def _set_status(self, text: str, color: str | None = None) -> None:
        self._ui_queue.put(("status", text, color))
        self._ui_queue.put(("log", f"[floater] {text}"))
        print(f"[floater] {text}", flush=True)  # 控制台同步打印，便于诊断

    def _drain_ui_queue(self) -> None:
        """主线程消费 UI 队列（由 root.after 周期调度）。

        单条消息处理失败不能中断队列消费（否则 after 链断裂，界面永久冻结）。
        """
        while True:
            try:
                item = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            try:
                if kind == "state":
                    st = item[1]
                    print(f"[floater][消费] state phase={st.phase} in_cs={st.in_champ_select} "
                          f"champion={st.champion_id} skin={st.selected_skin_id}", flush=True)
                    self._apply_state(st)
                elif kind == "status":
                    self._apply_status(item[1], item[2])
                elif kind == "panel":
                    if item[1] is not None:
                        self.build_state_var.set(item[1])
                    if item[2] is not None:
                        self.inject_state_var.set(item[2])
                elif kind == "connection":
                    connected = bool(item[1])
                    self.connection_dot.configure(fg=OK if connected else MUTED)
                    self.connection_var.set("客户端已连接" if connected else "等待客户端")
                    self._apply_status(
                        "客户端已连接，等待进入英雄选择" if connected else "等待客户端连接",
                        OK if connected else MUTED,
                    )
                elif kind == "log":
                    self._append_terminal_line(str(item[1]))
                elif kind == "catalog":
                    self._on_catalog_updated(item[1], item[2])
                elif kind == "busy":
                    self._set_busy(bool(item[1]))
                elif kind == "actions":
                    self._update_action_buttons()
            except Exception as exc:
                import traceback
                print(f"[floater][UI异常] {kind}: {exc}", flush=True)
                traceback.print_exc()
        self.root.after(100, self._drain_ui_queue)

    def _apply_status(self, text: str, color: str | None) -> None:
        self.status_var.set(text)
        self.status_label_color(color or MUTED)

    def _append_terminal_line(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        self._terminal_lines.append(line)
        text = self._terminal_text
        window = self._terminal_window
        if text is None or window is None or not window.winfo_exists():
            return
        tag = ""
        if "ERROR" in line or "失败" in line:
            tag = "error"
        elif "WARN" in line or "HINT" in line:
            tag = "warn"
        elif "构建完成" in line or "注入成功" in line or "已连接客户端" in line:
            tag = "ok"
        text.configure(state="normal")
        text.insert("end", line + "\n", tag)
        line_count = int(text.index("end-1c").split(".", 1)[0])
        if line_count > 2400:
            text.delete("1.0", f"{line_count - 2000}.0")
        text.configure(state="disabled")
        text.see("end")

    def _toggle_terminal(self) -> None:
        window = self._terminal_window
        if window is not None and window.winfo_exists():
            if window.state() == "iconic":
                window.deiconify()
            window.lift()
            window.focus_force()
            return
        self._build_terminal_window()

    def _build_terminal_window(self) -> None:
        window = tk.Toplevel(self.root)
        self._terminal_window = window
        window.title("Eolian - 运行终端")
        window.minsize(680, 360)
        window.configure(bg=WINDOW_BG)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_terminal)
        window.bind("<Escape>", lambda _: self._close_terminal())
        window.update_idletasks()
        width, height = 860, 480
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

        toolbar = tk.Frame(window, bg=PANEL_BG, height=52)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar,
            text="●",
            bg=PANEL_BG,
            fg=OK,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(16, 6))
        tk.Label(
            toolbar,
            text="实时运行日志",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        tk.Button(
            toolbar,
            text="打开日志目录",
            command=self._open_log_folder,
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground="#263345",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
            padx=12,
            pady=5,
        ).pack(side="right", padx=(8, 14), pady=11)
        tk.Button(
            toolbar,
            text="清空",
            command=self._clear_terminal,
            bg=PANEL_BG,
            fg=MUTED,
            activebackground=PANEL_ALT,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
            padx=12,
            pady=5,
        ).pack(side="right", pady=11)

        text_frame = tk.Frame(window, bg="#080b0f")
        text_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)
        self._terminal_text = tk.Text(
            text_frame,
            bg="#080b0f",
            fg="#b9c7d8",
            insertbackground=TEXT,
            selectbackground="#334155",
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            wrap="word",
            font=("Cascadia Mono", 9),
            padx=12,
            pady=10,
            state="disabled",
        )
        self._terminal_text.grid(row=0, column=0, sticky="nsew")
        vertical = SlimScrollbar(
            text_frame,
            command=self._terminal_text.yview,
            trough_color="#080b0f",
            thumb_color="#334155",
        )
        vertical.grid(row=0, column=1, sticky="ns")
        self._terminal_text.configure(yscrollcommand=vertical.set)
        self._terminal_text.tag_configure("error", foreground=DANGER)
        self._terminal_text.tag_configure("warn", foreground=ACCENT)
        self._terminal_text.tag_configure("ok", foreground=OK)

        self._terminal_text.configure(state="normal")
        if self._terminal_lines:
            self._terminal_text.insert("end", "\n".join(self._terminal_lines) + "\n")
        else:
            self._terminal_text.insert("end", "等待运行日志…\n", "warn")
        self._terminal_text.configure(state="disabled")
        self._terminal_text.see("end")
        self._apply_native_window_style(window)

    def _close_terminal(self) -> None:
        if self._terminal_window is not None:
            self._terminal_window.destroy()
        self._terminal_window = None
        self._terminal_text = None

    def _clear_terminal(self) -> None:
        self._terminal_lines.clear()
        if self._terminal_text is not None:
            self._terminal_text.configure(state="normal")
            self._terminal_text.delete("1.0", "end")
            self._terminal_text.configure(state="disabled")

    # ---- 皮肤目录刷新 ----

    def _refresh_catalog(self) -> None:
        """从本地客户端拉 skins.json / champion-summary.json（新皮肤靠这个进来）。"""
        if getattr(self, "_catalog_busy", False):
            return
        from ..catalog import refresh_from_lcu
        from ..diag import log

        self._catalog_busy = True
        if hasattr(self, "catalog_btn"):
            self.catalog_btn.configure(text="刷新中…", state="disabled")
        self._set_status("正在从客户端拉取皮肤目录…")
        log("CATALOG", "手动刷新皮肤目录")

        def worker() -> None:
            try:
                info = refresh_from_lcu()
            except Exception as exc:  # noqa: BLE001 - 客户端没开就报错回去
                log("CATALOG", "目录刷新失败", error=str(exc))
                self._ui_queue.put(("catalog", None, str(exc)))
                return
            self._ui_queue.put(("catalog", info, None))

        threading.Thread(target=worker, daemon=True, name="catalog-refresh").start()

    def _on_catalog_updated(self, info, error) -> None:
        from ..diag import log

        self._catalog_busy = False
        if hasattr(self, "catalog_btn"):
            self.catalog_btn.configure(text="刷新目录", state="normal")
        if error:
            self._set_status(f"目录刷新失败：{error}", DANGER)
            return
        source = self.catalog.reload()
        if self.champion_id > 0:
            self._set_champion()
        count = info.get("skins_count") if isinstance(info, dict) else None
        log("CATALOG", "目录已更新", skins=count, source=source)
        self._set_status(f"皮肤目录已更新（{count} 款皮肤，来源 {source}）", OK)

    def _catalog_autocheck(self) -> None:
        """启动时若缓存缺失/超过 3 天，静默刷一次（客户端没开就跳过）。"""
        from ..catalog import catalog_age_days, refresh_from_lcu
        from ..diag import log

        age = catalog_age_days()
        if age is not None and age < 3:
            return
        try:
            info = refresh_from_lcu()
        except Exception as exc:  # noqa: BLE001 - 客户端没开是常态
            log("CATALOG", "启动自动刷新跳过", reason=str(exc)[:80])
            return
        log("CATALOG", "启动自动刷新完成", skins=info.get("skins_count"), age_days=age)
        self._ui_queue.put(("catalog", info, None))

    def _open_log_folder(self) -> None:
        try:
            os.startfile(load_settings().session_dir)  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showerror("无法打开日志目录", str(exc), parent=self.root)

    def _apply_state(self, state: ChampSelectState) -> None:
        self.connection_dot.configure(fg=OK)
        self.connection_var.set("客户端已连接")
        if self.status_var.get() == "等待客户端连接":
            self._apply_status("客户端已连接，等待进入英雄选择", OK)
        self.phase_var.set(f"阶段：{state.phase or '未知'}")
        if not state.in_champ_select:
            self.champion_id = 0
            self._set_idle()
            return
        if state.champion_id <= 0:
            self.champion_id = 0
            self.champion_var.set("等待选择")
            self.skin_info_var.set("英雄尚未锁定")
            self._clear_skin_list()
            self._set_busy(False)
            self._set_status("等待选择英雄", INFO)
            return
        self.champion_id = state.champion_id
        self.current_skin_id = state.selected_skin_id
        self._set_champion()
        self._update_overlay_mode()

    def _set_idle(self) -> None:
        self.champion_var.set("未选择")
        self.skin_info_var.set("未进入选人阶段")
        self.skin_count_var.set("0 款可用皮肤")
        self._clear_skin_list()
        self.swap_btn["state"] = "disabled"
        self.selected_name_var.set("未选择")
        self.selected_meta_var.set("进入英雄选择后加载皮肤")
        self.build_state_var.set("构建：等待操作")
        self.inject_state_var.set("注入：未启动")
        self._update_overlay_mode()

    @staticmethod
    def _lcu_patch_enabled() -> bool:
        """策略开关：是否允许改客户端皮肤选择（默认关，只做本地 overlay）。"""
        try:
            from ..safe import lcu_patch_enabled

            return lcu_patch_enabled()
        except Exception:  # noqa: BLE001 - 开关读不到就按"关闭"处理（更保守）
            return False

    @staticmethod
    def _post_injection_check():
        """注入成功后的环境二次校验；拿不到结论时返回 None（不阻断）。"""
        try:
            from ..safe import SafeGuard

            return SafeGuard().post_injection_check()
        except Exception as exc:  # noqa: BLE001 - 校验本身不能打断换肤
            from ..diag import warn

            warn("SAFE", "注入后校验不可用", error=type(exc).__name__)
            return None

    def _update_overlay_mode(self) -> None:
        """讲清楚"这次换肤会不会真的显示出来"。

        overlay 只替换 skin0 的资源；客户端若选着别的皮肤（skinN），
        游戏加载的是 skinN，overlay 根本不会被读到 —— 白按一次。
        """
        if not hasattr(self, "overlay_mode_var"):
            return
        if self.champion_id <= 0:
            self.overlay_mode_var.set("本地覆盖：等待进入选人")
            return
        if self._lcu_patch_enabled():
            self.overlay_mode_var.set("本地覆盖：已开启客户端皮肤同步")
            self.overlay_mode_label.configure(fg=MUTED)
            return
        skin_num = self.current_skin_id % 1000
        if skin_num == 0:
            self.overlay_mode_var.set("本地覆盖：当前为基础皮肤，换肤可生效")
            self.overlay_mode_label.configure(fg=OK)
        else:
            self.overlay_mode_var.set(
                f"本地覆盖：当前为皮肤{skin_num}，改回基础皮肤才生效")
            self.overlay_mode_label.configure(fg=DANGER)

    def _set_champion(self) -> None:
        champion = self.catalog.champion(self.champion_id)
        if champion is None:
            self._set_idle()
            return
        skins = self.catalog.skins_of(self.champion_id)
        current = self.catalog.skin(self.current_skin_id)
        self.champion_var.set(champion.name)
        self.skin_info_var.set(
            current.name if current else f"skin{self.current_skin_id % 1000}"
        )
        self.skin_count_var.set(f"{len(skins)} 款可用皮肤")
        self._refresh_skin_rows()

    def _clear_skin_list(self) -> None:
        for item in self.skin_tree.get_children():
            self.skin_tree.delete(item)
        self._skins_by_id = {}
        self.selected_name_var.set("未选择")
        self.selected_meta_var.set("从左侧列表选择一个皮肤")
        self.swap_btn.configure(state="disabled")

    def _refresh_skin_rows(self) -> None:
        if self.champion_id <= 0:
            return
        skins = self.catalog.skins_of(self.champion_id)
        query = self.search_var.get().strip().lower()
        selected_id = self._selected_skin_id()
        for item in self.skin_tree.get_children():
            self.skin_tree.delete(item)
        self._skins_by_id = {str(skin.id): skin for skin in skins}
        visible = []
        for index, skin in enumerate(skins, start=1):
            if query and query not in skin.name.lower() and query not in str(skin.skin_num):
                continue
            if skin.id == self.current_skin_id:
                state_text = "当前"
            elif skin.is_base:
                state_text = "基础"
            else:
                state_text = ""
            tags = ("current",) if skin.id == self.current_skin_id else (
                ("base",) if skin.is_base else ()
            )
            self.skin_tree.insert(
                "",
                "end",
                iid=str(skin.id),
                values=(index, skin.name, skin.skin_num, state_text),
                tags=tags,
            )
            visible.append(skin.id)
        if not visible:
            self._update_selection_detail()
            return
        target_id = selected_id if selected_id in visible else None
        if target_id is None and self.current_skin_id in visible:
            target_id = self.current_skin_id
        if target_id is None:
            target_id = next(
                (skin_id for skin_id in visible if not self._skins_by_id[str(skin_id)].is_base),
                visible[0],
            )
        self.skin_tree.selection_set(str(target_id))
        self.skin_tree.focus(str(target_id))
        self.skin_tree.see(str(target_id))
        self._update_selection_detail()

    def _selected_skin_id(self) -> int:
        selection = self.skin_tree.selection() if hasattr(self, "skin_tree") else ()
        if not selection:
            return 0
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return 0

    def _update_selection_detail(self) -> None:
        skin_id = self._selected_skin_id()
        skin = self.catalog.skin(skin_id)
        if skin is None:
            self.selected_name_var.set("未选择")
            self.selected_meta_var.set("从左侧列表选择一个皮肤")
            self.swap_btn.configure(state="disabled")
            return
        self.selected_name_var.set(skin.name)
        kind = "基础皮肤" if skin.is_base else "可替换皮肤"
        current = " · 当前使用" if skin.id == self.current_skin_id else ""
        self.selected_meta_var.set(
            f"skin{skin.skin_num}  ·  ID {skin.id}  ·  {kind}{current}"
        )
        self.swap_btn.configure(
            state="disabled" if self._busy or skin.is_base or skin.skin_num == 0 else "normal"
        )

    # ---- 换肤 ----

    def _do_swap(self) -> None:
        if self._busy or self.champion_id <= 0:
            return
        skin = self.catalog.skin(self._selected_skin_id())
        if skin is None:
            self._set_status("请先在列表中选择皮肤", DANGER)
            return
        if skin.is_base or skin.skin_num == 0:
            self._set_status("基础皮肤无需换肤，请选择其他皮肤", DANGER)
            return
        if (not self._lcu_patch_enabled() and self.champion_id > 0
                and (self.current_skin_id % 1000) != 0):
            self._set_status(
                f"客户端当前选的是皮肤{self.current_skin_id % 1000}，本地覆盖只对基础皮肤生效："
                "请先切回基础皮肤再点换肤", DANGER)
            return
        champion: Champion | None = self.catalog.champion(self.champion_id)
        if champion is None:
            return
        # 本局已经注入过：overlay 的重定向表在注入时就固定了，
        # 再点换肤对本局无效，必须等这一局结束（下一局是新的游戏进程）。
        if self._patcher_host is not None and self._injected:
            self._set_status("本局已经注入过了，换肤要等这一局结束再点", DANGER)
            return
        self._set_busy(True)
        self._queue_panel("构建：准备中", "注入：等待")
        self._set_status(f"构建 {skin.name} …")
        threading.Thread(target=self._swap_worker, args=(champion, skin), daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.progress.start(12)
            self.swap_btn.configure(text="构建中…", state="disabled")
        else:
            self.progress.stop()
            self.swap_btn.configure(text="开始换肤")
            self._update_selection_detail()
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        """「取消本次构建」只在"补丁器在跑且本局还没注入"时可点。"""
        if not hasattr(self, "cancel_btn"):
            return
        can_cancel = self._patcher_host is not None and not self._injected
        self.cancel_btn.configure(state="normal" if can_cancel else "disabled")

    def _cancel_build(self) -> None:
        if self._patcher_host is None:
            self._set_status("当前没有待取消的构建", MUTED)
            return
        if self._injected:
            self._set_status("本局已经注入，无法取消；等这一局结束再换", DANGER)
            return
        from ..diag import log

        self._queue_panel("构建：取消中", "注入：未启动")
        self._set_status("正在取消本次构建…", MUTED)
        log("SWAP", "用户取消本次构建")

        def worker() -> None:  # 停补丁器最多等几秒，别卡住界面
            self._stop_patcher()
            self._ui_queue.put(("busy", False))
            self._ui_queue.put(("actions", None))
            self._ui_queue.put(("panel", "构建：已取消", "注入：未启动"))
            self._ui_queue.put(("status", "已取消，可以重新选择皮肤再点【开始换肤】", MUTED))

        threading.Thread(target=worker, daemon=True, name="cancel-build").start()

    def _queue_panel(self, build: str | None = None, inject: str | None = None) -> None:
        self._ui_queue.put(("panel", build, inject))

    def _stop_patcher(self) -> None:
        """停掉上一次的补丁器（换皮肤前先收旧的，避免两个 mod-tools 打架）。"""
        host = self._patcher_host
        self._patcher_host = None
        self._injected = False
        if host is None:
            return
        try:
            host.stop()
        except Exception:  # noqa: BLE001 - 旧实例停不掉也不能挡住新的一次
            pass

    def _swap_worker(self, champion: Champion, skin: Skin) -> None:
        from ..diag import error, log, warn

        try:
            # 0) 先收掉上一次的补丁器（场景：构建完还没进游戏，想换另一个皮肤）
            self._stop_patcher()
            # 1) 构建覆盖 WAD（game_index 缓存后约几秒）
            t0 = time.monotonic()
            self._set_status(f"构建 {skin.name} …")
            log("SWAP", "开始构建", champion=champion.alias, skin_num=skin.skin_num)
            result = self.swapper.build_overlay(champion.alias.lower(), skin.skin_num)
            log("SWAP", "构建完成", elapsed_s=round(time.monotonic() - t0, 1),
                overlay=str(result.overlay_root))
            self._queue_panel("构建：完成", "注入：准备")
            self._set_status(f"构建完成 {time.monotonic() - t0:.0f}s，启动补丁器…")
            # 2) 提权补丁器（UAC 弹窗首次需授权；国服游戏以管理员运行）
            #    用 ASCII 路径副本（DLL 对中文路径兼容性差）+ debug 日志（DLL 内部 tracing）
            # 2.5) 防封门禁：版本指纹 / 补丁器 EOL / 环境 / 本机痕迹 / 覆盖合规
            #      任一硬失败即放弃本次注入（fail-closed），并把原因直接显示出来。
            #      放在 ASCII 复制之前：拦得住就不必再复制上百 MB。
            self._queue_panel("构建：完成", "注入：安全检查")
            self._set_status("安全检查中：版本、环境、覆盖合规…")
            from ..safe import SafeRefusal, guard_injection, mark_guarded

            try:
                gate = guard_injection(result.overlay_root)
            except SafeRefusal as exc:
                blocked = exc.report.blocking if exc.report else []
                for finding in blocked:
                    detail = " ".join(f"{k}={v}" for k, v in finding.detail.items())
                    error("SAFE", f"{finding.code} {finding.message}", detail=detail[:200])
                reason = exc.report.short_reason() if exc.report else str(exc)
                self._queue_panel("构建：完成", "注入：已拦截")
                self._set_status(f"安全检查未通过：{reason}", DANGER)
                log("SAFE", "门禁拦截，本次不注入", blocks=len(blocked), reason=reason)
                return
            if gate is not None and gate.warnings:
                for finding in gate.warnings:
                    warn("SAFE", f"{finding.code} {finding.message}")
                log("SAFE", "门禁放行（有告警）", warns=len(gate.warnings))
                self._set_status(f"安全检查通过（{len(gate.warnings)} 条告警，详见日志）")
            else:
                log("SAFE", "门禁通过")
                self._set_status("安全检查通过")
            overlay_ascii = self.swapper.copy_to_ascii(result.overlay_root)
            mark_guarded(overlay_ascii)  # 与源目录字节一致，不重复体检
            from ..patcher import create_patcher
            # debug 日志会把游戏打开的每个文件（含 ACE 扫描路径）写进 diag.log，
            # 默认关；排查问题再设 EOLIAN_DEBUG_PATCHER=1。
            debug_patcher = os.environ.get("EOLIAN_DEBUG_PATCHER", "").strip() == "1"
            host = create_patcher(overlay_ascii, debug=debug_patcher)
            host.start()
            self._patcher_host = host
            self._injected = False
            log("SWAP", "补丁器已启动，等待游戏进程", pid=host.proc.pid if host.proc else None,
                overlay=str(overlay_ascii))
            self._queue_panel("构建：完成", "注入：等待游戏")
            self._set_status("补丁器就位！现在可以点【开始】进入游戏；"
                             "进游戏前还能换别的皮肤")
            # 补丁器已就位，立刻放开按钮：构建完成后允许再换一次皮肤
            # （再换会先停掉这个补丁器，用新 overlay 重启一个）
            # 注意：必须走 _ui_queue，不能从工作线程直接调 root.after/_set_busy
            # —— tkinter 不是线程安全的，之前就是这里失败导致按钮一直卡在"构建中…"。
            self._ui_queue.put(("busy", False))
            self._ui_queue.put(("actions", None))
            # 3) 客户端皮肤同步（默认关闭）：PATCH selectedSkinId 会同步给服务端，
            #    换成未拥有的皮肤等于发一次越权声明。所以默认只做本地 overlay。
            if self.gameflow is not None and not self._lcu_patch_enabled():
                log("SWAP", "跳过 LCU 皮肤同步（lcu_patch_skin=false，服务端状态保持原样）")
            elif self.gameflow is not None:
                try:
                    if self.gameflow.select_skin(skin.id, force=True):
                        log("SWAP", "LCU 已同步皮肤", skin_id=skin.id)
                        self._set_status(f"已选择 {skin.name}，补丁器待命")
                except Exception as exc:
                    log("SWAP", "LCU 同步失败（不影响游戏内换肤）", error=str(exc))
            # 4) 保持补丁器运行直到本局结束（dll detached 后停止——重定向表
            #    在注入时固定，跨局复用旧 overlay 会导致下局不生效）
            last_state = ""
            seen_logs = 0
            safety_stop = False
            while host.proc is not None and host.proc.poll() is None:
                state = host.last_state
                if state and state != last_state:
                    last_state = state
                    detail = host.events[-1].detail if host.events else ""
                    log("PATCHER", f"状态: {state} {detail}")
                    if state == "injected":
                        self._injected = True
                        self._ui_queue.put(("actions", None))
                        # 注入后二次校验：游戏这时才真正存在，权限/调试器/
                        # 残留模块这些项只有在此时此刻才能拿到真实值。
                        post = self._post_injection_check()
                        if post is not None and not post.passed:
                            for finding in post.blocking:
                                error("SAFE", f"{finding.code} {finding.message}")
                            self._queue_panel("构建：完成", "注入：已拦截")
                            self._set_status(
                                f"注入后校验未通过，已停止：{post.short_reason()}", DANGER)
                            log("SAFE", "注入后校验未通过，停止补丁器",
                                blocks=len(post.blocking))
                            safety_stop = True
                            break
                        if post is not None:
                            env = next((f.detail for f in post.findings if f.code == "ENV002"), {})
                            log("SAFE", "注入后校验通过",
                                pid=env.get("pid"), integrity=env.get("integrity"),
                                warns=len(post.warnings))
                        else:
                            log("SAFE", "注入后校验不可用（已跳过）")
                        self._queue_panel("构建：完成", "注入：成功")
                        self._set_status(f"注入成功！游戏内应为 {skin.name}", OK)
                    elif state == "failed":
                        self._queue_panel("构建：失败", "注入：失败")
                        self._set_status(f"注入失败: {detail}", DANGER)
                        error("PATCHER", "注入失败", detail=detail,
                              hint="游戏以管理员运行时补丁器必须 --elevate（已默认）；"
                                   "若仍失败见 session/diag.log 的 DLL 日志")
                    elif state == "exited":
                        # 本局结束：停止补丁器，下局需重新选皮肤
                        if self._patcher_host is host:
                            self._set_status("本局结束——下局请重新选择皮肤", MUTED)
                        log("SWAP", "游戏退出（dll detached），停止补丁器")
                        break
                    else:
                        self._set_status(f"补丁器: {state} {detail}")
                # 打印 DLL/HOST 日志到控制台（诊断用；logs 含 stderr + stdout dll 行）
                for line in host.logs[seen_logs:]:
                    if line.startswith("dll "):
                        log("DLL", line[4:])
                    else:
                        log("HOST", line)
                seen_logs = len(host.logs)
                time.sleep(1)
            host.stop()
            log("SWAP", "补丁器已停止")
            # 只有当这个 host 还是"当前"那个时才改界面状态；
            # 否则说明用户已经点了另一个皮肤，新的一轮接管了 UI。
            if self._patcher_host is host:
                self._patcher_host = None
                self._injected = False
                if not safety_stop:
                    self._set_status("补丁器已停止（本局结束）")
            self._ui_queue.put(("actions", None))
        except Exception as exc:
            self._queue_panel("构建：失败", "注入：失败")
            error("SWAP", "换肤流程失败", error=str(exc),
                  hint="按上方环节标记（B*/C*/DLL）定位；详见 session/diag.log")
            self._set_status(f"换肤失败: {exc}", DANGER)
        finally:
            self._ui_queue.put(("busy", False))
            self._ui_queue.put(("actions", None))

    def status_label_color(self, color: str | None) -> None:
        self.status_label.configure(fg=color or MUTED)

    # ---- 主循环 ----

    def run(self) -> None:
        stop = threading.Event()
        self._poll_stop = stop
        threading.Thread(target=self._poll_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=self._catalog_autocheck, daemon=True).start()
        self.root.after(100, self._drain_ui_queue)  # 主线程消费 UI 队列
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            remove_listener(self._terminal_listener)

    def _on_close(self) -> None:
        if self._poll_stop is not None:
            self._poll_stop.set()
        try:
            self._stop_patcher()   # 关窗口时别把 mod-tools 留成孤儿进程
        except Exception:  # noqa: BLE001
            pass
        remove_listener(self._terminal_listener)
        self.root.destroy()

    def _poll_loop(self, stop: threading.Event) -> None:
        """持续轮询：未连接客户端时自动重连（不依赖启动时状态）。"""
        import time as _time
        from ..settings import load_settings

        log_path = load_settings().session_dir / "floater.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8")
        except OSError:
            log_file = None

        def log_line(msg: str) -> None:
            ts = _time.strftime("%H:%M:%S")
            line = f"[{ts}][floater] {msg}"
            print(line, flush=True)
            self._ui_queue.put(("log", line))
            if log_file:
                log_file.write(line + "\n")
                log_file.flush()

        gameflow = self.gameflow
        last_key = None
        while not stop.is_set():
            if gameflow is None:
                log_line("未连接客户端，重试中…")
                try:
                    from ..lcu import discover
                    lockfile = discover(default_client_root())
                    gameflow = Gameflow(RiotClient(lockfile))
                    self.gameflow = gameflow
                    self._ui_queue.put(("connection", True))
                    log_line("已连接客户端")
                except Exception:
                    stop.wait(3)
                    continue
            try:
                state = gameflow.champ_select_state()
                key = (state.phase, state.champion_id, state.selected_skin_id)
                if key != last_key:
                    last_key = key
                    log_line(f"状态变化: phase={state.phase} in_cs={state.in_champ_select} "
                             f"champion={state.champion_id} skin={state.selected_skin_id}")
                    self.on_state(state)
            except Exception:
                gameflow = None
                self.gameflow = None
                self._ui_queue.put(("connection", False))
                continue
            stop.wait(0.5)


def prepare_settings() -> Settings | None:
    """首次运行自动探测；探测失败时只让用户选择目录。"""
    settings = load_settings()
    changed = not DEFAULT_CONFIG_PATH.is_file() or bool(getattr(sys, "frozen", False))
    dialog_root: tk.Tk | None = None

    if not settings.valid_client:
        dialog_root = dialog_root or tk.Tk()
        dialog_root.withdraw()
        selected = filedialog.askdirectory(
            parent=dialog_root,
            title="选择英雄联盟客户端目录（包含 LeagueClient / Game）",
            initialdir=str(settings.client_root.parent),
            mustexist=True,
        )
        if not selected:
            dialog_root.destroy()
            return None
        client_root = Path(selected)
        if (client_root / "Game" / "DATA" / "FINAL").is_dir():
            settings = replace(settings, client_root=client_root)
        elif (client_root.parent / "Game" / "DATA" / "FINAL").is_dir():
            client_root = client_root.parent
            settings = replace(settings, client_root=client_root)
        else:
            settings = replace(settings, client_root=client_root)
        settings = replace(settings, game_dir=detect_game_dir(settings.client_root))
        changed = True

    if not settings.valid_game:
        dialog_root = dialog_root or tk.Tk()
        dialog_root.withdraw()
        selected = filedialog.askdirectory(
            parent=dialog_root,
            title="选择英雄联盟 Game 目录（包含 DATA/FINAL）",
            initialdir=str(settings.client_root / "Game"),
            mustexist=True,
        )
        if not selected:
            dialog_root.destroy()
            return None
        game_dir = Path(selected)
        if (game_dir / "Game" / "DATA" / "FINAL").is_dir():
            game_dir = game_dir / "Game"
        settings = replace(settings, game_dir=game_dir)
        changed = True

    if dialog_root is not None:
        dialog_root.destroy()
    if not settings.valid_game:
        messagebox.showerror("路径无效", f"游戏目录无效:\n{settings.game_dir}")
        return None
    if not settings.valid_builder:
        messagebox.showerror(
            "缺少构建器",
            f"找不到 eolian-overlay.exe:\n{settings.learn_overlay}",
        )
        return None
    if changed:
        try:
            save_settings(settings)
        except OSError:
            pass
    return settings


def _startup_housekeeping(settings) -> None:
    """启动清理：脱敏上一份日志 + 清理 onefile 残留解包目录。

    必须在任何 ``log()`` 之前跑：diag 一旦打开 diag.log，Windows 上就换不掉它。
    """
    try:
        from ..diag import log, warn
        from ..safe import redact_previous_logs
        from ..safe.footprint import clean_stale_mei

        report = redact_previous_logs(settings.session_dir)
        for finding in report.findings:
            emit = log if finding.level in ("ok", "info") else warn
            emit("SAFE", f"{finding.code} {finding.message}",
                 **{k: str(v)[:120] for k, v in finding.detail.items()})
        removed = clean_stale_mei()
        if removed:
            log("SAFE", "已清理 onefile 残留解包目录", removed=removed)
    except Exception as exc:  # noqa: BLE001 - 启动清理不能挡住 UI
        try:
            from ..diag import warn

            warn("SAFE", "启动清理失败", error=type(exc).__name__)
        except Exception:
            pass


def main() -> int:
    import sys
    from pathlib import Path

    from ..catalog import SkinCatalog
    from ..overlay import SkinSwapper

    settings = prepare_settings()
    if settings is None:
        return 2
    _startup_housekeeping(settings)
    catalog = SkinCatalog()
    swapper = SkinSwapper(
        game_dir=settings.game_dir,
        session_dir=settings.session_dir,
        learn_overlay=settings.learn_overlay,
    )
    gameflow = None
    try:
        from ..lcu import discover
        lockfile = discover(settings.client_root)
        gameflow = Gameflow(RiotClient(lockfile))
    except Exception:
        pass
    SkinFloater(catalog, swapper, gameflow).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
