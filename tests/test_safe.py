"""eolian.safe 单元测试：不依赖游戏目录、不注入、不写注册表。

覆盖：策略/报告、日志脱敏、PE 指纹、覆盖 WAD 合规校验（含合成 WAD）。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eolian.hashing import chunk_path_hash
from eolian.safe import report as report_mod
from eolian.safe import footprint, overlay_verify, peinfo, policy, redact, version
from eolian.safe import session as session_mod
from eolian.safe.report import Report, SafeRefusal
from eolian.safe.session import SafeGuard

REPO = Path(__file__).resolve().parent.parent
VENDOR_EXE = REPO / "vendor" / "cslol" / "mod-tools.exe"


# ---------------------------------------------------------------- helpers


def write_wad(path: Path, files: dict[str, bytes]) -> None:
    """写一个 None 压缩的 WAD v3.4（只够本项目读取用）。"""
    chunks = sorted((chunk_path_hash(name), data) for name, data in files.items())
    with open(path, "wb") as handle:
        handle.write(b"RW" + bytes([3, 4]))
        handle.write(b"\0" * 256)
        handle.write(struct.pack("<Q", 0))
        handle.write(struct.pack("<i", len(chunks)))
        toc_offset = handle.tell()
        handle.write(b"\0" * (32 * len(chunks)))
        data_offset = handle.tell()
        offsets = []
        for _path_hash, data in chunks:
            offsets.append(data_offset)
            handle.write(data)
            data_offset += len(data)
        handle.seek(toc_offset)
        for (path_hash, data), offset in zip(chunks, offsets):
            handle.write(struct.pack("<QIII", path_hash, offset, len(data), len(data)))
            handle.write(bytes([0]))          # type_frame: compression none
            handle.write(b"\0\0\0")           # start_frame 24bit
            handle.write(struct.pack("<Q", zlib.crc32(data)))


def prop_blob(dependencies: list[str], class_hash: int = 0xAAA) -> bytes:
    out = bytearray(b"PROP")
    out += struct.pack("<I", 3)
    out += struct.pack("<I", len(dependencies))
    for dep in dependencies:
        encoded = dep.encode("utf-8")
        out += struct.pack("<H", len(encoded)) + encoded
    out += struct.pack("<I", 1)
    out += struct.pack("<I", class_hash)
    out += struct.pack("<I", 6) + b"\0" * 6
    return bytes(out)


def build_case(tmp: Path, *, overlay_extra: bool = False,
               bad_dep: bool = False, payload: bool = False) -> tuple[Path, Path]:
    """造一个 game_dir + overlay 目录。"""
    game = tmp / "Game"
    overlay = tmp / "overlay"
    base_wad = game / "DATA" / "FINAL" / "Champions" / "Ahri.wad.client"
    over_wad = overlay / "DATA" / "FINAL" / "Champions" / "Ahri.wad.client"
    base_wad.parent.mkdir(parents=True)
    over_wad.parent.mkdir(parents=True)

    base_files = {
        "data/characters/ahri/skins/skin0.bin": prop_blob(["data/characters/ahri/skins/skin0.bin"]),
        "data/characters/ahri/skins/skin5.bin": prop_blob(["data/characters/ahri/skins/skin5.bin"]),
        "data/characters/ahri/ahri.bin": b"PROP" + b"\0" * 20,
    }
    # 补一些无关 chunk，让"改动比例"落在真实量级（1/9 而不是 1/3）
    for index in range(1, 7):
        name = f"data/characters/ahri/skins/skin{index}.bin"
        base_files[name] = prop_blob([name])
    write_wad(base_wad, base_files)

    overlay_files = dict(base_files)
    dep = "data/characters/ahri/skins/skin999.bin" if bad_dep else "data/characters/ahri/skins/skin5.bin"
    overlay_files["data/characters/ahri/skins/skin0.bin"] = prop_blob([dep])
    if overlay_extra:
        overlay_files["data/characters/ahri/extra/new.bin"] = b"new file"
    write_wad(over_wad, overlay_files)

    if payload:
        (overlay / "loader.exe").write_bytes(b"MZ" + b"\0" * 16)
    return game, overlay


# ---------------------------------------------------------------- tests


class TestReport(unittest.TestCase):
    def test_raise_if_blocking(self):
        rep = Report("t")
        rep.warn("W1", "warn only")
        rep.raise_if_blocking()  # 不应抛
        rep.block("B1", "boom")
        with self.assertRaises(SafeRefusal) as ctx:
            rep.raise_if_blocking("ctx")
        self.assertIn("ctx", str(ctx.exception))
        self.assertIs(ctx.exception.report, rep)

    def test_counts_and_format(self):
        rep = Report("t")
        rep.ok("O", "ok")
        rep.info("I", "info")
        rep.warn("W", "warn")
        self.assertEqual(rep.counts()["warn"], 1)
        self.assertTrue(rep.passed)
        self.assertIn("通过", rep.format())
        payload = rep.to_dict()
        self.assertEqual(payload["scope"], "t")
        self.assertEqual(len(payload["findings"]), 3)

    def test_short_reason(self):
        rep = Report("t")
        self.assertEqual(rep.short_reason(), "")
        rep.block("B1", "版本未登记")
        rep.block("B2", "覆盖越界")
        reason = rep.short_reason()
        self.assertIn("B1", reason)
        self.assertIn("共 2 项", reason)


class TestPolicy(unittest.TestCase):
    def test_presets(self):
        off = policy.SafePolicy.preset("off")
        self.assertFalse(off.enabled)
        strict = policy.SafePolicy.preset("strict")
        self.assertTrue(strict.enabled)
        self.assertLess(strict.max_replaced_chunk_ratio, 0.25)
        with self.assertRaises(ValueError):
            policy.SafePolicy.preset("nope")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "safe.json"
            original = policy.SafePolicy.preset("balanced", known_builds=("deadbeefdeadbeef",))
            policy.save_policy(original, target)
            loaded = policy.load_policy(target)
            self.assertEqual(loaded.known_builds, ("deadbeefdeadbeef",))
            self.assertEqual(loaded.mode, "balanced")

    def test_broken_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "safe.json"
            target.write_text("{not json", encoding="utf-8")
            loaded = policy.load_policy(target)
            self.assertEqual(loaded.mode, policy.DEFAULT_MODE)
            self.assertTrue(loaded.enabled)

    def test_strict_upgrades_warnings(self):
        rep = Report("t")
        rep.warn("W", "suspect")
        policy.SafePolicy.preset("strict").apply_to(rep, "test")
        self.assertEqual(rep.findings[0].level, "block")


class TestRedact(unittest.TestCase):
    def test_addresses_paths_and_ids(self):
        line = (r"[DLL] patching module: 0000000140000000 CRYPTO_free offset: "
                r"0000000001CABDE0 at C:\Users\Administrator\AppData\Local\Wangcai\x.log "
                r"pid=4242 account=2718874843 hash="
                + "A" * 64)
        cleaned = redact.redact_line(line)
        self.assertNotIn("1CABDE0", cleaned)
        self.assertNotIn("Administrator", cleaned)
        self.assertNotIn("2718874843", cleaned)
        self.assertNotIn("A" * 64, cleaned)
        self.assertIn("CRYPTO_free", cleaned)  # 保留可排障的语义

    def test_date_not_treated_as_account(self):
        self.assertIn("20261106", redact.redact_line("build 20261106 ok"))

    def test_path_variants(self):
        """三种写法都要吃掉：单反斜杠 / 转义双反斜杠（repr）/ 正斜杠。"""
        cases = [
            r"open C:\Users\Administrator\a.dll ok",
            r"error 'C:\\Users\\Administrator\\AppData\\x.log' failed",
            "open e:/wegameapps/x/Game/tersafe.dll ok",
        ]
        for line in cases:
            cleaned = redact.redact_line(line)
            self.assertNotIn("Administrator", cleaned, cleaned)
            self.assertNotIn("wegameapps", cleaned, cleaned)
            self.assertIn("<path>", cleaned, cleaned)

    def test_sanitize_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "diag.log"
            target.write_text("path C:\\secret\\x.dll pid=99\n", encoding="utf-8")
            result = redact.sanitize_file(target)
            self.assertTrue(result.changed)
            self.assertNotIn("secret", target.read_text(encoding="utf-8"))


@unittest.skipUnless(VENDOR_EXE.is_file(), "需要 vendor/cslol/mod-tools.exe")
class TestPE(unittest.TestCase):
    def test_imports_and_fingerprint(self):
        image = peinfo.PEImage(VENDOR_EXE)
        self.assertTrue(image.is_64)
        self.assertTrue(image.has_import("kernel32.dll"))
        self.assertIn("CreateFileA", image.imports.get("KERNEL32.dll", []) or
                      image.imports.get("kernel32.dll", []))
        self.assertEqual(len(image.fingerprint()), 16)
        self.assertEqual(image.fingerprint(), peinfo.PEImage(VENDOR_EXE).fingerprint())


class TestOverlayVerify(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.pol = policy.SafePolicy.preset("balanced")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_overlay_passes(self):
        game, overlay = build_case(self.tmp)
        rep = overlay_verify.verify(overlay, game, self.pol)
        self.assertTrue(rep.passed, rep.format(verbose=True))
        codes = {f.code for f in rep.findings}
        self.assertIn("OV009", codes)

    def test_extra_chunk_blocks(self):
        game, overlay = build_case(self.tmp, overlay_extra=True)
        rep = overlay_verify.verify(overlay, game, self.pol)
        self.assertFalse(rep.passed)
        self.assertIn("OV006", {f.code for f in rep.blocking})

    def test_payload_blocks(self):
        game, overlay = build_case(self.tmp, payload=True)
        rep = overlay_verify.verify(overlay, game, self.pol)
        self.assertIn("OV003", {f.code for f in rep.blocking})

    def test_unresolved_skin0_dependency_blocks(self):
        game, overlay = build_case(self.tmp, bad_dep=True)
        rep = overlay_verify.verify(overlay, game, self.pol)
        self.assertFalse(rep.passed)
        blocks = {f.code for f in rep.blocking}
        self.assertTrue({"OV010"} & blocks, rep.format(verbose=True))

    def test_missing_overlay_wad_blocks(self):
        game, overlay = build_case(self.tmp)
        rep = overlay_verify.verify(overlay / "nope", game, self.pol)
        self.assertIn("OV001", {f.code for f in rep.blocking})


class TestFootprint(unittest.TestCase):
    def test_proxy_dll_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "League of Legends.exe").write_bytes(b"MZ")
            (game / "version.dll").write_bytes(b"MZ")
            (game / "old.bak").write_bytes(b"x")
            sites = footprint.scan_game_dir(game)
            kinds = {site.kind for site in sites}
            self.assertIn("proxy-dll", kinds)
            self.assertIn("leftover", kinds)

    def test_clean_dry_run_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            keep = session / "overlays" / ".staging-x"
            keep.mkdir(parents=True)
            (keep / "a.bin").write_bytes(b"x")
            rep = footprint.clean(tmp, session, policy.SafePolicy.preset("balanced"),
                                  dry_run=True, include_ascii=False)
            self.assertTrue(keep.exists(), rep.format(verbose=True))
            self.assertTrue(all(f.level != "block" for f in rep.findings))

    def test_clean_also_picks_up_fresh_ascii_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ascii_dir = root / "ascii-overlay"
            fresh = root / "ascii-overlay-1794314820393-22500"
            unrelated = root / "ascii-overlay-notes"
            for path in (ascii_dir, fresh, unrelated):
                path.mkdir()
                (path / "a.wad.client").write_bytes(b"x")
            rep = footprint.clean(root / "game", root / "session",
                                  policy.SafePolicy.preset("balanced"),
                                  dry_run=False, include_ascii=True,
                                  ascii_dir=ascii_dir)
            self.assertFalse(ascii_dir.exists(), rep.format(verbose=True))
            self.assertFalse(fresh.exists(), rep.format(verbose=True))
            self.assertTrue(unrelated.exists(), "不该动名字对不上的目录")


class TestVersion(unittest.TestCase):
    def test_unknown_build_blocked(self):
        rep = version.check_build(None, policy.SafePolicy.preset("balanced"), "C:/nope")
        self.assertIn("VER001", {f.code for f in rep.blocking})

    def test_eol_missing_file_is_info(self):
        rep = version.check_patcher_eol([Path("C:/definitely/not/here.dll")],
                                        policy.SafePolicy.preset("balanced"))
        self.assertTrue(rep.passed)

    @staticmethod
    def _build(fingerprint: str, timestamp: int) -> version.BuildInfo:
        return version.BuildInfo(Path("League of Legends.exe"), fingerprint, timestamp, 0x1000, 0x1000)

    def test_newer_build_is_auto_accepted(self):
        pol = policy.SafePolicy.preset(
            "balanced", known_builds=("aaaa",), last_build_timestamp=1000)
        rep = version.check_build(self._build("bbbb", 2000), pol, "C:/game")
        self.assertTrue(rep.passed, rep.format(verbose=True))
        self.assertIn("VER008", {f.code for f in rep.findings})
        updated = version.register_build(pol, self._build("bbbb", 2000))
        self.assertIn("bbbb", updated.known_builds)
        self.assertEqual(updated.last_build_timestamp, 2000)

    def test_rewritten_build_blocks(self):
        """同一 build 时间戳没前进却变了指纹 —— 当作被改写，拒绝。"""
        pol = policy.SafePolicy.preset(
            "balanced", known_builds=("aaaa",), last_build_timestamp=2000)
        for timestamp in (2000, 1500):
            rep = version.check_build(self._build("bbbb", timestamp), pol, "C:/game")
            self.assertFalse(rep.passed, f"timestamp={timestamp} 应被拦")
            self.assertIn("VER006", {f.code for f in rep.blocking})

    def test_auto_accept_can_be_disabled(self):
        pol = policy.SafePolicy.preset(
            "balanced", known_builds=("aaaa",), last_build_timestamp=1000,
            auto_accept_newer_build=False)
        rep = version.check_build(self._build("bbbb", 2000), pol, "C:/game")
        self.assertIn("VER006", {f.code for f in rep.blocking})

    def test_first_run_auto_registers(self):
        """干净机器（白名单为空、无时间戳）不能一上来就被拦死。"""
        pol = policy.SafePolicy.preset("balanced")
        rep = version.check_build(self._build("bbbb", 2000), pol, "C:/game")
        self.assertTrue(rep.passed, rep.format(verbose=True))
        self.assertIn("VER009", {f.code for f in rep.findings})
        updated = version.register_build(pol, self._build("bbbb", 2000))
        self.assertEqual(updated.known_builds, ("bbbb",))
        self.assertEqual(updated.last_build_timestamp, 2000)

    def test_first_run_blocked_when_auto_accept_off(self):
        pol = policy.SafePolicy.preset("balanced", auto_accept_newer_build=False)
        rep = version.check_build(self._build("bbbb", 2000), pol, "C:/game")
        self.assertIn("VER004", {f.code for f in rep.blocking})


class TestLcuPatchSwitch(unittest.TestCase):
    """lcu_patch_skin 默认关闭：把"服务端可见层"整条摘掉。"""

    def test_default_off(self):
        for mode in ("balanced", "strict"):
            self.assertFalse(policy.SafePolicy.preset(mode).lcu_patch_skin, mode)

    def test_policy_file_switch(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "safe.json"
            target.write_text(json.dumps({"mode": "balanced", "lcu_patch_skin": True}),
                              encoding="utf-8")
            self.assertTrue(policy.lcu_patch_enabled(target))
            target.write_text(json.dumps({"mode": "balanced"}), encoding="utf-8")
            self.assertFalse(policy.lcu_patch_enabled(target))
            self.assertFalse(policy.lcu_patch_enabled(Path(tmp) / "missing.json"))


class TestStartupHousekeeping(unittest.TestCase):
    """启动清理：上次日志脱敏 + onefile 残留目录。"""

    def test_redact_previous_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            log = session / "diag.log"
            log.write_text(
                "patching offset 0000000001CABDE0 at C:\\Users\\Administrator\\x.dll pid=99\n",
                encoding="utf-8",
            )
            report = session_mod.redact_previous_logs(session)
            self.assertTrue(report.passed, report.format(verbose=True))
            text = log.read_text(encoding="utf-8")
            self.assertNotIn("1CABDE0", text)
            self.assertNotIn("Administrator", text)
            self.assertIn("RDT003", {f.code for f in report.findings})

    def test_missing_logs_is_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = session_mod.redact_previous_logs(Path(tmp))
            self.assertTrue(report.passed)
            self.assertIn("RDT005", {f.code for f in report.findings})

    def test_clean_stale_mei(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "_MEI12345678"
            old.mkdir()
            (old / "x.bin").write_bytes(b"x")
            fresh = root / "_MEI87654321"
            fresh.mkdir()
            stale_time = 1000000.0
            os.utime(old, (stale_time, stale_time))
            removed = footprint.clean_stale_mei(24.0, root=root)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists(), "新目录不应被清理（可能是正在运行的实例）")

    def test_post_injection_check_returns_report(self):
        guard = SafeGuard(policy=policy.SafePolicy.preset("off"))
        report = guard.post_injection_check()
        self.assertTrue(report.scope)
        self.assertTrue(report.passed)


class TestGuard(unittest.TestCase):
    def test_gate_off_mode_is_noop(self):
        guard = SafeGuard(policy=policy.SafePolicy.preset("off"))
        rep = guard.gate("C:/definitely/not/here")
        self.assertTrue(rep.passed)
        self.assertEqual(rep.scope, "safe(off)")

    def test_gate_blocks_missing_overlay(self):
        guard = SafeGuard(policy=policy.SafePolicy.preset("balanced"))
        with self.assertRaises(SafeRefusal):
            guard.gate("C:/definitely/not/here")

    def test_patcher_factory_translates_refusal(self):
        """create_patcher 必须把 SafeRefusal 转成 PatcherError，UI 才能正常显示。"""
        import os
        from unittest import mock

        from eolian.patcher import PatcherError, create_patcher

        env = {"EOLIAN_SAFE_POLICY": "strict",
               "EOLIAN_SAFE_JSON": str(Path(tempfile.gettempdir()) / "no-such-safe.json")}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(PatcherError):
                create_patcher("C:/definitely/not/here")


if __name__ == "__main__":
    unittest.main()
