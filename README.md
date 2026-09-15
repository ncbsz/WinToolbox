# WinToolbox · Windows 系统工具箱

一个纯 Python（PySide6）实现的 Windows 桌面系统工具箱，浅色 / 深色 Fluent 风格，
打包为单文件绿色 EXE，双击即用，不写注册表、不装服务、不留后台。

## 功能

| 模块 | 功能 |
|---|---|
| 概览 | 系统信息、磁盘占用、CPU/内存/GPU/磁盘活动分色实时曲线、温度 |
| 系统优化 | 34 条数据驱动优化项（`rules.json`），两级导航、预读取当前状态、应用/还原/撤销 |
| 安全检测 | 只读扫描 Synaptics / XRed 感染型病毒（不执行、不修改、不删除） |
| 软件管理 | 已安装程序列表、搜索、卸载、残留目录扫描 |
| 启动与服务 | 启动项管理、服务启停与启动类型 |
| 内存与性能 | 实时占用、进程内存排行、工作集整理 |
| 网络修复 | 9 项诊断 + 10 项一键修复 |
| DNS 检测 | Ping 连通性测试 + DNS 解析速度测试（国内/国外/混合） |
| 垃圾清理 | 临时/缓存目录统计清理 + 回收站 |
| 策略诊断 | 检测并清除「由你的组织管理」的组策略残留 |
| 设置 | 备份/恢复、导入导出、偏好（开机自启/托盘/透明度）、浅色深色主题 |

## 安全设计

- 写入 `SOFTWARE\Policies` 的项标注 **【策略】** 并默认不勾选，应用前屏幕正中
  弹出安全确认框，逐条列出待改动项，需勾选「我已了解」才能确认。
- 每次应用前自动快照原值（journal），支持一键撤销；撤销采用递归删键，
  不存在「撤销了但没生效」的静默失败。
- 故意不提供「关闭 UAC」「关闭 Defender」这类危险项。

## 使用

双击 `WinToolbox.exe`，UAC 提权后进入主界面（启动即管理员模式）。

## 构建

```bat
cd WinToolbox
build.bat
```

依赖：Python 3.13 + PySide6-Essentials + PyInstaller（`--onefile --windowed`）。
输出 `dist/WinToolbox.exe`（约 40 MB，首次启动需解压，约 3~8 秒）。

## 源码结构

```
WinToolbox/
  app.py       PySide6 桌面界面（11 个页面 + 异步任务调度 + 安全确认对话框）
  server.py    系统操作服务层（注册表/服务/进程/网络/DNS/Ping/清理/病毒扫描）
  theme.py     浅色/深色 Fluent 样式表（QSS，支持 DPI 与窗口缩放）
  rules.json   34 条优化规则（数据驱动：check / optimize / restore 三段式）
  web/         浏览器版界面（本地 HTTP 模式）
  tools/       外部工具（iperf3 等）
  make_icon.py 纯标准库 ICO 编码器
  build.bat    一键重新打包
```

## 技术栈

Python 3.13 · PySide6 (Qt 6) · PyInstaller · PDH 性能计数器 · 原生 UDP DNS 查询
