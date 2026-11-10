"""modgen 的轻量结构测试。"""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.modgen import read_bin_dependencies


class TestReadBinDependencies(unittest.TestCase):
    def test_reads_prop_dependency_table(self):
        dependencies = [
            "DATA/Characters/Aatrox/Aatrox.bin",
            "DATA/Characters/Aatrox/Aatrox_Multi_Skins_Skin2.bin",
        ]
        data = bytearray(b"PROP")
        data += struct.pack("<I", 3)
        data += struct.pack("<I", len(dependencies))
        for dependency in dependencies:
            encoded = dependency.encode("utf-8")
            data += struct.pack("<H", len(encoded))
            data += encoded
        data += struct.pack("<I", 0)
        self.assertEqual(read_bin_dependencies(bytes(data)), dependencies)


if __name__ == "__main__":
    unittest.main()
