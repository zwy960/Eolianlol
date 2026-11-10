# 10 · Windows EXE 打包

## 用户使用

双击：

```text
dist\Eolian.exe
```

首次启动会请求 UAC 管理员权限。程序自动探测 WeGame / Riot 客户端目录，
常见安装环境下不需要设置路径。

如果自动探测失败，只在首次运行时弹出一次目录选择框。配置保存到：

```text
%LOCALAPPDATA%\Eolian\config.json
```

日志、缓存和构建结果：

```text
%LOCALAPPDATA%\Eolian\session
```

## 打包产物

```text
dist\Eolian.exe
dist\USAGE.txt
dist\Eolian-portable.zip
```

单文件 exe 内置以下内容：

- `resources\eolian-overlay.exe`
- `vendor\cslol`
- `data\hashes\binhashes-*.lhdb`
- `data\skins.json`
- `data\champion-summary.json`

## 开发构建

```powershell
cd C:\Users\Administrator\Desktop\lolhf\Eolian
.\packaging\build.ps1
```

构建前会结束正在运行的 `Eolian` 进程，避免 exe 被占用。

## 注意事项

- 当前 exe 没有代码签名，Windows SmartScreen 可能提示未知发布者。
- 单文件模式首次启动需要解压内部资源，启动会比后续慢。
- 客户端目录自动探测失败时才需要人工选择。
- 修改代码后重新运行 `packaging\build.ps1` 即可生成新版。
