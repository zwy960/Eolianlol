"""皮肤交换 mod 生成器：把目标皮肤 SkinN 的对象图别名到 Skin0 的覆盖。

原理：
  游戏加载英雄时读取 data/characters/<英雄>/skins/skin0.bin 及其链接的 bin。
  把 skinN.bin 的根对象改名为 skin0，并把技能管理器与粒子对象合并为
  自包含入口（PROP 只存 path_hash；对象引用为 Hash 值）：
    - 模型/贴图等资源路径保持指向 skinN（原版 WAD 中全部存在）
    - 保留 SkinN 粒子路径，由 Skin0 根对象直接引用
    - 技能管理器映射到 base Skin0 管理器路径

产物：ltk 生态的 mod 目录（ltk_overlay::content::FsModContent 兼容）：
  mod.config.json
  content/base/<英雄>.wad.client/data/characters/<x>/skins/skin0.bin
  content/base/<英雄>.wad.client/data/characters/<x>/animations/skin0.bin
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

from .wad import Wad
from .winproc import hidden_process_kwargs

DEFAULT_BINHASHES_DIR = Path(__file__).resolve().parent.parent / "data" / "hashes"


def find_binhashes() -> Path:
    """优先使用环境变量，否则选择 data/hashes 下日期最新的 binhashes 表。"""
    env_path = os.environ.get("EOLIAN_BINHASHES", "").strip()
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise FileNotFoundError(f"EOLIAN_BINHASHES 不存在: {path}")
        return path
    paths = sorted(DEFAULT_BINHASHES_DIR.glob("binhashes-*.lhdb"), reverse=True)
    if not paths:
        raise FileNotFoundError(
            f"缺少 binhashes 哈希表: {DEFAULT_BINHASHES_DIR / 'binhashes-*.lhdb'}"
            "（从 LeagueToolkit/Mimir Releases 下载对应日期文件）"
        )
    return paths[0]


def fnv1a32(data: bytes) -> int:
    """ltk BinHash：FNV-1a 32（小写路径）。与 binhashes 表验证一致。"""
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def read_bin_dependencies(data: bytes) -> list[str]:
    """读取 PROP v2/v3 头部的依赖路径列表。"""
    if len(data) < 12 or data[:4] != b"PROP":
        return []
    version = struct.unpack_from("<I", data, 4)[0]
    if version < 2:
        return []
    count = struct.unpack_from("<I", data, 8)[0]
    pos = 12
    dependencies = []
    for _ in range(count):
        if pos + 2 > len(data):
            break
        length = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if pos + length > len(data):
            break
        dependencies.append(data[pos:pos + length].decode("utf-8", errors="replace"))
        pos += length
    return dependencies


def read_prop_structure(data: bytes) -> dict:
    """读取 PROP 外层结构，不解析属性值。"""
    if len(data) < 12 or data[:4] != b"PROP":
        raise ValueError("不是 PROP bin")
    version = struct.unpack_from("<I", data, 4)[0]
    pos = 8
    dependencies = []
    if version >= 2:
        count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for _ in range(count):
            length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            dependencies.append(data[pos:pos + length].decode("utf-8", errors="replace"))
            pos += length
    class_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    classes = list(struct.unpack_from(f"<{class_count}I", data, pos)) if class_count else []
    pos += 4 * class_count
    objects = []
    for class_hash in classes:
        size = struct.unpack_from("<I", data, pos)[0]
        if size < 6 or pos + 4 + size > len(data):
            raise ValueError(f"PROP 对象记录越界: offset={pos} size={size}")
        body = bytearray(data[pos + 4:pos + 4 + size])
        objects.append([class_hash, body])
        pos += 4 + size
    if pos != len(data):
        raise ValueError(f"PROP 对象表后仍有 {len(data) - pos} 字节")
    return {
        "version": version,
        "dependencies": dependencies,
        "objects": objects,
    }


def write_prop_structure(prop: dict) -> bytes:
    """按读取时的外层结构写回 PROP bin。"""
    out = bytearray(b"PROP")
    out += struct.pack("<I", prop["version"])
    if prop["version"] >= 2:
        dependencies = prop["dependencies"]
        out += struct.pack("<I", len(dependencies))
        for dependency in dependencies:
            encoded = dependency.encode("utf-8")
            out += struct.pack("<H", len(encoded))
            out += encoded
    objects = prop["objects"]
    out += struct.pack("<I", len(objects))
    for class_hash, _ in objects:
        out += struct.pack("<I", class_hash)
    for _, body in objects:
        out += struct.pack("<I", len(body))
        out += body
    return bytes(out)


def object_path_hash(body: bytes) -> int:
    return struct.unpack_from("<I", body, 0)[0]


def build_self_contained_skin(
    wad: Wad,
    champion: str,
    skin_num: int,
    skin_data: bytes,
) -> tuple[bytes, int]:
    """把 SkinN 根对象改名为 Skin0，并合并依赖 bin 中的 SkinN 粒子对象。

    对齐已验证的 cslol 皮肤 mod：粒子对象保留原 SkinN 路径，只把入口根对象
    改名为 Skin0。这样 Skin0 根对象能直接引用 SkinN 的技能特效对象。
    """
    from .lhdb import Lhdb

    prop = read_prop_structure(skin_data)
    skin_prefix = f"characters/{champion}/skins/skin{skin_num}"
    skin0_hash = fnv1a32(f"characters/{champion}/skins/skin0".encode("utf-8"))
    skin_hash = fnv1a32(skin_prefix.encode("utf-8"))
    existing = {object_path_hash(body) for _, body in prop["objects"]}
    renamed_root = False
    for _, body in prop["objects"]:
        if object_path_hash(body) == skin_hash:
            body[:4] = struct.pack("<I", skin0_hash)
            renamed_root = True
            break
    if not renamed_root:
        raise RuntimeError(f"未找到 Skin{skin_num} 根对象: {skin_prefix}")

    # 根对象改名后，其它对象里对 SkinN 路径的引用也要一起改成 Skin0，
    # 否则会留下指向 "Characters/X/Skins/SkinN" 的悬空引用（实测 Riven 会残留 1 处）。
    old_bytes = struct.pack("<I", skin_hash)
    new_bytes = struct.pack("<I", skin0_hash)
    for _, body in prop["objects"]:
        pos = 0
        while True:
            pos = body.find(old_bytes, pos)
            if pos < 0:
                break
            body[pos:pos + 4] = new_bytes
            pos += 4

    db = Lhdb.open(find_binhashes())
    merged = 0
    related_dependencies: list[str] = []
    particle_hashes = {
        object_path_hash(body)
        for _, body in prop["objects"]
        if (db.get(object_path_hash(body)) or "").lower().startswith(
            f"{skin_prefix}/"
        )
    }
    for dependency in dict.fromkeys(prop["dependencies"]):
        rel = dependency.replace("\\", "/").lower()
        if not rel.startswith("data/characters/") or not rel.endswith(".bin"):
            continue
        if rel.endswith(f"/skins/skin{skin_num}.bin"):
            continue
        dependency_data = wad.read_path(rel)
        if dependency_data is None:
            continue
        try:
            dependency_prop = read_prop_structure(dependency_data)
        except (ValueError, struct.error):
            continue
        added_from_dependency = 0
        for class_hash, body in dependency_prop["objects"]:
            path_hash = object_path_hash(body)
            path = db.get(path_hash)
            if not path or not path.lower().startswith(skin_prefix):
                continue
            if path.lower().startswith(f"{skin_prefix}/"):
                particle_hashes.add(path_hash)
            if path_hash in existing:
                continue
            prop["objects"].append([class_hash, bytearray(body)])
            existing.add(path_hash)
            merged += 1
            added_from_dependency += 1
        if added_from_dependency:
            related_dependencies.append(dependency)

    # 现代皮肤入口通过一个对象管理器引用全部技能粒子。base skin0 中同
    # class 的管理器路径才是游戏在 selectedSkinId=0 时查找的入口，因此把
    # SkinN 管理器改挂到 base 管理器哈希，并同步根对象引用。
    base_skin0 = wad.read_path(f"data/characters/{champion}/skins/skin0.bin")
    if base_skin0 is not None and particle_hashes:
        try:
            base_prop = read_prop_structure(base_skin0)
        except (ValueError, struct.error):
            base_prop = None
        if base_prop is not None:
            manager = max(
                (
                    (class_hash, body, sum(
                        struct.pack("<I", particle_hash) in body[6:]
                        for particle_hash in particle_hashes
                    ))
                    for class_hash, body in prop["objects"]
                ),
                key=lambda item: item[2],
            )
            manager_class, manager_body, manager_refs = manager
            base_manager = next(
                (
                    body
                    for class_hash, body in base_prop["objects"]
                    if class_hash == manager_class
                    and struct.unpack_from("<H", body, 4)[0] == 1
                ),
                None,
            )
            if manager_refs > 1 and base_manager is not None:
                old_hash = object_path_hash(manager_body)
                new_hash = object_path_hash(base_manager)
                print(
                    f"[modgen] 技能管理器已映射: {old_hash:08x} -> {new_hash:08x}",
                    flush=True,
                )
                old_bytes = struct.pack("<I", old_hash)
                new_bytes = struct.pack("<I", new_hash)
                for _, body in prop["objects"]:
                    pos = 0
                    while True:
                        pos = body.find(old_bytes, pos)
                        if pos < 0:
                            break
                        body[pos:pos + 4] = new_bytes
                        pos += 4

    # 只保留目标皮肤需要的依赖。曾把 base Skin0 分组一起保留，游戏会重新
    # 解析原皮粒子并覆盖掉 SkinN 粒子；但反过来把目标皮肤自己的依赖（组 bin）
    # 丢掉也不会没事 —— 粒子对象就在那些组 bin 里，丢了就是"模型变了、特效没了"。
    kept: list[str] = []
    for dependency in prop["dependencies"]:
        rel = dependency.replace("\\", "/").lower()
        if rel.endswith(f"/{champion}.bin") or rel.endswith(
            f"/animations/skin{skin_num}.bin"
        ):
            kept.append(dependency)

    # 目标皮肤自己的依赖表：带上所有含该皮肤编号的组 bin（如
    # Riven_Multi_Skins_Skin22_Skins_Skin45.bin），它们提供 VFX 对象。
    skin_token = re.compile(rf"skin{skin_num}(?![0-9])", re.IGNORECASE)
    src_data = wad.read_path(f"data/characters/{champion}/skins/skin{skin_num}.bin")
    if src_data is not None:
        try:
            src_prop = read_prop_structure(src_data)
        except (ValueError, struct.error):
            src_prop = None
        if src_prop is not None:
            for dependency in src_prop["dependencies"]:
                rel = dependency.replace("\\", "/").lower()
                if skin_token.search(rel) or rel.endswith(f"/{champion}.bin"):
                    kept.append(dependency)

    kept.extend(related_dependencies)
    kept.append(f"data/characters/{champion}/skins/skin{skin_num}.bin")
    prop["dependencies"] = list(dict.fromkeys(kept))
    return write_prop_structure(prop), merged


def build_alias_map(object_hashes: list[int], old_prefix: str, new_prefix: str,
                    binhashes_db) -> list[tuple[int, int]]:
    """对象路径含 old_prefix 的 → 前缀替换为 new_prefix 的映射。"""
    mapping = []
    for h in object_hashes:
        path = binhashes_db.get(h)
        if path and path.lower().startswith(old_prefix):
            new_path = new_prefix + path[len(old_prefix):]
            mapping.append((h, fnv1a32(new_path.lower().encode("utf-8"))))
    return mapping


def alias_bin_file(learn_overlay: str | Path, data: bytes, champion: str,
                   skin_num: int, kind: str) -> tuple[bytes, list[tuple[int, int]]]:
    """对一个 bin 字节流执行全对象别名（skins 或 animations）。

    返回 (改后字节, 映射)。kind: 'skins'|'animations'。
    流程：bin-list 拿对象 hash → binhashes 反查路径 → 生成映射 → bin-alias-map。
    """
    from .lhdb import Lhdb

    old_prefix = f"characters/{champion}/{kind}/skin{skin_num}"
    new_prefix = f"characters/{champion}/{kind}/skin0"
    # 字符串引用前缀（bin 内对象引用是 "Characters/X/Skins/SkinN..." 形式；
    # 等长替换为 Skin0；ASSETS 资源路径不受影响）
    cap = champion.capitalize()
    kind_cap = "Skins" if kind == "skins" else "Animations"
    str_prefixes = [f"Characters/{cap}/{kind_cap}/Skin{skin_num}",
                    f"Characters/{cap}/{kind_cap}/Skin0"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw = tmp / "raw.bin"
        raw.write_bytes(data)
        # 1) 对象 hash 列表
        proc = subprocess.run([str(learn_overlay), "bin-list", str(raw)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60,
                              **hidden_process_kwargs())
        if proc.returncode != 0:
            raise RuntimeError(f"bin-list 失败: {proc.stdout} {proc.stderr}")
        hashes = [int(ln.split()[1], 16) for ln in proc.stdout.splitlines()
                  if ln.startswith("object ")]
        if not hashes:
            return b"", []
        # 2) binhashes 反查
        db = Lhdb.open(find_binhashes())
        mapping = build_alias_map(hashes, old_prefix, new_prefix, db)
        if not mapping:
            raise RuntimeError(
                f"未找到含 {old_prefix} 的对象（binhashes 表可能过期）")
        return run_bin_alias_map(learn_overlay, raw, data, mapping, str_prefixes), mapping


def alias_champion_bin(learn_overlay: str | Path, data: bytes, champion: str,
                       skin_num: int, particle_map: list[tuple[int, int]]) -> bytes:
    """角色 bin：{Champion}Skin{N}_Manager → {Champion}Skin0_Manager + 引用替换。

    游戏按皮肤编号在角色 bin 查 {Champion}Skin{N}_Manager（技能特效管理器）；
    训练营 selectedSkinId=base 时找不到 Skin0_Manager → 无特效。
    别名后 base 编号也能拿到皮肤 N 的特效；Manager 属性引用的皮肤粒子对象
    哈希一并替换（指向已别名的 Skin0/Particles/*）。
    """
    old_mgr = f"characters/{champion}/spells/{champion}skin{skin_num}_manager"
    new_mgr = f"characters/{champion}/spells/{champion}skin0_manager"
    mapping = [(fnv1a32(old_mgr.lower().encode("utf-8")),
                fnv1a32(new_mgr.lower().encode("utf-8")))]
    # 合并粒子映射（Manager 属性引用皮肤粒子对象），去重保序
    seen = set(mapping)
    for pair in particle_map:
        if pair not in seen:
            seen.add(pair)
            mapping.append(pair)
    # 字符串引用前缀（Manager 属性可能字符串引用皮肤粒子对象）
    cap = champion.capitalize()
    str_prefixes = [f"Characters/{cap}/Skins/Skin{skin_num}",
                    f"Characters/{cap}/Skins/Skin0"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw = tmp / "raw.bin"
        raw.write_bytes(data)
        return run_bin_alias_map(learn_overlay, raw, data, mapping, str_prefixes)


def run_bin_alias_map(learn_overlay: str | Path, raw: Path, data: bytes,
                      mapping: list[tuple[int, int]],
                      string_prefixes: list[str] | None = None) -> bytes:
    """执行 bin-edit（serde JSON 中转，精确无字节误伤）并返回输出字节。"""
    with tempfile.TemporaryDirectory() as tmp2:
        tmp2 = Path(tmp2)
        map_file = tmp2 / "map.txt"
        map_file.write_text(
            "\n".join(f"{o:08x} {n:08x}" for o, n in mapping), encoding="utf-8")
        out = tmp2 / "out.bin"
        cmd = [str(learn_overlay), "bin-edit", str(raw), str(out), str(map_file)]
        prefixes = string_prefixes or []
        for i in range(0, len(prefixes) - 1, 2):
            cmd += ["--string-prefix", prefixes[i], prefixes[i + 1]]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120,
                              **hidden_process_kwargs())
        if proc.returncode != 0:
            raise RuntimeError(f"bin-edit 失败: {proc.stdout} {proc.stderr}")
        return out.read_bytes()


def rename_skin_manager(data: bytes, champion: str, skin_num: int) -> tuple[bytes, list[str]]:
    """把英雄主 bin 里的 ``{Champ}SkinN_Manager`` 复制成 Skin0 的几种命名。

    游戏在 selectedSkinId=0 时会去找 "Skin0 的技能管理器"；很多英雄只存在
    ``AatroxSkin33_Manager`` 这类按皮肤编号命名的对象，没有 Skin0 版本，
    于是回落到基础技能 → 引用的粒子对象在换肤后被替换掉 → 技能特效整片消失。
    这里把目标皮肤的管理器按几种常见命名各复制一份（原对象保留），
    游戏无论用哪种写法查找都能命中。
    """
    from .lhdb import Lhdb

    if len(data) < 12 or data[:4] != b"PROP":
        return data, []
    try:
        prop = read_prop_structure(data)
    except (ValueError, struct.error):
        return data, []

    db = Lhdb.open(find_binhashes())
    pattern = re.compile(
        rf"{champion}skin0*{skin_num}_?manager$", re.IGNORECASE)
    existing = {object_path_hash(body) for _, body in prop["objects"]}
    added: list[str] = []
    for class_hash, body in list(prop["objects"]):
        path = db.get(object_path_hash(body))
        if not path or not pattern.search(path.replace("\\", "/")):
            continue
        base = path.rsplit("/", 1)[-1]              # 例如 AatroxSkin33_Manager
        for candidate in (
            f"{champion.capitalize()}Skin0_Manager",
            f"{champion.capitalize()}Skin0Manager",
            f"{champion.capitalize()}Skin00Manager",
            f"{champion.capitalize()}Skin00_Manager",
        ):
            if candidate.lower() == base.lower():
                continue
            new_hash = fnv1a32(candidate.lower().encode("utf-8"))
            if new_hash in existing:
                continue
            clone = bytearray(body)
            struct.pack_into("<I", clone, 0, new_hash)
            prop["objects"].append([class_hash, clone])
            existing.add(new_hash)
            added.append(candidate)
    if not added:
        return data, []
    return write_prop_structure(prop), added


def find_dangling_references(wad, champion: str, prop: dict,
                             dependencies: list[str]) -> list[str]:
    """找出 skin0.bin 里"指向对象、但对象不存在"的引用（缺特效的根因）。

    判定：对象体里 4 字节对齐的值能反查成 ``characters/<champ>/...`` 路径，
    但该哈希既不在本 bin 的对象里、也不在任一保留依赖的对象里、
    也不是 WAD 里的文件 chunk —— 那就是悬空。
    """
    from .lhdb import Lhdb

    db = Lhdb.open(find_binhashes())
    known = {object_path_hash(body) for _, body in prop["objects"]}
    for dep in dependencies:
        rel = dep.replace("\\", "/").lower()
        if not rel.startswith("data/"):
            rel = "data/" + rel.lstrip("/")
        chunk = wad.chunk_by_path(rel)
        if chunk is None:
            continue
        try:
            dep_prop = read_prop_structure(wad.read_path(rel))
        except (ValueError, struct.error):
            continue
        for _, body in dep_prop["objects"]:
            known.add(object_path_hash(body))

    missing: dict[str, int] = {}
    prefix = f"characters/{champion}/"
    for _, body in prop["objects"]:
        for off in range(0, len(body) - 3, 4):
            word = struct.unpack_from("<I", body, off)[0]
            if word in known:
                continue
            path = db.get(word)
            if not path:
                continue
            low = path.lower()
            if not low.startswith(prefix):
                continue
            if wad.chunk_by_path("data/" + low) is not None:
                continue
            missing[path] = missing.get(path, 0) + 1
    return list(missing)


def build_skin_swap_mod(
    wad: Wad,
    champion: str,
    skin_num: int,
    out_mod_dir: str | Path,
    learn_overlay: str | Path | None = None,
) -> Path:
    """提取 skinN.bin，生成包含 SkinN 技能粒子的自包含 skin0 入口。"""

    src_path = f"data/characters/{champion}/skins/skin{skin_num}.bin"
    data = wad.read_path(src_path)
    if data is None:
        raise FileNotFoundError(f"WAD 中不存在 {src_path}")
    if learn_overlay is None or not Path(learn_overlay).is_file():
        raise RuntimeError("bin-alias 需要 eolian-overlay（先 cargo build --release）")

    data, vfx_objects = build_self_contained_skin(wad, champion, skin_num, data)
    if vfx_objects:
        print(f"[modgen] 已合并 {vfx_objects} 个 Skin{skin_num} 技能粒子对象",
              flush=True)

    # 构建期自检：悬空引用 = 进游戏后"模型在、特效没"的直接原因。
    # 这里只报警不阻断（个别引用可能由引擎侧兜底），但会让你在构建时就看见。
    try:
        prop = read_prop_structure(data)
        dangling = find_dangling_references(wad, champion, prop, prop["dependencies"])
    except (ValueError, struct.error):
        dangling = []
    if dangling:
        print(f"[modgen] 警告: {len(dangling)} 个悬空对象引用（可能缺技能特效）: "
              f"{dangling[:3]}", flush=True)
    else:
        print("[modgen] 依赖自检通过: 无悬空引用", flush=True)

    mod_root = Path(out_mod_dir)
    wad_name = f"{champion.capitalize()}.wad.client"

    # 覆盖目标路径：skin0.bin（游戏加载的入口）
    dst_rel = Path("data") / "characters" / champion / "skins" / "skin0.bin"
    target = mod_root / "content" / "base" / wad_name / dst_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    # 英雄主 bin：把 SkinN 的技能管理器补成 Skin0 命名。
    # 只改 skin0.bin 不够 —— 部分英雄的技能特效是英雄主 bin 里
    # 「{Champ}SkinN_Manager」这条链拉起来的，缺 Skin0 版本就没有特效。
    champ_rel = f"data/characters/{champion}/{champion}.bin"
    champ_raw = wad.read_path(champ_rel)
    if champ_raw:
        patched, added = rename_skin_manager(champ_raw, champion, skin_num)
        if added:
            dst = (mod_root / "content" / "base" / wad_name
                   / "data" / "characters" / champion / f"{champion}.bin")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(patched)
            print("[modgen] 技能管理器已补 Skin0 命名: " + ", ".join(added), flush=True)

    # 最小 mod.config.json（ltk_mod_project::ModProject 必填字段；authors 为字符串数组）
    # 注意：必须显式声明 layers —— ltk_overlay 0.5.2 的收集逻辑直接遍历
    # project.layers，缺省空数组会收集到 0 个覆盖文件（注释里的"默认 base 层"
    # 约定在 0.5.2 中并未实现）。
    config = {
        "name": f"skin-swap-{champion}-{skin_num}",
        "display_name": f"{champion} Skin{skin_num} -> Skin0",
        "version": "1.0.0",
        "description": f"auto-generated skin swap: show skin{skin_num} as skin0 ({champion})",
        "authors": ["Eolian"],
        "layers": [{"name": "base", "priority": 0}],
    }
    (mod_root / "mod.config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return mod_root


def build_swap_for_skin(
    game_dir: str | Path,
    champion: str,
    skin_num: int,
    out_mod_dir: str | Path,
    learn_overlay: str | Path | None = None,
) -> Path:
    """便捷入口：打开英雄 WAD 并生成 mod。champion 为小写别名。"""
    wad_path = Path(game_dir) / "DATA" / "FINAL" / "Champions" / f"{champion.capitalize()}.wad.client"
    if not wad_path.is_file():
        # 尝试大小写不敏感查找
        matches = list((Path(game_dir) / "DATA" / "FINAL" / "Champions").glob(f"{champion}*.wad.client"))
        if not matches:
            raise FileNotFoundError(f"找不到英雄 WAD: {wad_path}")
        wad_path = matches[0]
    with Wad(wad_path) as wad:
        return build_skin_swap_mod(wad, champion, skin_num, out_mod_dir,
                                   learn_overlay=learn_overlay)
