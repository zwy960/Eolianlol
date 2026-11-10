"""覆盖目录替换与 ASCII 副本测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.overlay import SkinSwapper
from eolian.diag import close_log


class TestOverlayFiles(unittest.TestCase):
    def tearDown(self):
        close_log()

    def test_promote_dir_replaces_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "new"
            destination = root / "active"
            source.mkdir()
            destination.mkdir()
            (source / "value.txt").write_text("new", encoding="utf-8")
            (destination / "value.txt").write_text("old", encoding="utf-8")
            SkinSwapper._promote_dir(source, destination)
            self.assertEqual(
                (destination / "value.txt").read_text(encoding="utf-8"), "new"
            )
            self.assertFalse(source.exists())

    def test_copy_to_ascii_copies_only_valid_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = root / "client"
            game = root / "game"
            session = root / "session"
            ascii_dir = root / "ascii-overlay"
            (client / "LeagueClient").mkdir(parents=True)
            (game / "DATA" / "FINAL").mkdir(parents=True)
            source = session / "overlays" / "aatrox-skin33"
            wad = source / "DATA" / "FINAL" / "Champions" / "Aatrox.wad.client"
            wad.parent.mkdir(parents=True)
            wad.write_bytes(b"wad")
            with patch.dict(
                os.environ,
                {
                    "EOLIAN_CLIENT_DIR": str(client),
                    "EOLIAN_GAME_DIR": str(game),
                    "EOLIAN_SESSION_DIR": str(session),
                    "EOLIAN_OVERLAY_ASCII_DIR": str(ascii_dir),
                },
                clear=False,
            ):
                swapper = SkinSwapper()
                result = swapper.copy_to_ascii(source)
            close_log()
            self.assertEqual(result, ascii_dir.resolve())
            self.assertTrue(
                (ascii_dir / "DATA" / "FINAL" / "Champions" / "Aatrox.wad.client").is_file()
            )

    def test_copy_to_ascii_rejects_empty_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = root / "client"
            game = root / "game"
            (client / "LeagueClient").mkdir(parents=True)
            (game / "DATA" / "FINAL").mkdir(parents=True)
            empty = root / "empty"
            empty.mkdir()
            with patch.dict(
                os.environ,
                {
                    "EOLIAN_CLIENT_DIR": str(client),
                    "EOLIAN_GAME_DIR": str(game),
                    "EOLIAN_OVERLAY_ASCII_DIR": str(root / "ascii"),
                },
                clear=False,
            ):
                swapper = SkinSwapper()
                with self.assertRaises(FileNotFoundError):
                    swapper.copy_to_ascii(empty)


class _OverlayFixture(unittest.TestCase):
    """搭一套最小环境：client/game/session + 一个只有 WAD 的覆盖目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.client = self.root / "client"
        self.game = self.root / "game"
        self.session = self.root / "session"
        self.ascii_dir = self.root / "ascii-overlay"
        (self.client / "LeagueClient").mkdir(parents=True)
        (self.game / "DATA" / "FINAL").mkdir(parents=True)
        self.source = self.session / "overlays" / "aatrox-skin33"
        self.wad = self.source / "DATA" / "FINAL" / "Champions" / "Aatrox.wad.client"
        self.wad.parent.mkdir(parents=True)
        self.wad.write_bytes(b"wad")

    def tearDown(self):
        close_log()
        self._tmp.cleanup()

    def copy(self) -> Path:
        env = {
            "EOLIAN_CLIENT_DIR": str(self.client),
            "EOLIAN_GAME_DIR": str(self.game),
            "EOLIAN_SESSION_DIR": str(self.session),
            "EOLIAN_OVERLAY_ASCII_DIR": str(self.ascii_dir),
        }
        with patch.dict(os.environ, env, clear=False):
            return SkinSwapper().copy_to_ascii(self.source)


class TestAsciiSync(_OverlayFixture):
    def test_sync_places_wad_and_removes_stale_sibling(self):
        stale = self.ascii_dir / "DATA" / "FINAL" / "Champions" / "Ahri.wad.client"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old-skin-leftover")

        result = self.copy()

        self.assertEqual(result, self.ascii_dir.resolve())
        self.assertTrue((self.ascii_dir / self.wad.relative_to(self.source)).is_file())
        # 上一套皮肤的 WAD 留着会让旧皮肤继续被重定向，必须清掉
        self.assertFalse(stale.exists())

    def test_no_temp_files_left_behind(self):
        self.copy()
        leftovers = [p.name for p in self.ascii_dir.rglob("*") if "eolian-tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_second_copy_skips_when_up_to_date(self):
        self.copy()
        with patch.object(SkinSwapper, "_sync_tree",
                          side_effect=AssertionError("不该再同步")):
            result = self.copy()
        self.assertEqual(result, self.ascii_dir.resolve())

    def test_falls_back_to_fresh_dir_when_destination_busy(self):
        real = SkinSwapper._sync_tree
        touched: list[Path] = []

        def fake(source, destination, rels):
            destination = Path(destination)
            touched.append(destination)
            if destination == self.ascii_dir.resolve():
                raise PermissionError(13, "另一个程序正在使用此文件", None, 32)
            return real(source, destination, rels)

        with patch.object(SkinSwapper, "_sync_tree", staticmethod(fake)):
            result = self.copy()

        self.assertNotEqual(result, self.ascii_dir.resolve())
        self.assertTrue(result.name.startswith("ascii-overlay-"), result.name)
        self.assertTrue((result / self.wad.relative_to(self.source)).is_file())

    def test_fallback_failure_propagates(self):
        older = self.root / "ascii-overlay-1000-4242"
        (older / "DATA").mkdir(parents=True)
        (older / "DATA" / "stale.wad.client").write_bytes(b"x")

        def fake(source, destination, rels):
            raise PermissionError(13, "另一个程序正在使用此文件", None, 32)

        with patch.object(SkinSwapper, "_sync_tree", staticmethod(fake)):
            # 固定目录被占 → 兜底目录；但兜底目录也可能继续失败，这里只验证
            # 失败时会抛出来而不是静默
            with self.assertRaises(PermissionError):
                self.copy()
        self.assertTrue(older.exists())  # 失败路径不动历史目录

    def test_successful_sync_prunes_old_fresh_dirs(self):
        stale_fresh = self.root / "ascii-overlay-1700000000000-123"
        (stale_fresh / "DATA").mkdir(parents=True)
        (stale_fresh / "DATA" / "old.wad.client").write_bytes(b"x")

        result = self.copy()

        self.assertEqual(result, self.ascii_dir.resolve())
        self.assertFalse(stale_fresh.exists(), "兜底目录用完后应当被清掉")

    def test_legacy_staging_leftovers_are_removed(self):
        legacy = self.root / ".ascii-overlay.staging-1234"
        legacy.mkdir()
        (legacy / "junk").write_bytes(b"x")
        legacy_backup = self.root / ".ascii-overlay.backup"
        legacy_backup.mkdir()
        (legacy_backup / "junk").write_bytes(b"x")

        self.copy()

        self.assertFalse(legacy.exists())
        self.assertFalse(legacy_backup.exists())


class TestPromoteRetry(unittest.TestCase):
    def tearDown(self):
        close_log()

    def _dirs(self, tmp: str) -> tuple[Path, Path]:
        root = Path(tmp)
        source = root / "new"
        destination = root / "active"
        source.mkdir()
        destination.mkdir()
        (source / "value.txt").write_text("new", encoding="utf-8")
        (destination / "value.txt").write_text("old", encoding="utf-8")
        return source, destination

    def test_retries_through_share_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = self._dirs(tmp)
            real_rename = Path.rename
            state = {"n": 0}

            def flaky(self_path, target):
                state["n"] += 1
                if state["n"] <= 2:
                    raise PermissionError(13, "另一个程序正在使用此文件",
                                          str(self_path), 32)
                return real_rename(self_path, target)

            with patch.object(Path, "rename", flaky), \
                    patch("time.sleep", lambda _s: None):
                SkinSwapper._promote_dir(source, destination, attempts=5,
                                         base_delay=0.01)

            self.assertEqual((destination / "value.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(source.exists())

    def test_gives_up_after_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = self._dirs(tmp)

            def always_busy(self_path, target):
                raise PermissionError(13, "另一个程序正在使用此文件",
                                      str(self_path), 32)

            with patch.object(Path, "rename", always_busy), \
                    patch("time.sleep", lambda _s: None):
                with self.assertRaises(PermissionError):
                    SkinSwapper._promote_dir(source, destination, attempts=3,
                                             base_delay=0.01)

    def test_leftover_backup_does_not_fail_promote(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = self._dirs(tmp)
            real_remove = SkinSwapper._remove_path

            def remove_but_stub_backup(path):
                if str(path).endswith(".backup"):
                    raise PermissionError(13, "另一个程序正在使用此文件",
                                          str(path), 32)
                return real_remove(path)

            with patch.object(SkinSwapper, "_remove_path",
                              staticmethod(remove_but_stub_backup)):
                SkinSwapper._promote_dir(source, destination)

            self.assertEqual((destination / "value.txt").read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()