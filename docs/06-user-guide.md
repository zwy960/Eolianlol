# 06 · 个人使用手册（快速换肤）

> 仿照架构的快速实现：LCU 选择 + 覆盖 WAD + 官方补丁器注入。
> 只支持"普通皮肤"整体替换（skinN.bin → skin0），特殊皮肤（元素女皇、
> Viego 武器等）不做适配。

## 0. 环境要求

| 项 | 要求 |
|---|---|
| 游戏 | 国服客户端 + 完整游戏文件（本机 `E:\Program Files (x86)\英雄联盟(26)`） |
| Python | 3.11+，`pip install zstandard`（解压 zstd chunk） |
| 构建器 | `eolian-overlay/target/release/eolian-overlay.exe`（`cd eolian-overlay && cargo build --release`，需要 Rust + MinGW，见下） |
| 补丁器 | 默认 `vendor/cslol/mod-tools.exe` + `cslol-dll.dll`；LTK patcher 保留为历史回退 |

Rust 构建环境（一次性）：
```
rustup toolchain install stable-x86_64-pc-windows-gnu
# 需要 mingw-w64 工具链（gcc/ar/dlltool）：本机用 C:\w64devkit（w64devkit\bin 加入 PATH）
```

## 1. 快速开始

```bash
# 命令行换肤（推荐先验证）
python -m tools.skin_swap ahri 7                # 阿狸 → 皮肤7
python -m tools.skin_swap --build-only ahri 7   # 只构建（不注入，先验证构建链）

# 悬浮窗模式（选人阶段自动跟随英雄）
python -m tools.floater

# 端到端构建验证（无需游戏运行）
python -m tools.verify_build ahri 1
```

## 2. 使用流程（实战）

1. 启动英雄联盟客户端，进入**英雄选择阶段**；
2. 运行 `python -m tools.skin_swap`（自动读取当前英雄与皮肤）或悬浮窗；
3. 选择目标皮肤 → 工具**立即构建覆盖 WAD**（选人阶段越早越好）；
4. 构建完成后补丁器启动，**等待游戏进程**（进入对局读秒时游戏进程出现，
   补丁器自动注入）；
5. 进游戏后英雄即显示目标皮肤；工具同时 PATCH LCU 让客户端 UI/语音同步。

## 3. 原理（一分钟版）

```
选皮肤 → 提取 skinN.bin（游戏 WAD 中所有皮肤都存在，含未拥有的）
       → 生成 mod：把 skinN.bin 作为 data/.../skins/skin0.bin 的覆盖
       → eolian-overlay（ltk_overlay 引擎）构建覆盖 WAD
       → ltk_patcher_host 注入游戏：CreateFileA 重定向
       → 游戏加载 skin0 时实际读到 skinN 的完整配置（模型/纹理/粒子/音效）
```

## 4. 已知限制

- **特殊皮肤**（多形态/专属机制）：可能显示异常，不做适配；
- **首次进游戏前必须完成构建**（进游戏后构建无意义）；
- 换肤在**训练模式/自定义**验证最安全；
- 反作弊（ACE）风险：注入类工具可能被判定异常，**自担风险**；
- 覆盖 WAD 与 LCU 选择的皮肤不一致时（如只 PATCH 不注入），客户端预览
  与游戏内可能不一致——正常使用时两者同步执行。

## 5. 排错

| 现象 | 处理 |
|---|---|
| `LCU 请求失败` | 客户端未启动或 lockfile 过期；先启动客户端 |
| `overlay 构建失败` | 查看 eolian-overlay 输出；确认游戏文件完整 |
| 补丁器停在 `scanning for game` | 正常——等游戏进程出现；游戏需与补丁器同权限（管理员） |
| 游戏内没有换肤 | 确认注入时游戏已启动；确认覆盖 WAD 存在（verify_build） |
| `[WinError 32] 另一个程序正在使用此文件` | Windows 目录句柄占用（资源管理器窗口 / 实时防护正在扫刚落盘的 WAD / 索引或同步服务）。现在会自动退避重试，重试不过就改用独立副本目录 `eolian-overlay-current-<毫秒>-<pid>` 继续跑，不再中断换肤；日志里会打 `[B6] 目录被占用，退避重试` 或 `[COPY] ASCII 目录被占用，改用独立副本目录`，两者都不是失败 |
| 想确认到底是哪个目录/文件被占 | `python -m eolian.winlock C:\eolian-overlay-current`（`busy=True` 会指出是目录句柄还是某个 WAD 被独占）；要指名进程用 `handle64.exe <路径>` |
| 想复现/回归验证占用容错 | `python tools/verify_overlay_sync.py --champion aatrox --skin 11`（只构建不注入；模拟"目录被占"和"单文件被独占"两种场景，全部 PASS 即正常） |

## 6. ASCII 副本与占用容错

DLL 侧对中文路径兼容性差，所以覆盖目录会先同步到 `C:\eolian-overlay-current`
（配置项 `overlay_ascii_dir`，可用 `EOLIAN_OVERLAY_ASCII_DIR` 覆盖）。

同步方式是**逐文件 `os.replace`**，不是"复制到临时目录再改目录名"——后者在
Windows 上遇到任何不带 `FILE_SHARE_DELETE` 的目录句柄就抛 WinError 32，而那时
临时目录已经写完，前面几十秒全白干。现在的行为：

- 同一套皮肤的覆盖目录内容一致时直接跳过复制；
- 固定目录被占 → 逐文件替换照常进行（文件级替换只被"同一个文件的独占句柄"挡住）；
- 某个 WAD 被独占 → 6 次指数退避（约 6 秒）后自动改用独立副本目录，流程继续；
- 上一套皮肤的残留 WAD、旧版留下的 `.staging-*` / `.backup`、历史独立副本目录
  会在下次同步时清掉（残留 WAD 必须删，否则旧皮肤会被继续重定向）。
