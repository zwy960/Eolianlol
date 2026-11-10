"""占用探测与退避重试（winlock）测试。"""

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian import winlock


def _busy_error(code: int = 32) -> OSError:
    return PermissionError(13, "另一个程序正在使用此文件", None, code)


class TestBusyClassification(unittest.TestCase):
    def test_share_violation_is_busy(self):
        self.assertTrue(winlock.is_busy(_busy_error(32)))

    def test_access_denied_is_busy(self):
        self.assertTrue(winlock.is_busy(_busy_error(5)))

    def test_missing_file_is_not_busy(self):
        exc = FileNotFoundError(2, "file not found", None, 2)
        self.assertFalse(winlock.is_busy(exc))

    def test_value_error_is_not_busy(self):
        self.assertFalse(winlock.is_busy(ValueError("nope")))


class TestRetry(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _busy_error()
            return "ok"

        with patch("time.sleep", lambda _s: None):
            self.assertEqual(winlock.retry_os(flaky, attempts=5, base_delay=0.01), "ok")
        self.assertEqual(calls["n"], 3)

    def test_gives_up_and_raises(self):
        calls = {"n": 0}

        def always_busy():
            calls["n"] += 1
            raise _busy_error()

        with patch("time.sleep", lambda _s: None):
            with self.assertRaises(PermissionError):
                winlock.retry_os(always_busy, attempts=3, base_delay=0.01)
        self.assertEqual(calls["n"], 3)

    def test_non_busy_error_is_not_retried(self):
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise FileNotFoundError(2, "missing", None, 2)

        with patch("time.sleep", lambda _s: None):
            with self.assertRaises(FileNotFoundError):
                winlock.retry_os(broken, attempts=4, base_delay=0.01)
        self.assertEqual(calls["n"], 1)

    def test_on_retry_callback_failure_is_swallowed(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _busy_error()
            return 1

        def boom(*_args):
            raise RuntimeError("logger down")

        with patch("time.sleep", lambda _s: None):
            self.assertEqual(winlock.retry_os(flaky, attempts=3, base_delay=0.01,
                                              on_retry=boom), 1)


class TestProbe(unittest.TestCase):
    def test_missing_path_is_not_busy(self):
        snapshot = winlock.probe(Path(tempfile.gettempdir()) / "definitely-not-there-xyz")
        self.assertFalse(snapshot.busy)

    def test_free_directory_is_not_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "overlay"
            target.mkdir()
            (target / "a.wad.client").write_bytes(b"x")
            snapshot = winlock.probe(target)
        self.assertFalse(snapshot.busy)
        self.assertIn("未被占用", snapshot.detail)

    @unittest.skipUnless(os.name == "nt", "Windows 目录句柄语义")
    def test_directory_handle_without_share_delete_is_busy(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p]
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "held"
            target.mkdir()
            # 只共享 READ|WRITE（不含 DELETE），与资源管理器/扫描器行为一致
            handle = create(str(target), 0x80000000, 0x3, None, 3, 0x02000000, None)
            self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
            try:
                snapshot = winlock.probe(target)
            finally:
                close(handle)
            self.assertTrue(snapshot.busy)
            self.assertFalse(snapshot.files_busy)
            self.assertEqual(snapshot.winerror, 32)

    @unittest.skipUnless(os.name == "nt", "Windows 文件句柄语义")
    def test_exclusive_file_inside_free_directory_is_busy(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p]
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "overlay"
            wad = target / "DATA" / "FINAL" / "Champions" / "Aatrox.wad.client"
            wad.parent.mkdir(parents=True)
            wad.write_bytes(b"wad")
            # 扫描器式独占：只要读权限，不共享 DELETE
            handle = create(str(wad), 0x80000000, 0x1, None, 3, 0, None)
            self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
            try:
                snapshot = winlock.probe(target)
            finally:
                close(handle)
            self.assertTrue(snapshot.busy)
            self.assertTrue(snapshot.files_busy)
            self.assertEqual(len(snapshot.blocked_files), 1)


if __name__ == "__main__":
    unittest.main()
