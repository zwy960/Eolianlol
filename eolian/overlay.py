"""换肤编排层：mod 生成 → 覆盖 WAD 构建 → 补丁器注入。

流程：
    SkinSwapper.swap(champion, skin_num)
      ├─ modgen.build_swap_for_skin()      # skinN.bin → skin0 覆盖 mod
      ├─ eolian-overlay build               # ltk_overlay 构建覆盖 WAD
      └─ PatcherHost.runoverlay()          # 注入游戏进程（需游戏已启动）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import winlock
from .modgen import build_swap_for_skin
from .patcher import create_patcher
from .settings import load_settings
from .winproc import hidden_process_kwargs

@dataclass
class SwapResult:
    mod_dir: Path
    overlay_root: Path
    state_dir: Path
    overlay_built: list[str]
    patcher_state: str | None = None


class SkinSwapper:
    """完整换肤流程（构建不依赖游戏运行，注入需要游戏进程）。"""

    def __init__(self, game_dir: str | Path | None = None,
                 session_dir: str | Path | None = None,
                 learn_overlay: str | Path | None = None,
                 patcher_host: str | Path | None = None):
        settings = load_settings()
        self.settings = settings
        self.game_dir = Path(game_dir) if game_dir else settings.game_dir
        self.learn_overlay = Path(learn_overlay) if learn_overlay else settings.learn_overlay
        self.session_dir = Path(session_dir) if session_dir else settings.session_dir
        self.overlay_ascii_dir = settings.overlay_ascii_dir
        self.patcher_host = Path(patcher_host) if patcher_host else None

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    @staticmethod
    def _remove_best_effort(path: Path) -> bool:
        """删目录/文件，失败只记日志不抛（收尾清理不该把成功报成失败）。"""
        if not path.exists():
            return True
        try:
            SkinSwapper._remove_path(path)
            return True
        except OSError as exc:
            from .diag import warn

            warn("B6", "清理残留失败（不影响本次结果）", path=str(path),
                 error=str(exc), hold=winlock.probe(path).summary())
            return False

    @staticmethod
    def _promote_dir(source: Path, destination: Path, *,
                     attempts: int = 6, base_delay: float = 0.2) -> None:
        """用已构建完成的临时目录替换目标目录。

        目录改名是 Windows 上最容易被外部句柄卡住的动作：资源管理器窗口、
        实时防护扫描刚落盘的 WAD、同步/索引服务都会持有一个不带
        ``FILE_SHARE_DELETE`` 的目录句柄，改名立刻抛 WinError 32。这类占用
        通常几十毫秒就消失，所以整段操作按"备份 → 改名 → 收尾"重来，遇到
        占用类错误指数退避重试；重试期间把探测到的占用情况写进日志。
        """
        from .diag import warn

        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = destination.parent / f".{destination.name}.backup"
        delay = base_delay
        last: OSError | None = None
        for attempt in range(1, attempts + 1):
            try:
                SkinSwapper._remove_best_effort(backup)
                if destination.exists():
                    try:
                        destination.rename(backup)
                    except FileExistsError:
                        # 备份名被上一轮占着：换一个唯一名字，别让收尾卡死在这里
                        backup = destination.parent / (
                            f".{destination.name}.backup-{os.getpid()}-{int(time.time() * 1000)}"
                        )
                        destination.rename(backup)
                try:
                    source.rename(destination)
                except Exception:
                    if backup.exists() and not destination.exists():
                        try:
                            backup.rename(destination)
                        except OSError:
                            pass
                    raise
                # 目标已经就位，剩下的删备份属于收尾：删不掉不能算失败
                SkinSwapper._remove_best_effort(backup)
                return
            except OSError as exc:
                last = exc
                if attempt >= attempts or not winlock.is_busy(exc):
                    raise
                warn("B6", "目录被占用，退避重试", target=str(destination),
                     attempt=f"{attempt}/{attempts}", wait_s=round(delay, 2),
                     error=str(exc), hold=winlock.probe(destination).summary())
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
        if last is not None:
            raise last

    # ---- 构建（无需游戏运行） ----

    def build_overlay(self, champion: str, skin_num: int,
                      tag: str | None = None) -> SwapResult:
        """生成 mod 并构建覆盖 WAD。champion 为小写别名（'ahri'）。"""
        from .diag import error, log

        champion = champion.strip().lower()
        if not champion or not champion.replace("_", "").isalnum():
            raise ValueError(f"非法英雄别名: {champion!r}")
        if skin_num <= 0:
            raise ValueError("皮肤编号必须大于 0")
        if not self.game_dir.is_dir() or not (self.game_dir / "DATA" / "FINAL").is_dir():
            raise FileNotFoundError(
                f"游戏目录无效: {self.game_dir}（需要包含 DATA/FINAL）"
            )
        if not self.learn_overlay.is_file():
            error("B0", "eolian-overlay 未构建", path=str(self.learn_overlay),
                  hint="在 eolian-overlay 目录执行 cargo build --release")
            raise FileNotFoundError(
                f"eolian-overlay 未构建: {self.learn_overlay}"
            )

        tag = tag or f"{champion}-skin{skin_num}"
        state_dir = self.session_dir / "state" / tag
        mod_dir = self.session_dir / "mods" / tag
        overlay_root = self.session_dir / "overlays" / tag
        build_id = f".{tag}-{os.getpid()}-{int(time.time() * 1000)}"
        work_mod = self.session_dir / "mods" / build_id
        work_overlay = self.session_dir / "overlays" / build_id
        state_dir.mkdir(parents=True, exist_ok=True)

        log("B1", "生成 mod（提取 skinN.bin → bin-alias 对象改名 → skin0 覆盖）",
            champion=champion, skin_num=skin_num)
        try:
            work_mod = build_swap_for_skin(
                self.game_dir, champion, skin_num, work_mod,
                learn_overlay=self.learn_overlay,
            )
            log("B3", "调用 eolian-overlay 构建覆盖 WAD",
                game_dir=str(self.game_dir), overlay_root=str(work_overlay))
            try:
                proc = subprocess.run(
                    [str(self.learn_overlay), "build",
                     "--game-dir", str(self.game_dir),
                     "--mod-dir", str(work_mod),
                     "--overlay-root", str(work_overlay),
                     "--state-dir", str(state_dir)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=600,
                    **hidden_process_kwargs(),
                )
            except subprocess.TimeoutExpired:
                error("B4", "eolian-overlay 构建超时（>600s）",
                      hint="game_index 首次构建较慢，重试通常秒级完成（缓存已落盘）")
                raise
            if proc.returncode != 0:
                error("B4", "eolian-overlay 构建失败", returncode=proc.returncode)
                for ln in proc.stdout.splitlines():
                    error("B4", f"  stdout: {ln}")
                for ln in proc.stderr.splitlines():
                    error("B4", f"  stderr: {ln}")
                raise RuntimeError(
                    "overlay 构建失败（详见上方日志，或检查 mod.config.json 格式）"
                )
            self._promote_dir(work_overlay, overlay_root)
            self._promote_dir(work_mod, mod_dir)
        except Exception:
            for path in (work_mod, work_overlay):
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
            raise

        log("B2", "mod 生成完成", mod_dir=str(mod_dir))
        built = [
            ln.replace(str(work_overlay), str(overlay_root))
            for ln in proc.stdout.splitlines()
            if ln.startswith(("built:", "reused:"))
        ]
        log("B5", "构建成功", wads=len(built) or len(proc.stdout.splitlines()))
        return SwapResult(mod_dir=mod_dir, overlay_root=overlay_root,
                          state_dir=state_dir, overlay_built=built)

    # ---- 注入（需要游戏进程运行） ----

    def apply_overlay(self, overlay_root: str | Path, timeout: float = 60.0) -> str:
        """启动补丁器注入覆盖 WAD，等待 injected 状态。"""
        if self.patcher_host is not None:
            from .patcher import PatcherHost
            with PatcherHost(overlay_root, patcher_host=self.patcher_host) as host:
                return host.wait_for_state("injected", timeout=timeout).detail
        with create_patcher(overlay_root) as host:
            event = host.wait_for_state("injected", timeout=timeout)
            return event.detail

    # ---- ASCII 副本（逐文件同步，不整体改名目录） ----

    @staticmethod
    def _cleanup_legacy_staging(base: Path) -> None:
        """清掉旧版本留下的 ``.<name>.staging-<pid>`` / ``.<name>.backup``。

        旧实现是"复制到临时目录再改目录名"，卡在 WinError 32 时会留下这两种
        残骸，占着几百 MB 空间。现在只做 best-effort 清理。
        """
        try:
            siblings = list(base.parent.iterdir())
        except OSError:
            return
        for item in siblings:
            name = item.name
            if name.startswith(f".{base.name}.staging-") or name == f".{base.name}.backup":
                SkinSwapper._remove_best_effort(item)

    @staticmethod
    def _ascii_up_to_date(source: Path, destination: Path,
                          relative_wads: list[Path]) -> bool:
        if not destination.is_dir():
            return False
        wanted = set(relative_wads)
        for rel in wanted:
            try:
                if (destination / rel).stat().st_mtime < (source / rel).stat().st_mtime:
                    return False
            except OSError:
                return False
        # 也不能有多余文件：上一套皮肤的 WAD 留着会让旧皮肤继续被重定向
        try:
            extras = [p for p in destination.rglob("*")
                      if p.is_file() and p.relative_to(destination) not in wanted]
        except OSError:
            return False
        return not extras

    @staticmethod
    def _sync_tree(source: Path, destination: Path,
                   relative_wads: list[Path]) -> tuple[int, int]:
        """把 overlay 的 WAD 逐文件放进 destination，返回 (复制数, 清理数)。

        不用"临时目录 + 目录改名"：目录改名只要碰到任何一个不带
        ``FILE_SHARE_DELETE`` 的目录句柄就抛 WinError 32，而那时临时目录已经
        写完，前面几十秒全白干。逐文件 ``os.replace`` 只会被"同一个文件的独占
        句柄"挡住，可以安全重试，也不会因为一个文件慢就整体失败。
        """
        from .diag import warn

        destination.mkdir(parents=True, exist_ok=True)
        wanted = set(relative_wads)
        for rel in sorted({rel.parent for rel in wanted}, key=lambda p: len(p.parts)):
            (destination / rel).mkdir(parents=True, exist_ok=True)

        copied = 0
        for rel in sorted(wanted):
            src_file = source / rel
            dst_file = destination / rel
            src_stat = src_file.stat()
            try:
                dst_stat = dst_file.stat()
            except OSError:
                dst_stat = None
            if (dst_stat is not None
                    and dst_stat.st_size == src_stat.st_size
                    and dst_stat.st_mtime >= src_stat.st_mtime):
                continue
            temp = dst_file.with_name(f"{dst_file.name}.{os.getpid()}.eolian-tmp")
            shutil.copy2(src_file, temp)
            winlock.retry_os(
                lambda t=temp, d=dst_file: os.replace(t, d),
                what=f"替换覆盖 WAD {rel}",
            )
            copied += 1

        # 上一套皮肤留下的 WAD 必须清掉：补丁器是按"目录里有什么"来重定向的，
        # 残留文件会让旧皮肤继续生效。
        removed = 0
        for path in sorted(destination.rglob("*"), key=lambda p: len(p.parts),
                           reverse=True):
            if not path.is_file():
                continue
            if path.relative_to(destination) in wanted:
                continue
            try:
                winlock.retry_os(path.unlink, attempts=3, what=f"清理旧文件 {path.name}")
                removed += 1
            except OSError as exc:
                warn("COPY", "旧文件清不掉（可能被判为占用）", path=str(path),
                     error=str(exc))
        return copied, removed

    @staticmethod
    def _prune_ascii_siblings(base: Path, keep: Path) -> None:
        """删掉同前缀的历史独立副本目录（只认 ``<base>-<数字>-<数字>``）。"""
        import re

        pattern = re.compile(rf"^{re.escape(base.name)}-\d+-\d+$")
        try:
            candidates = list(base.parent.iterdir())
        except OSError:
            return
        for item in candidates:
            if item == keep or not pattern.match(item.name):
                continue
            SkinSwapper._remove_best_effort(item)

    def _fresh_copy_dir(self, source: Path, base: Path,
                        relative_wads: list[Path]) -> Path:
        stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
        fresh = base.parent / f"{base.name}-{stamp}"
        self._sync_tree(source, fresh, relative_wads)
        self._prune_ascii_siblings(base, keep=fresh)
        return fresh

    def copy_to_ascii(self, overlay_root: str | Path,
                      dst: str | Path | None = None) -> Path:
        """把覆盖目录同步到纯 ASCII 路径（MinGW 系工具/DLL 对中文路径兼容性差）。

        覆盖 WAD 可能上百 MB，仅当目标缺失或源更新时复制。同步过程不整体改名
        目录：固定目录被外部句柄占死时，退化成"另建一个独立副本目录"继续跑，
        绝不因为一个目录句柄把整次换肤卡死。
        """
        from .diag import log, warn

        src = Path(overlay_root).resolve()
        dst = Path(dst or self.overlay_ascii_dir).resolve()
        self._cleanup_legacy_staging(dst)
        relative_wads = [wad.relative_to(src) for wad in sorted(src.rglob("*.wad.client"))]
        if not relative_wads:
            raise FileNotFoundError(f"覆盖目录中没有 WAD 文件: {src}")
        if self._ascii_up_to_date(src, dst, relative_wads):
            log("COPY", "ASCII 副本已是最新，跳过复制", dst=str(dst))
            return dst
        try:
            copied, removed = self._sync_tree(src, dst, relative_wads)
            log("COPY", "已同步到 ASCII 路径", dst=str(dst),
                copied=copied, removed=removed)
            # 之前兜底用的独立副本目录现在没用了，顺手清掉（几百 MB 一份）
            self._prune_ascii_siblings(dst, keep=dst)
            return dst
        except OSError as exc:
            if not winlock.is_busy(exc):
                raise
            warn("COPY", "ASCII 目录被占用，改用独立副本目录",
                 dst=str(dst), error=str(exc),
                 hold=winlock.wait_until_free(dst, attempts=3).summary())
        fresh = self._fresh_copy_dir(src, dst, relative_wads)
        log("COPY", "已复制到独立副本目录", dst=str(fresh))
        return fresh

    def swap(self, champion: str, skin_num: int,
             inject: bool = True) -> SwapResult:
        """一键换肤：构建 + 注入（游戏须在运行）。"""
        result = self.build_overlay(champion, skin_num)
        if inject:
            result.patcher_state = self.apply_overlay(result.overlay_root)
        return result
