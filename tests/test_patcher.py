"""patcher.py 驱动测试（对真实补丁器二进制做黑盒验证）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.patcher import (
    CSLOL_DLL,
    CSLOL_MOD_TOOLS,
    CslolPatcher,
    PATCHER_HOST,
    PatcherHost,
)


@unittest.skipUnless(PATCHER_HOST.is_file(), "需要 vendor 补丁器二进制")
class TestPatcherHost(unittest.TestCase):
    def test_runoverlay_scan_then_stop(self):
        """无游戏时停留在 scanning，stdin EOF 后干净退出。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "DATA" / "FINAL" / "Champions").mkdir(parents=True)
            (overlay / "DATA" / "FINAL" / "Champions" / "fake.wad.client").write_bytes(b"test")
            with PatcherHost(overlay) as host:
                event = host.wait_for_state("injecting", timeout=15)
                self.assertEqual(event.detail, "scanning for game")
                # 主动关闭 stdin → 干净退出
                host.stop()
                self.assertIsNone(host.proc)  # stop 后置空
            self.assertIn("injecting", [e.state for e in host.events])

    def test_missing_overlay_raises(self):
        from eolian.patcher import PatcherError

        with self.assertRaises(PatcherError):
            PatcherHost(Path("nonexistent-overlay-dir")).start()


@unittest.skipUnless(
    CSLOL_MOD_TOOLS.is_file() and CSLOL_DLL.is_file(),
    "需要 vendor/cslol 补丁器二进制",
)
class TestCslolPatcher(unittest.TestCase):
    def test_scan_then_stop(self):
        import ctypes
        import tempfile

        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("cslol 测试需要管理员权限")
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "DATA" / "FINAL" / "Champions").mkdir(parents=True)
            (overlay / "DATA" / "FINAL" / "Champions" / "fake.wad.client").write_bytes(b"test")
            with CslolPatcher(overlay) as patcher:
                event = patcher.wait_for_state("injecting", timeout=15)
                self.assertIn("Waiting for league match", event.detail)
                patcher.stop()
                self.assertIsNone(patcher.proc)


if __name__ == "__main__":
    unittest.main()
