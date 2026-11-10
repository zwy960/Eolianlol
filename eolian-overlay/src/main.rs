// eolian-overlay: 把 mod 目录构建为覆盖 WAD（复用 ltk_overlay，与 ltk-manager 同款引擎）。
//
// 用法:
//   eolian-overlay build --game-dir <Game目录> --mod-dir <mod目录> \
//       --overlay-root <输出> --state-dir <状态目录>
//
// mod 目录布局（FsModContent）:
//   mod.config.json
//   content/base/<WAD名>.wad.client/<相对路径>/<文件>

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use ltk_overlay::builder::{EnabledMod, OverlayBuilder};
use ltk_overlay::content::FsModContent;

#[derive(Parser)]
#[command(name = "eolian-overlay", version, about = "mod 目录 -> 覆盖 WAD（复用 ltk_overlay）")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 构建覆盖 WAD
    Build {
        /// 英雄联盟 Game 目录（含 DATA/FINAL）
        #[arg(long)]
        game_dir: PathBuf,
        /// mod 目录（含 mod.config.json 与 content/）
        #[arg(long)]
        mod_dir: PathBuf,
        /// 覆盖 WAD 输出目录（patched wads）
        #[arg(long)]
        overlay_root: PathBuf,
        /// 状态目录（game_index 缓存、overlay.json）
        #[arg(long)]
        state_dir: PathBuf,
    },
    /// 对象改名：把 bin 里 path_hash 为 old_path 的对象改名为 new_path。
    /// PROP 只存 path_hash 不存路径字符串；用于 skinN 对象别名到 skin0
    /// （满足 DLL 的 base-skin 验证 78555f28）。
    BinAlias {
        /// 输入 bin 文件
        input: PathBuf,
        /// 输出 bin 文件
        output: PathBuf,
        /// 旧对象路径（如 characters/lissandra/skins/skin34）
        old_path: String,
        /// 新对象路径（如 characters/lissandra/skins/skin0）
        new_path: String,
    },
    /// 列出 bin 的对象（path_hash 十六进制 + class_hash），供 binhashes 表反查
    BinList {
        /// 输入 bin 文件
        input: PathBuf,
    },
    /// 按映射批量改名对象 + 替换属性引用。
    /// 映射文件每行：`<old_hash_hex> <new_hash_hex>`（对象 path_hash 改名）
    /// 同时把属性值里的 Hash 引用（命中 old）替换为新 hash；
    /// String 引用按前缀规则由 --string-prefix 指定替换。
    BinAliasMap {
        /// 输入 bin 文件
        input: PathBuf,
        /// 输出 bin 文件
        output: PathBuf,
        /// 映射文件（每行 old new，hex u32）
        map: PathBuf,
        /// 可选字符串前缀替换：--string-prefix <old> <new>（可多次）
        #[arg(long = "string-prefix", num_args = 2, action = clap::ArgAction::Append)]
        string_prefixes: Vec<String>,
    },
    /// 同 bin-alias-map，但通过 serde JSON 中转（无字节级误伤风险）：
    /// 对象表 key + path_hash + Hash 属性值 + String 属性值统一精确替换。
    BinEdit {
        /// 输入 bin 文件
        input: PathBuf,
        /// 输出 bin 文件
        output: PathBuf,
        /// 映射文件（每行 old new，hex u32）
        map: PathBuf,
        /// 可选字符串前缀替换：--string-prefix <old> <new>（可多次）
        #[arg(long = "string-prefix", num_args = 2, action = clap::ArgAction::Append)]
        string_prefixes: Vec<String>,
    },
    /// 把 bin 转储为 JSON（属性完整可见，用于分析引用链）
    BinDump {
        /// 输入 bin 文件
        input: PathBuf,
        /// 输出 JSON 文件
        output: PathBuf,
    },
    /// 合并两个 bin 的对象表（base 先、override 后，冲突时 override 胜），
    /// dependencies 取并集。用于：base 特效对象 + skinN 别名对象合入 skin0.bin，
    /// 使 base 编号加载时 base 特效对象仍存在（避免特效空白）。
    BinMerge {
        /// 基础 bin（如原版 skin0.bin）
        base: PathBuf,
        /// 覆盖 bin（如 skinN 别名版）
        override_bin: PathBuf,
        /// 输出 bin
        output: PathBuf,
    },
}

fn utf8(p: &PathBuf, what: &str) -> Result<String, String> {
    p.to_str()
        .map(str::to_owned)
        .ok_or_else(|| format!("{what} 不是合法 UTF-8 路径: {}", p.display()))
}

fn main() -> Result<(), String> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Build {
            game_dir,
            mod_dir,
            overlay_root,
            state_dir,
        } => {
            let game_dir = utf8(&game_dir, "game_dir")?;
            let mod_dir = utf8(&mod_dir, "mod_dir")?;
            let overlay_root = utf8(&overlay_root, "overlay_root")?;
            let state_dir = utf8(&state_dir, "state_dir")?;

            let mut builder = OverlayBuilder::new(
                game_dir.clone().into(),
                overlay_root.clone().into(),
                state_dir.into(),
            );
            let content = FsModContent::new(mod_dir.clone().into());
            let enabled = EnabledMod {
                id: "skin-swap".to_string(),
                content: Box::new(content),
                enabled_layers: None,
            };
            builder.set_enabled_mods(vec![enabled]);

            let result = builder
                .build()
                .map_err(|e| format!("overlay 构建失败: {e}"))?;
            println!(
                "OK overlay_root={} wads_built={} wads_reused={} elapsed_ms={}",
                result.overlay_root,
                result.wads_built.len(),
                result.wads_reused.len(),
                result.build_time.as_millis()
            );
            for wad in &result.wads_built {
                println!("built: {}", wad);
            }
            for wad in &result.wads_reused {
                println!("reused: {}", wad);
            }
            Ok(())
        }
        Cmd::BinAlias {
            input,
            output,
            old_path,
            new_path,
        } => bin_alias_raw(&input, &output, &old_path, &new_path),
        Cmd::BinList { input } => bin_list_raw(&input),
        Cmd::BinAliasMap {
            input,
            output,
            map,
            string_prefixes,
        } => bin_alias_map_raw(&input, &output, &map, &string_prefixes),
        Cmd::BinEdit {
            input,
            output,
            map,
            string_prefixes,
        } => bin_edit_raw(&input, &output, &map, &string_prefixes),
        Cmd::BinDump { input, output } => bin_dump(&input, &output),
        Cmd::BinMerge {
            base,
            override_bin,
            output,
        } => bin_merge(&base, &override_bin, &output),
    }
}

#[derive(Clone)]
struct RawObject {
    class_hash: u32,
    body: Vec<u8>,
}

struct RawProp {
    version: u32,
    dependencies: Vec<String>,
    objects: Vec<RawObject>,
}

fn read_u16(data: &[u8], pos: &mut usize) -> Result<u16, String> {
    if *pos + 2 > data.len() {
        return Err("bin 意外结束（u16）".to_string());
    }
    let value = u16::from_le_bytes([data[*pos], data[*pos + 1]]);
    *pos += 2;
    Ok(value)
}

fn read_u32(data: &[u8], pos: &mut usize) -> Result<u32, String> {
    if *pos + 4 > data.len() {
        return Err("bin 意外结束（u32）".to_string());
    }
    let value = u32::from_le_bytes([
        data[*pos],
        data[*pos + 1],
        data[*pos + 2],
        data[*pos + 3],
    ]);
    *pos += 4;
    Ok(value)
}

fn object_path_hash(object: &RawObject) -> Result<u32, String> {
    if object.body.len() < 6 {
        return Err("对象记录过短，缺少 path_hash/property_count".to_string());
    }
    Ok(u32::from_le_bytes([
        object.body[0],
        object.body[1],
        object.body[2],
        object.body[3],
    ]))
}

/// 按 PROP 外层结构读取对象记录，不反序列化属性值。
///
/// 对象记录为 `u32 size + size bytes`，body 前 4 字节是 path_hash，
/// 随后是 `u16 property_count`。跳过属性值后即可完整遍历任意受支持版本。
fn parse_raw_prop(path: &PathBuf) -> Result<RawProp, String> {
    let data = std::fs::read(path).map_err(|e| format!("读取 bin 失败 {path:?}: {e}"))?;
    if data.len() < 12 || &data[0..4] != b"PROP" {
        return Err(format!("不是 PROP bin: {path:?}"));
    }
    let mut pos = 4usize;
    let version = read_u32(&data, &mut pos)?;
    if !(1..=3).contains(&version) {
        return Err(format!("不支持的 PROP 版本: {version}"));
    }
    let mut dependencies = Vec::new();
    if version >= 2 {
        let dep_count = read_u32(&data, &mut pos)? as usize;
        dependencies.reserve(dep_count);
        for _ in 0..dep_count {
            let len = read_u16(&data, &mut pos)? as usize;
            if pos + len > data.len() {
                return Err("依赖字符串越界".to_string());
            }
            dependencies.push(
                String::from_utf8_lossy(&data[pos..pos + len]).into_owned(),
            );
            pos += len;
        }
    }

    let class_count = read_u32(&data, &mut pos)? as usize;
    let mut classes = Vec::with_capacity(class_count);
    for _ in 0..class_count {
        classes.push(read_u32(&data, &mut pos)?);
    }

    let mut objects = Vec::with_capacity(class_count);
    for class_hash in classes {
        let size = read_u32(&data, &mut pos)? as usize;
        if size < 6 || pos + size > data.len() {
            return Err(format!(
                "对象记录越界（offset={pos}, size={size}, file={}）",
                data.len(),
            ));
        }
        let body = data[pos..pos + size].to_vec();
        object_path_hash(&RawObject {
            class_hash,
            body: body.clone(),
        })?;
        objects.push(RawObject { class_hash, body });
        pos += size;
    }
    if pos != data.len() {
        return Err(format!(
            "PROP 对象表结束后仍有 {} 字节",
            data.len() - pos
        ));
    }
    Ok(RawProp {
        version,
        dependencies,
        objects,
    })
}

fn write_raw_prop(prop: &RawProp, output: &PathBuf) -> Result<(), String> {
    let mut data = Vec::new();
    data.extend_from_slice(b"PROP");
    data.extend_from_slice(&prop.version.to_le_bytes());
    if prop.version >= 2 {
        data.extend_from_slice(&(prop.dependencies.len() as u32).to_le_bytes());
        for dep in &prop.dependencies {
            let bytes = dep.as_bytes();
            let len = u16::try_from(bytes.len())
                .map_err(|_| format!("依赖路径过长: {}", dep.len()))?;
            data.extend_from_slice(&len.to_le_bytes());
            data.extend_from_slice(bytes);
        }
    }
    data.extend_from_slice(&(prop.objects.len() as u32).to_le_bytes());
    for object in &prop.objects {
        data.extend_from_slice(&object.class_hash.to_le_bytes());
    }
    for object in &prop.objects {
        let size = u32::try_from(object.body.len())
            .map_err(|_| "对象记录超过 4 GiB".to_string())?;
        data.extend_from_slice(&size.to_le_bytes());
        data.extend_from_slice(&object.body);
    }
    std::fs::write(output, data).map_err(|e| format!("创建输出失败 {output:?}: {e}"))
}

fn parse_alias_map(path: &PathBuf) -> Result<std::collections::HashMap<u32, u32>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("读映射失败: {e}"))?;
    let mut result = std::collections::HashMap::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.split_whitespace();
        let old = u32::from_str_radix(
            parts.next().ok_or_else(|| format!("映射第{i}行缺 old"))?,
            16,
        )
        .map_err(|e| format!("映射第{i}行 old 非法: {e}"))?;
        let new = u32::from_str_radix(
            parts.next().ok_or_else(|| format!("映射第{i}行缺 new"))?,
            16,
        )
        .map_err(|e| format!("映射第{i}行 new 非法: {e}"))?;
        result.insert(old, new);
    }
    Ok(result)
}

fn replace_u32_refs(data: &mut [u8], aliases: &std::collections::HashMap<u32, u32>) -> usize {
    let mut refs = 0usize;
    for (&old, &new) in aliases {
        let old_bytes = old.to_le_bytes();
        let new_bytes = new.to_le_bytes();
        let mut i = 0usize;
        while i + 4 <= data.len() {
            if data[i..i + 4] == old_bytes {
                data[i..i + 4].copy_from_slice(&new_bytes);
                refs += 1;
                i += 4;
            } else {
                i += 1;
            }
        }
    }
    refs
}

fn replace_string_prefixes(
    data: &mut [u8],
    string_prefixes: &[String],
) -> usize {
    let mut refs = 0usize;
    for pair in string_prefixes.chunks(2) {
        if pair.len() != 2 {
            continue;
        }
        let old = pair[0].as_bytes();
        let new = pair[1].as_bytes();
        if old.is_empty() || old.len() != new.len() {
            continue;
        }
        let mut i = 0usize;
        while i + 2 + old.len() <= data.len() {
            let len = u16::from_le_bytes([data[i], data[i + 1]]) as usize;
            if len >= old.len()
                && i + 2 + len <= data.len()
                && data[i + 2..i + 2 + old.len()] == *old
            {
                data[i + 2..i + 2 + old.len()].copy_from_slice(new);
                refs += 1;
            }
            i += 1;
        }
    }
    refs
}

/// 原始结构别名：改名对象 path_hash，并替换对象 body 内的 Hash/String 引用。
fn alias_raw_objects(
    input: &PathBuf,
    output: &PathBuf,
    aliases: &std::collections::HashMap<u32, u32>,
    string_prefixes: &[String],
) -> Result<(), String> {
    let mut prop = parse_raw_prop(input)?;
    let mut renamed = 0usize;
    let mut refs = 0usize;
    for object in &mut prop.objects {
        let old = object_path_hash(object)?;
        if let Some(&new) = aliases.get(&old) {
            object.body[0..4].copy_from_slice(&new.to_le_bytes());
            renamed += 1;
        }
        refs += replace_u32_refs(&mut object.body, aliases);
        refs += replace_string_prefixes(&mut object.body, string_prefixes);
    }
    write_raw_prop(&prop, output)?;
    println!(
        "bin-alias-map: 对象改名 {renamed} 个，引用替换 {refs} 处，共 {} 个对象",
        prop.objects.len()
    );
    println!("OK 已写出 {output:?}");
    Ok(())
}

/// bin-merge：合并两个 bin（对象表 union，override 胜；dependencies 并集）。
fn bin_merge(base: &PathBuf, override_bin: &PathBuf, output: &PathBuf) -> Result<(), String> {
    let mut merged = parse_raw_prop(base)?;
    let override_prop = parse_raw_prop(override_bin)?;
    let base_count = merged.objects.len();
    let override_count = override_prop.objects.len();

    for dep in override_prop.dependencies {
        if !merged.dependencies.contains(&dep) {
            merged.dependencies.push(dep);
        }
    }
    for object in override_prop.objects {
        let hash = object_path_hash(&object)?;
        if let Some(index) = merged
            .objects
            .iter()
            .position(|item| object_path_hash(item).ok() == Some(hash))
        {
            merged.objects[index] = object;
        } else {
            merged.objects.push(object);
        }
    }
    write_raw_prop(&merged, output)?;
    println!(
        "bin-merge: base={} override={} -> merged={} 对象，deps={}",
        base_count,
        override_count,
        merged.objects.len(),
        merged.dependencies.len()
    );
    println!("OK 已写出 {output:?}");
    Ok(())
}

/// bin-dump：解析 bin → 美化 JSON 写出（属性引用链完整可见）。
fn bin_dump(input: &PathBuf, output: &PathBuf) -> Result<(), String> {
    use std::fs::File;
    use std::io::BufReader;

    use ltk_meta::Bin;

    let file = File::open(input).map_err(|e| format!("打开输入失败 {input:?}: {e}"))?;
    let bin = Bin::from_reader(&mut BufReader::new(file))
        .map_err(|e| format!("解析 bin 失败: {e}"))?;
    let json = serde_json::to_value(&bin).map_err(|e| format!("序列化失败: {e}"))?;
    let text = serde_json::to_string_pretty(&json).map_err(|e| format!("格式化失败: {e}"))?;
    std::fs::write(output, text).map_err(|e| format!("写出失败: {e}"))?;
    println!("OK 已写出 {output:?}");
    Ok(())
}

/// bin-alias-map：批量对象改名（ltk_meta）+ 属性 Hash 引用字节级替换。
///
/// bin 内对象引用全部是 Hash 值（u32，无字符串引用——已实证），所以：
/// 1) ltk_meta 改名对象表（old→new）
/// 2) 序列化后对字节做 u32 LE 替换（old→new）——对象表里 old 已不存在，
///    替换只命中属性区的引用值，零冲突
#[allow(dead_code)]
fn bin_alias_map(
    input: &PathBuf,
    output: &PathBuf,
    map: &PathBuf,
    string_prefixes: &[String],
) -> Result<(), String> {
    use std::collections::HashMap;
    use std::fs::File;
    use std::io::{BufReader, Write};

    use ltk_hash::BinHash;
    use ltk_meta::Bin;

    // 读取映射
    let map_text = std::fs::read_to_string(map).map_err(|e| format!("读映射失败: {e}"))?;
    let mut hash_map: HashMap<u32, u32> = HashMap::new();
    for (i, line) in map_text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.split_whitespace();
        let old = u32::from_str_radix(parts.next().ok_or_else(|| format!("映射第{i}行缺 old"))?, 16)
            .map_err(|e| format!("映射第{i}行 old 非法: {e}"))?;
        let new = u32::from_str_radix(parts.next().ok_or_else(|| format!("映射第{i}行缺 new"))?, 16)
            .map_err(|e| format!("映射第{i}行 new 非法: {e}"))?;
        hash_map.insert(old, new);
    }
    println!("bin-alias-map: 映射 {} 条", hash_map.len());

    // 解析 bin
    let file = File::open(input).map_err(|e| format!("打开输入失败 {input:?}: {e}"))?;
    let mut bin = Bin::from_reader(&mut BufReader::new(file))
        .map_err(|e| format!("解析 bin 失败: {e}"))?;

    // 1) 对象改名
    let mut renamed = 0usize;
    let objects = std::mem::take(&mut bin.objects);
    let mut new_objects = indexmap::IndexMap::new();
    for (hash, mut obj) in objects {
        if let Some(&new_hash) = hash_map.get(&hash.0) {
            obj.path_hash = BinHash(new_hash);
            new_objects.insert(BinHash(new_hash), obj);
            renamed += 1;
        } else {
            new_objects.insert(hash, obj);
        }
    }
    bin.objects = new_objects;

    // 2) 序列化到内存
    let mut bytes: Vec<u8> = Vec::new();
    {
        let mut cursor = std::io::Cursor::new(&mut bytes);
        bin.to_writer(&mut cursor).map_err(|e| format!("序列化失败: {e}"))?;
    }

    // 3) 字符串前缀替换（PROP String 值 = u16 LE 长度 + UTF-8）。
    //    只替换以 old 开头的字符串（如 Characters/Aatrox/Skins/Skin33 → Skin0，
    //    对象引用）；ASSETS 资源路径不以 Characters/ 开头，不受影响。
    //    要求等长（SkinN→Skin0 均为 5 字符）。
    let mut str_refs = 0usize;
    for pair in string_prefixes.chunks(2) {
        if pair.len() != 2 {
            continue;
        }
        let old = pair[0].as_bytes();
        let new = pair[1].as_bytes();
        if old.is_empty() || old.len() != new.len() {
            continue;
        }
        let mut i = 0usize;
        while i + 2 + old.len() <= bytes.len() {
            let len = u16::from_le_bytes([bytes[i], bytes[i + 1]]) as usize;
            if len >= old.len() && i + 2 + len <= bytes.len()
                && bytes[i + 2..i + 2 + old.len()] == *old
            {
                bytes[i + 2..i + 2 + old.len()].copy_from_slice(new);
                str_refs += 1;
            }
            i += 1;
        }
    }
    println!("bin-alias-map: 字符串引用替换 {str_refs} 处");

    // 4) 字节级 u32 LE 替换（属性区 Hash 引用）
    let mut refs = 0usize;
    for (old, new) in &hash_map {
        let old_bytes = old.to_le_bytes();
        let new_bytes = new.to_le_bytes();
        let mut i = 0;
        while i + 4 <= bytes.len() {
            if bytes[i..i + 4] == old_bytes {
                bytes[i..i + 4].copy_from_slice(&new_bytes);
                refs += 1;
                i += 4;
            } else {
                i += 1;
            }
        }
    }
    println!("bin-alias-map: 对象改名 {renamed} 个，引用替换 {refs} 处");

    // 写出
    let mut out = File::create(output).map_err(|e| format!("创建输出失败 {output:?}: {e}"))?;
    out.write_all(&bytes).map_err(|e| format!("写入失败: {e}"))?;
    println!("OK 已写出 {output:?}");
    Ok(())
}

/// bin-list：列出对象（path_hash, class_hash, 属性数），供 binhashes 表反查路径。
#[allow(dead_code)]
fn bin_list(input: &PathBuf) -> Result<(), String> {
    use std::fs::File;
    use std::io::BufReader;

    use ltk_meta::Bin;

    let file = File::open(input).map_err(|e| format!("打开输入失败 {input:?}: {e}"))?;
    let bin = Bin::from_reader(&mut BufReader::new(file))
        .map_err(|e| format!("解析 bin 失败: {e}"))?;
    println!("dependencies: {}", bin.dependencies.len());
    for d in &bin.dependencies {
        println!("  dep: {d}");
    }
    for (hash, obj) in bin.iter() {
        println!("object {:08x} class={:08x} props={}", hash.0, obj.class_hash.0,
                 obj.properties.len());
    }
    Ok(())
}

/// bin-alias：把对象的 path_hash 从 old_path 改为 new_path（内容不变）。
#[allow(dead_code)]
fn bin_alias(input: &PathBuf, output: &PathBuf, old_path: &str, new_path: &str) -> Result<(), String> {
    use std::fs::File;
    use std::io::BufReader;

    use ltk_hash::{BinHash, Hash};
    use ltk_meta::Bin;

    let old_hash = BinHash::hash_str(old_path);
    let new_hash = BinHash::hash_str(new_path);
    println!("bin-alias: {old_path} ({old_hash:x}) -> {new_path} ({new_hash:x})");

    let file = File::open(input).map_err(|e| format!("打开输入失败 {input:?}: {e}"))?;
    let mut bin = Bin::from_reader(&mut BufReader::new(file))
        .map_err(|e| format!("解析 bin 失败: {e}"))?;

    let obj = bin
        .remove_object(old_hash)
        .ok_or_else(|| format!("对象 {old_hash:x} ({old_path}) 不存在（共 {} 个对象）", bin.len()))?;
    println!("找到对象: class={:08x} properties={}", obj.class_hash, obj.properties.len());

    let mut renamed = obj;
    renamed.path_hash = new_hash;
    bin.add_object(renamed);

    let mut out = File::create(output).map_err(|e| format!("创建输出失败 {output:?}: {e}"))?;
    bin.to_writer(&mut out).map_err(|e| format!("写入 bin 失败: {e}"))?;
    println!("OK 已写出 {output:?}，对象数={}", bin.len());
    Ok(())
}

fn bin_edit_raw(
    input: &PathBuf,
    output: &PathBuf,
    map: &PathBuf,
    string_prefixes: &[String],
) -> Result<(), String> {
    let aliases = parse_alias_map(map)?;
    println!("bin-edit: 映射 {} 条", aliases.len());
    alias_raw_objects(input, output, &aliases, string_prefixes)
}

fn bin_alias_map_raw(
    input: &PathBuf,
    output: &PathBuf,
    map: &PathBuf,
    string_prefixes: &[String],
) -> Result<(), String> {
    let aliases = parse_alias_map(map)?;
    println!("bin-alias-map: 映射 {} 条", aliases.len());
    alias_raw_objects(input, output, &aliases, string_prefixes)
}

fn bin_list_raw(input: &PathBuf) -> Result<(), String> {
    let prop = parse_raw_prop(input)?;
    println!("dependencies: {}", prop.dependencies.len());
    for dep in &prop.dependencies {
        println!("  dep: {dep}");
    }
    for object in &prop.objects {
        let path_hash = object_path_hash(object)?;
        let property_count = u16::from_le_bytes([object.body[4], object.body[5]]);
        println!(
            "object {:08x} class={:08x} props={}",
            path_hash, object.class_hash, property_count
        );
    }
    Ok(())
}

fn bin_alias_raw(
    input: &PathBuf,
    output: &PathBuf,
    old_path: &str,
    new_path: &str,
) -> Result<(), String> {
    use ltk_hash::{BinHash, Hash};

    let old_hash = BinHash::hash_str(old_path);
    let new_hash = BinHash::hash_str(new_path);
    println!("bin-alias: {old_path} ({old_hash:x}) -> {new_path} ({new_hash:x})");

    let mut prop = parse_raw_prop(input)?;
    let index = prop
        .objects
        .iter()
        .position(|object| object_path_hash(object).ok() == Some(old_hash.0))
        .ok_or_else(|| {
            format!(
                "对象 {old_hash:x} ({old_path}) 不存在（共 {} 个对象）",
                prop.objects.len()
            )
        })?;
    let properties = u16::from_le_bytes([
        prop.objects[index].body[4],
        prop.objects[index].body[5],
    ]);
    println!(
        "找到对象: class={:08x} properties={}",
        prop.objects[index].class_hash, properties
    );
    prop.objects[index].body[0..4].copy_from_slice(&new_hash.0.to_le_bytes());
    write_raw_prop(&prop, output)?;
    println!("OK 已写出 {output:?}，对象数={}", prop.objects.len());
    Ok(())
}
