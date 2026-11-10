"""Windows 子进程窗口隐藏参数测试。"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.winproc import hidden_process_kwargs


@unittest.skipUnless(os.name == "nt", "仅 Windows 需要")
class TestHiddenProcess(unittest.TestCase):
    def test_uses_no_window_flags(self):
        kwargs = hidden_process_kwargs()
        self.assertTrue(
            kwargs["creationflags"] & getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        self.assertEqual(
            kwargs["startupinfo"].wShowWindow,
            getattr(subprocess, "SW_HIDE", 0),
        )


if __name__ == "__main__":
    unittest.main()
