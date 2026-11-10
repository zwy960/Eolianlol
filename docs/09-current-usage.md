# 09 · 当前环境使用说明

本文按当前机器实测环境编写：

- 项目目录：`C:\Users\Administrator\Desktop\lolhf\Eolian`
- Python：`C:\Users\Administrator\Desktop\lolhf\.venv\Scripts\python.exe`
- 国服客户端：`E:\WeGameApps\英雄联盟`
- 游戏目录：`E:\WeGameApps\英雄联盟\Game`
- 覆盖构建器：`C:\eolian-target\release\eolian-overlay.exe`
- 默认补丁器：`vendor\cslol\mod-tools.exe` + `cslol-dll.dll`

以下命令都在 `Eolian` 目录执行。

## 1. 首次检查

```powershell
# 一键检查权限、路径、客户端、构建器和补丁器
..\.venv\Scripts\python.exe -m tools.doctor

# 确认客户端在线并能读到 LCU 状态
..\.venv\Scripts\python.exe -m tools.lcu_probe

# 确认构建器可运行
C:\eolian-target\release\eolian-overlay.exe --help

# 确认哈希表存在
Get-ChildItem data\hashes\binhashes-*.lhdb
```

`lcu_probe` 能显示 `游戏阶段: Lobby` 或 `ChampSelect` 就表示连接正常。

悬浮窗不是管理员启动时会自动弹出 UAC 并重新启动。设置
`EOLIAN_NO_ELEVATE=1` 可以关闭自动提权。

如果构建器不存在：

```powershell
cd eolian-overlay
cargo build --release
cd ..
```

项目内的 `.cargo/config.toml` 会把构建产物输出到
`C:\eolian-target\release\eolian-overlay.exe`。

## 2. 推荐流程：悬浮窗

1. 启动英雄联盟客户端并进入英雄选择。
2. 执行：

```powershell
..\.venv\Scripts\python.exe -m tools.floater
```

3. 悬浮窗自动跟随当前英雄，选择皮肤后点“换肤”。
4. 终端必须已有管理员权限；cslol 需要与游戏进程同权限。
5. 进入对局后生效。本局结束后工具停止，下一局需要重新选择。

## 3. 命令行流程

先只构建，不改客户端选择：

```powershell
..\.venv\Scripts\python.exe -m tools.skin_swap ahri 1 --build-only
```

确认构建成功后，在进入对局前执行完整流程：

```powershell
..\.venv\Scripts\python.exe -m tools.skin_swap ahri 1
```

- `ahri` 是英雄别名，大小写不敏感。
- `1` 是皮肤编号 `skinNum`，不是皮肤列表里的行号。
- 不传参数时，程序会尝试从选人阶段自动读取英雄和当前皮肤。
- 运行后保持窗口打开；按 `Ctrl+C` 可停止补丁器。

## 4. 构建验证

```powershell
..\.venv\Scripts\python.exe -m tools.verify_build ahri 1
```

最后看到 `[OK] 端到端构建链路验证通过` 即表示：

- 已从游戏 WAD 提取目标皮肤；
- 已生成 `skin0` 别名覆盖；
- 覆盖 WAD 已成功构建；
- 覆盖里的 `skin0.bin` 可以被重新解析。

生成的中间文件在 `session\mods`、`session\overlays` 和 `session\state`。

## 5. 自定义路径

推荐复制 `config.example.json` 为 `config.json`，按实际安装位置修改：

```json
{
  "client_dir": "E:\\WeGameApps\\英雄联盟",
  "game_dir": "E:\\WeGameApps\\英雄联盟\\Game",
  "session_dir": "session",
  "learn_overlay": "C:\\eolian-target\\release\\eolian-overlay.exe",
  "overlay_ascii_dir": "C:\\eolian-overlay-current"
}
```

也可以临时用环境变量覆盖，环境变量优先级高于 `config.json`：

```powershell
$env:EOLIAN_CLIENT_DIR = 'D:\你的客户端目录'
$env:EOLIAN_GAME_DIR = 'D:\你的客户端目录\Game'
```

哈希表路径也可以单独指定：

```powershell
$env:EOLIAN_BINHASHES = 'D:\hashes\binhashes-YYYY-MM-DD.lhdb'
```

游戏目录由 `eolian\overlay.py` 的 `DEFAULT_GAME_DIR` 指定，默认值为
`E:\WeGameApps\英雄联盟\Game`。

## 6. 常见问题

| 现象 | 处理方法 |
|---|---|
| 找不到客户端 | 先启动客户端；或设置 `EOLIAN_LEARN_CLIENT_DIR` |
| 缺少 binhashes 哈希表 | 从 LeagueToolkit/Mimir Releases 下载 `binhashes-YYYY-MM-DD.lhdb`，放进 `data\hashes` |
| 构建器版本旧 | 在 `eolian-overlay` 执行 `cargo build --release` |
| 补丁器一直扫描 | 这是正常等待；进入对局、游戏进程出现后才会注入 |
| 日志出现 `OpenProcess` 或权限错误 | 关闭 PowerShell，右键“以管理员身份运行”后重新启动工具 |
| 注入失败 | 游戏和补丁器都要有管理员权限；查看 `session\diag.log` |
| 特殊皮肤显示异常 | 当前只保证普通皮肤的整皮替换 |

## 7. 风险

覆盖 WAD 和补丁器注入属于修改游戏运行行为，可能触发反作弊或违反服务条款。
建议只在训练模式/自定义模式验证，并自行承担账号风险。

## 8. 补丁器后端

默认使用 `vendor\cslol`。这套组件已在本机实际 hook 当前国服客户端，
日志中应出现：

```text
Status: Found League
[DLL] info: Init in process!
[DLL] info: redirected wad: ...
```

LTK patcher 二进制仍保留在 `vendor` 和 `vendor\archive` 作为历史参考，
但当前国服客户端会在 LTK DLL 的完整性钩子阶段报
`ossl: .rdata section not found`，因此不再作为默认后端。

## 9. 构建可靠性

覆盖构建采用临时目录：

1. 先在 `session\mods\.<tag>-<timestamp>` 生成 mod；
2. 再在 `session\overlays\.<tag>-<timestamp>` 构建 WAD；
3. 全部成功后原子替换正式目录；
4. 任意步骤失败时保留上一份可用覆盖，不会留下半成品。

ASCII 覆盖副本同样先写 staging 目录，成功后再替换
`overlay_ascii_dir`。
