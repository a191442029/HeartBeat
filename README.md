# HRMLink — BLE 心率监测桌面端 + 安卓悬浮窗接收端

基于 Python (PyQt5) 的蓝牙心率监测工具：通过 BLE 连接心率手环，实时显示心率（波形页 + 桌面浮动窗口），并支持多通道数据推送（MQTT / InfluxDB / 手机通知），可通过 Tailscale 内网穿透将心率推送到安卓手机悬浮气泡显示。

## 功能特点

### 心率监测
- BLE 心率手环连接（支持收藏设备、自动连接、智能重连）
- 实时心率波形图（独立监测页）
- 心律不齐检测（波动阈值 / 跳变占比 / 静息上限可调）
- 桌面悬浮窗口（透明度 / 亮度 / 纯度可调，支持 OBS 捕获）

### 数据记录与分析
- 心率数据自动按天保存为 CSV（`log/heart_rate_YYYY-MM-DD.csv`）
- 内置统计分析与可视化脚本（见 [心率日志记录说明.md](心率日志记录说明.md)）

### 数据推送
- **MQTT**：发送到 Home Assistant 等智能家居平台，支持自动发现
- **InfluxDB**：时序数据库存储
- **多渠道手机通知**：MeoW（鸿蒙）/ Bark（iOS）/ ntfy
- 推送规则：心率超限（上限/下限）、检测时段、告警冷却、心律不齐告警

### Tailscale 跨网推送 + 安卓接收端
- 桌面端内置 aiohttp 服务（WebSocket 推送 + HTTP 轮询兜底），绑定 Tailscale IP
- 安卓接收端 **HRBubble**（Android 8.0+）：悬浮气泡显示心率，可拖动、点击调整字号、位置记忆
- 详见 [安卓接收端方案.md](安卓接收端方案.md)

### 其他
- 敏感配置（MQTT 密码 / InfluxDB Token）使用 Windows DPAPI 加密存储
- 系统托盘常驻、开机自启、自动检查更新

## 运行程序

**方法 1**（推荐）：源码运行 —— 下载 zip 解压后，根据需要修改 [`start.bat`](start.bat) 中的 Python 路径，双击运行即可
- 优点：随时可以检查与修改代码
- 缺点：运行环境需要自己配置

**方法 2**（推荐）：下载编译好的程序 —— 前往 [Releases 页面](https://github.com/a191442029/HeartBeat/releases/latest) 下载 `HRMLink.exe`，双击运行
- 优点：门槛低，单文件双击即可运行
- 缺点：相对不太透明，exe 体积较大

**方法 3**：自行编译 —— 安装 `pyinstaller` 后运行 `build_standalone.bat`，详见 [编译EXE指南.md](编译EXE指南.md)
- 优点：可以自行编译最新版本
- 缺点：门槛较高，可能因系统环境不同遇到问题

如果运行出现依赖缺失错误，通过命令行手动安装（推荐 Python 3.13）：

```
pip install pyqt5 qasync bleak paho-mqtt influxdb-client aiohttp
pip install matplotlib numpy    # 数据分析/可视化脚本需要（可选）
```

## 安卓接收端

1. 电脑端与手机安装并登录**同一个 Tailscale 账号**
2. 电脑端"Tailscale"页启用服务（地址填 `auto` 即可自动探测），端口默认 `8765`
3. 手机侧载安装 `HRBubble` APK（从 [Releases](https://github.com/a191442029/HeartBeat/releases/latest) 下载），授予悬浮窗权限与电池白名单
4. 手机端填入电脑的 Tailscale IP + 端口，心率即以悬浮气泡实时显示

## Q&A

#### Q: 我的设备支持蓝牙且已经打开蓝牙，运行程序时却扫描不到任何设备？

- **A:** 请检查手环是否开启心跳广播。如果开启后仍然找不到，可以先进入系统的蓝牙设置页面，再回到本程序点击设备处的刷新按钮。

#### Q: 我要如何卸载 HRMLink？

- **A:** 程序只在 exe 所在目录写入配置与日志文件。卸载时先`关闭开机自启`，然后：
  - 直接删除 `HRMLink.exe` 所在文件夹即可；
  - 或只删除其中的 `HRMLink.exe`、`config.ini` 和 `log` 文件夹（包含个人设置、运行日志和心率数据）。

#### Q: 如何将心率数据发送到 Home Assistant？

- **A:** 在"MQTT"设置页启用 MQTT 并配置服务器地址，勾选"启用 Home Assistant 自动发现"，心率传感器会自动出现在 Home Assistant 中。需要认证时填写正确的用户名和密码。

#### Q: 心率数据保存在哪里？如何查看历史数据？

- **A:** 保存在 `log` 目录下的 CSV 文件（`heart_rate_YYYY-MM-DD.csv`），可用 Excel/WPS 打开，也可使用项目自带的分析脚本。详见 [心率日志记录说明.md](心率日志记录说明.md)。

## 相关文档

- [架构文档.md](架构文档.md) —— 系统架构、模块说明、配置项
- [编译EXE指南.md](编译EXE指南.md) —— 从源码编译 Windows 程序
- [心率日志记录说明.md](心率日志记录说明.md) —— 心率日志与分析工具
- [安卓接收端方案.md](安卓接收端方案.md) —— Tailscale + 安卓悬浮窗方案设计

## License

本项目基于 [GPL-3.0](LICENSE) 协议开源。
