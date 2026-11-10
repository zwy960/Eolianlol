"""皮肤目录刷新/缓存测试（不联网、不依赖游戏客户端）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.catalog import (
    CatalogError,
    SkinCatalog,
    catalog_age_days,
    resolve_catalog_paths,
    write_catalog,
)

SKINS = {
    "1000": {"name": "Base", "isBase": True},
    "1001": {"name": "Skin 1", "isBase": False},
    "92045": {"name": "至臻 玉剑传说 武剑仙 (2022)", "isBase": False},
}
CHAMPIONS = [
    {"id": 1, "name": "Annie", "alias": "Annie"},
    {"id": 92, "name": "Riven", "alias": "Riven"},
]


class TestWriteCatalog(unittest.TestCase):
    def test_writes_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = write_catalog(SKINS, CHAMPIONS, tmp)
            self.assertEqual(info["skins_count"], 3)
            self.assertTrue((Path(tmp) / "skins.json").is_file())
            self.assertTrue((Path(tmp) / "champion-summary.json").is_file())
            self.assertEqual(json.loads((Path(tmp) / "skins.json").read_text("utf-8")), SKINS)

    def test_invalid_payload_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError):
                write_catalog(["not", "a", "dict"], CHAMPIONS, tmp)
            with self.assertRaises(CatalogError):
                write_catalog(SKINS, {"id": 1}, tmp)
            # 校验失败时不应留下半成品
            self.assertFalse((Path(tmp) / "skins.json").exists())


class TestResolve(unittest.TestCase):
    def test_cache_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_catalog(SKINS, CHAMPIONS, tmp)
            skins, champions, source = resolve_catalog_paths(cache_dir=tmp)
            self.assertEqual(source, "cache")
            self.assertTrue(str(skins).endswith("skins.json"))

    def test_broken_cache_falls_back_to_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "skins.json").write_text("{broken", encoding="utf-8")
            (Path(tmp) / "champion-summary.json").write_text("[]", encoding="utf-8")
            _, _, source = resolve_catalog_paths(cache_dir=tmp)
            self.assertEqual(source, "bundled")

    def test_explicit_paths_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_catalog(SKINS, CHAMPIONS, tmp)
            skins = Path(tmp) / "skins.json"
            champions = Path(tmp) / "champion-summary.json"
            got_skins, got_champions, source = resolve_catalog_paths(
                skins, champions, cache_dir=None)
            self.assertEqual(source, "explicit")
            self.assertEqual(got_skins, skins)
            self.assertEqual(got_champions, champions)


class TestCatalogLoad(unittest.TestCase):
    def test_reads_cache_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_catalog(SKINS, CHAMPIONS, tmp)
            catalog = SkinCatalog(cache_dir=tmp)
            self.assertEqual(catalog.source, "cache")
            self.assertEqual(len(catalog), 3)
            self.assertIsNotNone(catalog.skin(92045))
            self.assertEqual(catalog.champion(92).alias, "Riven")

            # 删掉缓存后 reload 应回退到包内快照
            (Path(tmp) / "skins.json").unlink()
            source = catalog.reload()
            self.assertEqual(source, "bundled")
            self.assertGreater(len(catalog), 3)

    def test_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(catalog_age_days(tmp))
            write_catalog(SKINS, CHAMPIONS, tmp)
            age = catalog_age_days(tmp)
            self.assertIsNotNone(age)
            self.assertLess(age, 1.0)


if __name__ == "__main__":
    unittest.main()
