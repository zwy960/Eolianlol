# Eolian

Eolian 是一个用于个人学习和研究的《英雄联盟》换肤工具原型，提供自动路径探测、
皮肤目录、覆盖 WAD 构建、补丁器注入和一个 WebView2 驱动的现代 Web 界面。

> 本项目与 Riot Games、腾讯无关联。修改游戏客户端可能违反服务条款并存在账号风险，
> 请仅在训练模式或自定义模式中使用。

## 功能

- 自动探测国服 WeGame 和 Riot 客户端安装目录，读取 lockfile 建立本地 API 连接
- LCU 游戏流程状态轮询（大厅 / 英雄选择 / 游戏中），英雄选择阶段自动就绪
- 皮肤目录加载、中文名称显示与缓存
- `skinN.bin -> skin0.bin` 自包含皮肤入口生成，技能管理器、粒子和关联 VFX 依赖一并处理
- Rust 覆盖 WAD 构建器（`eolian-overlay/`），原子构建，失败时保留上一份可用版本
- cslol 补丁器注入（默认），保留 LTK patcher 作为历史回退
- **Web 界面**：pywebview + Edge WebView2 渲染的深色毛玻璃主题悬浮窗
  （实时皮肤卡片、日志增量拉取、换肤注入状态与"取消本次构建"）；
  pywebview 不可用时自动回退旧 Tkinter 界面
- 打包为 Windows onedir EXE（PyInstaller），内置 Web 前端与补丁器运行时

## 工作原理

不改游戏文件、不改内存对象，而是让游戏加载"假 Skin0"：

1. **构建层** —— 读取目标皮肤 SkinN 的全部资源，构建覆盖 WAD，
   将 `skin0.bin` 等条目改写为指向 SkinN 的资源；
2. **注入层** —— 补丁器在游戏进程内挂钩 `CreateFileA`，
   把游戏对 `*.wad.client` 的打开操作重定向到覆盖 WAD；
3. **选择层** —— 通过 LCU 让客户端"以为自己选了 SkinN"，
   客户端 UI / 语音 / 加载框随之变为目标皮肤。

技术细节见 `docs/`（架构、LCU 协议、WAD 格式、补丁器协议、哈希表格式、打包等）。

## 快速开始

需要 Python 3.11+（建议 venv），Windows 系统：

```powershell
# 安装依赖
pip install -e .[extras]
pip install pywebview

# 一键自检：权限、路径、客户端、构建器、补丁器
python -m tools.doctor

# 启动悬浮窗（优先 Web UI，回退 Tkinter）
python -m tools.floater
```

也可以直接运行打包版：

```text
dist\Eolian\Eolian.exe
```

配置和日志位于 `%LOCALAPPDATA%\Eolian`；`config.example.json` 是最小配置示例。

## 目录

```text
Eolian/
├── eolian/              # Python 核心库（LCU、皮肤目录、覆盖构建、补丁器驱动）
│   ├── ui/              # Web UI（web/ 前端 + web_window.py 桥接）与旧 Tk 悬浮窗
│   └── safe/            # 安全自检工具（进程/PE 校验、报告，`eolian-safe` 命令）
├── eolian-overlay/      # Rust 覆盖 WAD 构建器
├── tools/               # 命令行工具（doctor、lcu_probe、live_swap、wad_inspect 等）
├── tests/               # 单元测试
├── data/                # 皮肤目录样本和哈希表（data/hashes/ 不入库，可再下载）
├── vendor/              # cslol / LTK 补丁器运行时（二进制不入库，从上游获取）
├── resources/           # 打包内置的覆盖构建器（编译产物，不入库；
│                          由 eolian-overlay/ `cargo build --release` 生成后放入）
├── packaging/           # PyInstaller 配置与构建脚本
└── docs/                # 架构、协议、使用与打包文档
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 打包

```powershell
.\packaging\build.ps1
```

输出（onedir）：

```text
dist\Eolian\Eolian.exe
dist\Eolian-portable.zip
dist\USAGE.txt
```

## 归属与许可

- Eolian 自有代码：MIT，见 [LICENSE](LICENSE)，仅用于个人学习和研究。
- `vendor/` 下的补丁器二进制**不随仓库分发**，请从各自上游项目获取：
  cslol（`mod-tools.exe` / `cslol-dll.dll`，许可证见 `vendor/cslol/CSLOL-LICENSE.txt`）
  与 LTK patcher（许可证见 `vendor/LTK-PATCHER-LICENSE.md`）。
  这些组件归各自项目所有，不受本仓库 MIT 许可证覆盖。
- `data/` 中的游戏目录数据仅作为学习样本。
- 技术实现参考 LeagueToolkit 的 ltk-manager、league-mod、wadtools 和 cslol-manager。
