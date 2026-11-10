"""纯标准库 PE 解析：只读，不加载、不执行。

用途：给"版本门禁"提供稳定的 build 指纹，避免把偏移硬编码进注入流程
（Yoousn 的 ``CRYPTO_free offset: 0x1C6AA00 → 0x1CABDE0`` 就是硬编码的代价）。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path


class PEError(RuntimeError):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_size: int
    raw_pointer: int
    characteristics: int

    def contains(self, rva: int) -> bool:
        span = max(self.virtual_size, self.raw_size)
        return self.virtual_address <= rva < self.virtual_address + span


class PEImage:
    """最小可用 PE 读取器（PE32/PE32+），只解析头和导入表。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self._data = self.path.read_bytes()
        except OSError as exc:
            raise PEError(f"无法读取: {self.path} ({exc})") from exc
        data = self._data
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise PEError(f"不是 PE 文件: {self.path}")
        self.e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[self.e_lfanew:self.e_lfanew + 4] != b"PE\0\0":
            raise PEError(f"PE 签名缺失: {self.path}")

        coff = self.e_lfanew + 4
        (
            self.machine,
            self.section_count,
            self.timestamp,
            _sym_ptr,
            _sym_count,
            self.optional_size,
            self.characteristics,
        ) = struct.unpack_from("<HHIIIHH", data, coff)

        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        if magic not in (0x10B, 0x20B):
            raise PEError(f"未知可选头 magic: 0x{magic:04X}")
        self.is_64 = magic == 0x20B

        if self.is_64:
            self.image_base = struct.unpack_from("<Q", data, opt + 24)[0]
            self.size_of_image = struct.unpack_from("<I", data, opt + 56)[0]
            self.size_of_headers = struct.unpack_from("<I", data, opt + 60)[0]
            self.checksum = struct.unpack_from("<I", data, opt + 64)[0]
            self.subsystem = struct.unpack_from("<H", data, opt + 68)[0]
            self.entry_point = struct.unpack_from("<I", data, opt + 16)[0]
            self.data_dir_offset = opt + 112
        else:
            self.image_base = struct.unpack_from("<I", data, opt + 28)[0]
            self.size_of_image = struct.unpack_from("<I", data, opt + 56)[0]
            self.size_of_headers = struct.unpack_from("<I", data, opt + 60)[0]
            self.checksum = struct.unpack_from("<I", data, opt + 64)[0]
            self.subsystem = struct.unpack_from("<H", data, opt + 68)[0]
            self.entry_point = struct.unpack_from("<I", data, opt + 16)[0]
            self.data_dir_offset = opt + 96

        self.sections: list[Section] = []
        table = opt + self.optional_size
        for index in range(self.section_count):
            off = table + index * 40
            raw_name = data[off:off + 8].split(b"\0", 1)[0]
            name = raw_name.decode("ascii", errors="replace")
            vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            chars = struct.unpack_from("<I", data, off + 36)[0]
            self.sections.append(Section(name, va, vsize, raw_size, raw_ptr, chars))

        self._imports: dict[str, list[str]] | None = None

    # ---- 基础查询 ----

    @property
    def name(self) -> str:
        return self.path.name

    def section_by_name(self, needle: str) -> Section | None:
        needle = needle.lower()
        for section in self.sections:
            if needle in section.name.lower():
                return section
        return None

    def rva_to_offset(self, rva: int) -> int:
        for section in self.sections:
            if section.contains(rva):
                delta = rva - section.virtual_address
                if delta >= section.raw_size:
                    break
                return section.raw_pointer + delta
        if rva < self.size_of_headers:
            return rva
        raise PEError(f"RVA 不在任何节内: 0x{rva:X}")

    def _cstring(self, offset: int) -> str:
        end = self._data.find(b"\0", offset)
        if end < 0:
            end = len(self._data)
        return self._data[offset:end].decode("ascii", errors="replace")

    @property
    def imports(self) -> dict[str, list[str]]:
        if self._imports is not None:
            return self._imports
        result: dict[str, list[str]] = {}
        data = self._data
        try:
            import_rva = struct.unpack_from("<I", data, self.data_dir_offset + 8)[0]
        except struct.error:
            import_rva = 0
        if import_rva:
            try:
                descriptor = self.rva_to_offset(import_rva)
            except PEError:
                descriptor = -1
            while descriptor > 0:
                chunk = data[descriptor:descriptor + 20]
                if len(chunk) < 20:
                    break
                original_first_thunk, _ts, _fwd, name_rva, first_thunk = struct.unpack("<IIIII", chunk)
                if name_rva == 0 and first_thunk == 0:
                    break
                try:
                    dll = self._cstring(self.rva_to_offset(name_rva))
                except PEError:
                    dll = "?"
                thunk_rva = original_first_thunk or first_thunk
                functions: list[str] = []
                if thunk_rva:
                    try:
                        thunk_off = self.rva_to_offset(thunk_rva)
                    except PEError:
                        thunk_off = -1
                    step = 8 if self.is_64 else 4
                    ordinal_flag = 1 << (63 if self.is_64 else 31)
                    limit = 0
                    while thunk_off > 0 and limit < 65536:
                        limit += 1
                        if self.is_64:
                            value = struct.unpack_from("<Q", data, thunk_off)[0]
                        else:
                            value = struct.unpack_from("<I", data, thunk_off)[0]
                        if value == 0:
                            break
                        if value & ordinal_flag:
                            functions.append(f"#{value & 0xFFFF}")
                        else:
                            try:
                                functions.append(self._cstring(self.rva_to_offset((value & 0x7FFFFFFF) + 2)))
                            except PEError:
                                functions.append("?")
                        thunk_off += step
                result.setdefault(dll, []).extend(functions)
                descriptor += 20
        self._imports = result
        return result

    def has_import(self, dll: str, function: str | None = None) -> bool:
        want = dll.lower()
        for name, functions in self.imports.items():
            if name.lower() != want:
                continue
            if function is None:
                return True
            return function in functions
        return False

    def import_function_owner(self, function: str) -> str | None:
        for name, functions in self.imports.items():
            if function in functions:
                return name
        return None

    # ---- 指纹 ----

    def fingerprint(self) -> str:
        """稳定 build 指纹：时间戳 + 镜像大小 + 入口 + 节表 + 导入 DLL 列表。

        不含代码哈希（16MB 全量哈希每次注入都要几百毫秒，且对同一 build 无增量信息）。
        """
        feed = [
            f"ts={self.timestamp:08X}",
            f"img={self.size_of_image:08X}",
            f"ep={self.entry_point:08X}",
            f"sum={self.checksum:08X}",
            "secs=" + ",".join(
                f"{s.name}:{s.virtual_address:X}:{s.virtual_size:X}" for s in self.sections
            ),
            "dlls=" + ",".join(sorted(name.lower() for name in self.imports)),
        ]
        digest = hashlib.sha256("|".join(feed).encode("utf-8")).hexdigest()
        return digest[:16]

    def summary(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "machine": f"0x{self.machine:04X}",
            "x64": self.is_64,
            "timestamp": self.timestamp,
            "built_at": _utc(self.timestamp),
            "size_of_image": self.size_of_image,
            "entry_point": self.entry_point,
            "checksum_ok": self.checksum != 0,
            "sections": [s.name for s in self.sections],
            "fingerprint": self.fingerprint(),
        }


def _utc(timestamp: int) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(
            timestamp, datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "?"
