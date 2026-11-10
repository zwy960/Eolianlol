"""路径配置与自动探测测试。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.settings import Settings, load_settings, save_settings


class TestSettings(unittest.TestCase):
    def _make_layout(self, root: Path) -> tuple[Path, Path]:
        client = root / "client"
        game = root / "game"
        (client / "LeagueClient").mkdir(parents=True)
        (game / "DATA" / "FINAL").mkdir(parents=True)
        return client, game

    def test_environment_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, game = self._make_layout(root)
            config = root / "config.json"
            config.write_text(
                json.dumps({"client_dir": "wrong-client", "game_dir": "wrong-game"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "EOLIAN_CLIENT_DIR": str(client),
                    "EOLIAN_GAME_DIR": str(game),
                    "EOLIAN_SESSION_DIR": str(root / "runtime"),
                    "EOLIAN_LEARN_OVERLAY": str(root / "builder.exe"),
                    "EOLIAN_CONFIG": str(config),
                },
                clear=False,
            ):
                settings = load_settings()
            self.assertEqual(settings.client_root, client.resolve())
            self.assertEqual(settings.game_dir, game.resolve())
            self.assertEqual(settings.session_dir, (root / "runtime").resolve())

    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, game = self._make_layout(root)
            settings = Settings(
                client_root=client.resolve(),
                game_dir=game.resolve(),
                session_dir=(root / "session").resolve(),
                learn_overlay=(root / "builder.exe").resolve(),
                overlay_ascii_dir=(root / "ascii").resolve(),
            )
            config = root / "config.json"
            with patch.dict(os.environ, {"EOLIAN_CONFIG": str(config)}, clear=False):
                save_settings(settings)
                loaded = load_settings()
            self.assertEqual(loaded, settings)

    def test_invalid_paths_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing"
            settings = Settings(
                client_root=path,
                game_dir=path,
                session_dir=Path(tmp) / "session",
                learn_overlay=path / "builder.exe",
                overlay_ascii_dir=Path(tmp) / "ascii",
            )
            self.assertFalse(settings.valid_client)
            self.assertFalse(settings.valid_game)
            self.assertFalse(settings.valid_builder)


if __name__ == "__main__":
    unittest.main()
