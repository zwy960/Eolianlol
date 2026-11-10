"""覆盖 WAD 合规校验：只允许"看起来像原版"的改动。

三条硬约束（对应防封里最值钱的那一条：**不改判定、不加文件**）：

1. **chunk 集合不得越界** —— 覆盖里出现的每个 chunk 哈希，原版 WAD 里必须本来就有。
   多出来一个 = 多了一个新文件 = 客户端/反作弊一眼能看出被塞了东西。
2. **改动比例可控** —— 被替换的 chunk 占原版比例超过阈值就拦（正常换肤只动 skin 相关条目）。
3. **base-skin 结构不劣化** —— ``skin0.bin`` 的依赖表与对象类不出现"凭空新增"，
   避免出现"换了模型/多出骨骼"这种影响观感与判定的改动。

另外顺手挡住：覆盖目录里出现任何非 ``.wad.client`` 的载荷（dll/exe/js/json）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..hashing import chunk_path_hash
from ..modgen import read_prop_structure
from ..wad import Wad, WadError
from .policy import SafePolicy
from .report import Report

SKIN0_PATHS = (
    "data/characters/{champ}/skins/skin0.bin",
    "data/characters/{champ}/animations/skin0.bin",
)


@dataclass
class WadScan:
    name: str
    base_path: Path | None
    overlay_path: Path
    base_chunks: int = 0
    overlay_chunks: int = 0
    extra: list[int] = field(default_factory=list)
    replaced: int = 0
    identical: int = 0
    ratio: float = 0.0
    skin_diffs: list[str] = field(default_factory=list)
    skin_blocks: list[str] = field(default_factory=list)
    base_hashes: set[int] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "base": str(self.base_path) if self.base_path else None,
            "overlay": str(self.overlay_path),
            "base_chunks": self.base_chunks,
            "overlay_chunks": self.overlay_chunks,
            "extra_chunks": len(self.extra),
            "replaced": self.replaced,
            "identical": self.identical,
            "replaced_ratio": round(self.ratio, 4),
            "skin_diffs": self.skin_diffs,
            "skin_blocks": self.skin_blocks,
        }


def _champion_from_name(name: str) -> str:
    return name.split(".", 1)[0].strip().lower()


def _scan_one(overlay_wad: Path, base_wad: Path | None) -> WadScan:
    scan = WadScan(name=overlay_wad.name, base_path=base_wad, overlay_path=overlay_wad)
    try:
        with Wad(overlay_wad) as overlay:
            overlay_chunks = {chunk.path_hash: chunk for chunk in overlay.chunks()}
    except (OSError, WadError) as exc:
        scan.skin_diffs.append(f"无法读取覆盖 WAD: {exc}")
        return scan
    scan.overlay_chunks = len(overlay_chunks)

    if base_wad is None or not base_wad.is_file():
        scan.skin_diffs.append("原版 WAD 不存在，无法做集合比对（只做结构检查）")
        return scan

    try:
        with Wad(base_wad) as base:
            base_chunks = {chunk.path_hash: chunk for chunk in base.chunks()}
    except (OSError, WadError) as exc:
        scan.skin_diffs.append(f"无法读取原版 WAD: {exc}")
        return scan
    scan.base_chunks = len(base_chunks)

    scan.extra = [h for h in overlay_chunks if h not in base_chunks]
    for path_hash, chunk in overlay_chunks.items():
        original = base_chunks.get(path_hash)
        if original is None:
            continue
        if original.checksum == chunk.checksum:
            scan.identical += 1
        else:
            scan.replaced += 1
    compared = scan.identical + scan.replaced
    scan.ratio = (scan.replaced / compared) if compared else 0.0
    scan.base_hashes = set(base_chunks)
    return scan


def _skin0_compare(overlay_wad: Path, base_wad: Path,
                   base_hashes: set[int]) -> tuple[list[str], list[str]]:
    """比较 skin0.bin 的依赖表。

    返回 ``(blocking, notes)``：
      blocking  依赖路径在 WAD 里根本不存在，或跨英雄引用 —— 这才是真问题
      notes     对象类/数量变化 —— 换肤本来就会合并对象，只作记录
    """
    champ = _champion_from_name(overlay_wad.name)
    owner = f"data/characters/{champ}/"
    blocking: list[str] = []
    notes: list[str] = []
    for template in SKIN0_PATHS:
        path = template.format(champ=champ)
        try:
            with Wad(overlay_wad) as overlay, Wad(base_wad) as base:
                overlay_bytes = overlay.read_path(path)
                base_bytes = base.read_path(path)
        except ImportError:
            notes.append("缺少 zstandard 库，跳过 skin0 深度比对")
            return blocking, notes
        except (OSError, WadError) as exc:
            notes.append(f"{path}: 读取失败 ({exc})")
            continue
        if not overlay_bytes or not base_bytes:
            continue
        try:
            overlay_prop = read_prop_structure(overlay_bytes)
            base_prop = read_prop_structure(base_bytes)
        except (ValueError, struct.error) as exc:
            notes.append(f"{path}: PROP 解析失败 ({exc})")
            continue

        unresolved: list[str] = []
        foreign: list[str] = []
        for dep in overlay_prop["dependencies"]:
            if dep in base_prop["dependencies"]:
                continue
            normalized = dep.replace("\\", "/").lower()
            if not normalized.startswith(owner):
                foreign.append(dep)
                continue
            if chunk_path_hash(normalized) not in base_hashes:
                unresolved.append(dep)
        if unresolved:
            blocking.append(
                f"{path}: 依赖指向 WAD 里不存在的文件 {len(unresolved)} 个"
                f"（前 3 个 {unresolved[:3]}）"
            )
        if foreign:
            blocking.append(
                f"{path}: 依赖跨到别的英雄目录 {len(foreign)} 个（前 3 个 {foreign[:3]}）"
            )

        base_count = len(base_prop["objects"]) or 1
        drift = abs(len(overlay_prop["objects"]) - base_count) / base_count
        if drift > 2.0:
            notes.append(
                f"{path}: 对象数量偏差 {drift:.0%}"
                f"（原版 {base_count} → 覆盖 {len(overlay_prop['objects'])}）"
            )
    return blocking, notes


def verify(overlay_root: str | Path, game_dir: str | Path, policy: SafePolicy) -> Report:
    report = Report("覆盖 WAD 合规")
    root = Path(overlay_root)
    game = Path(game_dir)
    if not root.is_dir():
        report.block("OV001", "覆盖目录不存在", path=str(root))
        return report

    wads = sorted(root.rglob("*.wad.client"))
    if not wads:
        report.block("OV002", "覆盖目录里没有任何 .wad.client", path=str(root))
        return report

    # 1) 载荷检查：覆盖目录只应该是 WAD
    if policy.strict_overlay_paths:
        allowed = tuple(s.lower() for s in policy.allowed_overlay_suffixes)
        intruders = [
            item for item in root.rglob("*")
            if item.is_file() and not item.name.lower().endswith(allowed)
        ]
        if intruders:
            report.block(
                "OV003",
                "覆盖目录里出现非 WAD 载荷（可能被塞了可执行内容）",
                count=len(intruders),
                samples=[str(p.relative_to(root)) for p in intruders[:5]],
            )
        else:
            report.ok("OV004", "覆盖目录只有 WAD 文件")

    scans: list[WadScan] = []
    for overlay_wad in wads:
        relative = overlay_wad.relative_to(root)
        base_wad = game / relative
        base = base_wad if base_wad.is_file() else None
        scan = _scan_one(overlay_wad, base)
        if base is not None:
            blocking, notes = _skin0_compare(overlay_wad, base, scan.base_hashes)
            scan.skin_blocks = blocking
            scan.skin_diffs = notes
        scans.append(scan)

    for scan in scans:
        if scan.base_path is None:
            report.warn(
                "OV005",
                "覆盖里的 WAD 在原版目录找不到对应文件",
                wad=scan.name,
            )
            continue

        if policy.require_overlay_chunk_subset and scan.extra:
            report.block(
                "OV006",
                "覆盖里出现了原版不存在的 chunk（等于往客户端塞新文件）",
                wad=scan.name,
                extra=len(scan.extra),
                samples=[f"0x{h:016X}" for h in scan.extra[:5]],
            )
        elif scan.extra:
            report.warn("OV007", "覆盖包含原版不存在的 chunk", wad=scan.name, extra=len(scan.extra))

        if scan.ratio > policy.max_replaced_chunk_ratio:
            report.block(
                "OV008",
                "被替换的 chunk 比例超过阈值（改动面过大，容易偏离原版特征）",
                wad=scan.name,
                ratio=f"{scan.ratio:.1%}",
                limit=f"{policy.max_replaced_chunk_ratio:.0%}",
                replaced=scan.replaced,
                compared=scan.identical + scan.replaced,
            )
        else:
            report.ok(
                "OV009",
                "改动面在阈值内",
                wad=scan.name,
                ratio=f"{scan.ratio:.1%}",
                replaced=scan.replaced,
                identical=scan.identical,
            )

        if scan.skin_blocks:
            level = "block" if policy.forbid_base_skin_change else "warn"
            report.add(
                level,
                "OV010",
                "skin0 依赖不可解析（引用了 WAD 里不存在/别的英雄的文件）",
                wad=scan.name,
                notes=scan.skin_blocks[:5],
            )
        if scan.skin_diffs:
            report.info(
                "OV011",
                "skin0 结构差异（换肤正常会合并对象，仅记录）",
                wad=scan.name,
                notes=scan.skin_diffs[:5],
            )
        if not scan.skin_blocks and not scan.skin_diffs:
            report.ok("OV013", "skin0 基线与原版一致", wad=scan.name)

    report.info("OV012", "覆盖扫描完成", wads=len(scans),
                details=[scan.to_dict() for scan in scans])
    return report
