# 11 · safe 模块（防封守护）

> 目标不是"更隐蔽地作弊"，而是把**换肤这件事的可见面**压到最小：
> 不改判定、不写游戏目录、不碰反作弊、失败即放弃。
> 一句话原则：**宁可这局没皮肤，也不要带着可疑状态进对局。**

代码位置：`eolian/safe/`，独立包，不依赖第三方库；
唯一侵入点是 `eolian/patcher.py::create_patcher()`（注入前统一过门禁）。

```bash
python -m eolian.safe audit            # 全量体检（不动任何东西）
python -m eolian.safe gate             # 只跑门禁；被拦截退出码 2
python -m eolian.safe policy --add-build   # 把当前游戏 build 指纹登记进白名单
python -m eolian.safe clean --apply    # 清理本工具留下的痕迹
python -m eolian.safe status --json    # 给 UI 读的状态
```

策略文件：`%LOCALAPPDATA%\Eolian\safe.json`（三档 `off / balanced / strict`）。

## 1. 每一道"防封"落在哪

| 手段 | 实现 | 失败时 |
|---|---|---|
| 版本变了就不再打补丁（偏移硬编码的根治） | `version.read_build()` 用 PE 头算 build 指纹；`known_builds` 白名单 | **block**（VER004/VER006） |
| 补丁器"过期即静默失效"要提前发现 | `version.check_patcher_eol()` 扫 DLL 内嵌 `CSLOL_EOL_TIMESTAMP` | 过期 block，临期 warn |
| 游戏是管理员、工具不是 → 注入必然半失败 | `anticheat.probe()` + `procinfo.integrity_level()` | **block**（ENV003） |
| 游戏被调试器附加 | `procinfo.remote_debugger_present()` | **block**（ENV006） |
| 上一会话没退干净（hook 还挂着） | `anticheat` 查游戏进程模块表里的 `cslol-dll.dll` 等 | warn（ENV007） |
| 往客户端塞新文件 | `overlay_verify` 比对 chunk 哈希集合，`extra > 0` | **block**（OV006） |
| 改动面过大（不像原版） | 被替换 chunk 比例 > `max_replaced_chunk_ratio` | **block**（OV008） |
| skin0 引用了不存在的资源 / 跨英雄引用 | `overlay_verify._skin0_compare()` 解析 PROP 依赖并回查 chunk 集合 | **block**（OV010） |
| 覆盖目录里夹带 dll/exe | `strict_overlay_paths` | **block**（OV003） |
| IFEO `Debugger` 劫持 | `footprint.scan_ifeo()` | **block**（FP001） |
| 兼容层里出现 rundll32 / 第三方目录 | `footprint.scan_layers()` | warn（FP003） |
| 游戏目录被塞侧载 DLL（`version.dll` / `d3d9.dll` …） | `footprint.scan_game_dir()` | **block**（FP005） |
| 备份残留（`*.bak` / `*.yoousn-backup-*`） | 同上 | warn（FP007） |
| 残留宿主进程 | `footprint.scan_stray_hosts()` | warn / strict 下 block |
| 崩溃后清理没做完 | journal + `recover()` | warn（FP011） |
| 日志把基址/偏移写进磁盘 | `redact.sanitize_file()`（收尾时执行） | 失败仅告警 |

## 2. 为什么这样设计

1. **fail-closed**：所有检查的默认值都朝"拒绝"取。策略写坏（JSON 损坏）时回退 `balanced`，
   而不是"关闭检查"。
2. **只在单点拦**：`create_patcher()` 是所有注入路径（`overlay.apply_overlay`、
   `tools/skin_swap.py`、`ui/web_window.py`）的必经之路，拦这里不会漏。
3. **观测与裁决分离**：`preflight()` 只产出 `Report`；`gate()` 才按策略抛 `SafeRefusal`。
   `mode=off` 时所有 block 自动降级为 warn，方便"先看看到底哪里不对"。
4. **配置与代码解耦**：`safe.json` 里只放阈值和 build 白名单，不放开关逻辑；
   预设写死在 `policy.PRESETS`，避免用户一改就变危险。

## 3. 明确不做的事

- 不结束/停用/绕过反作弊（ACE、Vanguard）进程与服务；
- 不摘 PEB 模块链、不隐藏注入模块、不抹 PE 头（"更隐蔽"不是目标，副作用更大）；
- 不改游戏目录里的任何文件；
- 不写 IFEO / 兼容层注册表（只检测别人留下的并报告）；
- 不代打偏移：`CRYPTO_free` 这类偏移仍然由补丁器决定，`safe` 只负责"版本不对就别打"。

## 4. Report 分级

| 级别 | 含义 | balanced | strict |
|---|---|---|---|
| `ok` | 通过 / 已执行的动作 | 放行 | 放行 |
| `info` | 事实记录 | 放行 | 放行 |
| `warn` | 可疑但不致命 | 放行（打日志） | **升级为 block** |
| `block` | 硬失败 | 拒绝注入 | 拒绝注入 |

## 5. 接入点（谁在跑门禁）

| 位置 | 行为 |
|---|---|
| `eolian/patcher.py::create_patcher()` | 构造前先 `_safe_gate()`，被拒抛 `PatcherError` |
| `PatcherHost.start()` / `CslolPatcher.start()` | 注入点本身再查一次（幂等），直连脚本也拦得住 |
| `eolian/ui/web_window.py（换肤编排）` | 构建完成后、ASCII 复制**之前**跑门禁：通过 → 「安全检查通过（N 条告警）」；拦截 → 「防封门禁拦截：CODE 原因」，面板显示「注入：已拦截」，直接 return |
| 命令行 | `eolian-safe` / `python -m eolian.safe` |

**幂等**：同一覆盖目录在一个进程里只体检一次（`is_guarded()`）；ASCII 副本与源目录字节一致，
复制完成后用 `mark_guarded()` 跳过重复体检。单次体检约 130 ms（含读原版 WAD 的 TOC）。

## 6. 已知不可控风险（记录，不试图对抗）

这些不是靠改代码能消除的，写在这里是为了不让"防封"变成心理安慰。

| 风险 | 证据 | 现状 |
|---|---|---|
| 反作弊走**原始卷读取**，绕过 `CreateFileA` 的 IAT hook | 实测日志：`ACE-DFS64.dll` + `\\.\C:` `\\.\D:` `\\.\E:` 卷句柄 + 随机名 `.nhn` | 无法拦截；只能靠"chunk 集合不越界 + TOC 哈希还原 + 只动 skin0"把差异压到最小 |
| 注入模块的路径与名字可见 | 游戏模块表里会有 `cslol-dll.dll`（onedir 后路径固定为 `_internal\vendor\cslol\`） | 已改 onedir 去掉 `%TEMP%\_MEIxxxx` 随机目录；名字本身不改（属既定特征） |
| IAT / 代码补丁可被静态比对 | `kernel32!CreateFileA` 槽位被改写；`CRYPTO_free` 处被补丁 | 只能保持"只在基础皮肤下生效、失败即放弃" |
| 选人状态是服务端可见的 | LCU `selectedSkinId` 会同步给服务端 | 已默认关闭（`lcu_patch_skin=false`），只做本地 overlay |
| 未上线/跨区皮肤差异更明显 | 覆盖 WAD 里引用目标皮肤资源 | 不使用未上线皮肤；`overlay_verify` 挡越界引用 |

底线：这些风险只改善、不消除。真要零风险，只能是"不注入"。
