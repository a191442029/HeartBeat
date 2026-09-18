# HeartBeat — BLE 心率监测系统（HRMLink / HRHub / HRBubble）

一套三端的 BLE 心率监测方案：通过 BLE 连接心率手环，实时显示心率（波形 + 悬浮窗），并支持多通道数据推送（MQTT / InfluxDB / 手机通知），可经 Tailscale 内网穿透跨网使用。

| 组件 | 平台 | 角色 |
|---|---|---|
| **HRMLink** | Windows 桌面端（PyQt5） | 服务端：BLE 采集 + 实时波形 + 桌面悬浮窗 + 数据服务/推送 |
| **HRHub** | 安卓服务端（Android 8.0+） | 服务端：1:1 复刻桌面端服务能力，直连手环采集并对外服务 |
| **HRBubble** | 安卓接收端（Android 8.0+） | 接收端：悬浮气泡实时显示服务端推送的心率 |

> 服务端二选一：用电脑跑 **HRMLink**，或用安卓盒子/平板长期通电跑 **HRHub**；**HRBubble** 悬浮气泡连接任一服务端显示心率。

## 功能特点

### 心率监测（桌面端 HRMLink）
- BLE 心率手环连接（支持收藏设备、自动连接、智能重连）
- 实时心率波形图（独立监测页）
- 心律不齐检测（波动阈值 / 跳变占比 / 静息上限可调）
- 桌面悬浮窗口（透明度 / 亮度 / 纯度可调，支持 OBS 捕获）

### 安卓服务端（HRHub，与桌面端能力对等）
- 直连 BLE 手环采集：可见扫描列表 + 收藏设备 + 智能重连（最后连接→收藏轮换，连续 3 轮失败自动停止）
- 数据服务：WebSocket 推送 + HTTP 轮询兜底，协议与桌面端完全一致，绑定 Tailscale IP（auto 自动探测）
- 告警引擎：心率上下限、持续判定、检测时段、告警冷却、心律不齐检测
- 多渠道推送：Bark（iOS）/ ntfy / MeoW（鸿蒙）；数据输出：InfluxDB / MQTT（Home Assistant 自动发现）
- 配置即运行：收藏/保存设备后立即自动连接，前台服务常驻（被系统杀死自动拉起）
- 心率悬浮窗（开关在设置页）；推送记录页查看最近 50 条告警

### 数据记录与分析（桌面端）
- 心率数据自动按天保存为 CSV（`log/heart_rate_YYYY-MM-DD.csv`）
- 内置统计分析与可视化脚本（见 [心率日志记录说明.md](心率日志记录说明.md)）

### 数据推送（桌面端）
- **MQTT**：发送到 Home Assistant 等智能家居平台，支持自动发现
- **InfluxDB**：时序数据库存储
- **多渠道手机通知**：MeoW（鸿蒙）/ Bark（iOS）/ ntfy
- 推送规则：心率超限（上限/下限）、检测时段、告警冷却、心律不齐告警

### 其他
- 敏感配置（桌面端 MQTT 密码 / InfluxDB Token）使用 Windows DPAPI 加密存储
- 桌面端系统托盘常驻、开机自启、自动检查更新

## 下载与运行

### 桌面端 HRMLink
**方法 1**（推荐）：源码运行 —— 下载 zip 解压后，根据需要修改 [`start.bat`](start.bat) 中的 Python 路径，双击运行即可
- 优点：随时可以检查与修改代码
- 缺点：运行环境需要自己配置

**方法 2**（推荐）：下载编译好的程序 —— 前往 [Releases 页面](https://github.com/a191442029/HeartBeat/releases/latest) 下载 `HRMLink.exe`，双击运行
- 优点：门槛低，单文件双击即可运行
- 缺点：相对不太透明，exe 体积较大

**方法 3**：自行编译 —— 安装 `pyinstaller` 后运行 `build_standalone.bat`，详见 [编译EXE指南.md](编译EXE指南.md)

如果运行出现依赖缺失错误，通过命令行手动安装（推荐 Python 3.13）：

```
pip install pyqt5 qasync bleak paho-mqtt influxdb-client aiohttp
pip install matplotlib numpy    # 数据分析/可视化脚本需要（可选）
```

### 安卓端 HRHub / HRBubble
从 [Releases 页面](https://github.com/a191442029/HeartBeat/releases/latest) 下载 APK 侧载安装（Android 8.0+）：
- `HRHub-1.0.0.apk` —— 安卓服务端
- `HRBubble-1.0.0.apk` —— 安卓接收端

## 典型组网

**方案 A：电脑做服务端**
1. 电脑与安卓设备安装并登录**同一个 Tailscale 账号**
2. 电脑端 HRMLink "Tailscale"页启用服务（地址填 `auto` 自动探测），端口默认 `8765`
3. 手机侧载 `HRBubble`，授予悬浮窗权限与电池白名单
4. 手机端填入电脑的 Tailscale IP + 端口，心率即以悬浮气泡实时显示

**方案 B：安卓设备做服务端（长期通电场景，替代电脑）**
1. 全部设备登录同一 Tailscale 账号
2. 安卓设备侧载 `HRHub`，授予定位（BLE 扫描）、悬浮窗权限与电池白名单
3. 设置页扫描并收藏手环（保存后立即自动连接），数据服务默认开启（地址 `auto`、端口 `8765`）
4. 其他设备的 `HRBubble` 填入该安卓设备的 Tailscale IP + 端口即可接收

## Q&A

#### Q: 我的设备支持蓝牙且已经打开蓝牙，运行程序时却扫描不到任何设备？

- **A:** 请检查手环是否开启心跳广播。如果开启后仍然找不到，可以先进入系统的蓝牙设置页面，再回到本程序点击设备处的刷新按钮。

#### Q: 我要如何卸载 HRMLink？

- **A:** 程序只在 exe 所在目录写入配置与日志文件。卸载时先`关闭开机自启`，然后：
  - 直接删除 `HRMLink.exe` 所在文件夹即可；
  - 或只删除其中的 `HRMLink.exe`、`config.ini` 和 `log` 文件夹（包含个人设置、运行日志和心率数据）。

#### Q: 如何将心率数据发送到 Home Assistant？

- **A:** 在"MQTT"设置页启用 MQTT 并配置服务器地址，勾选"启用 Home Assistant 自动发现"，心率传感器会自动出现在 Home Assistant 中。需要认证时填写正确的用户名和密码。安卓服务端 HRHub 同样支持：在设置页 MQTT 分组配置即可。

#### Q: 心率数据保存在哪里？如何查看历史数据？

- **A:** 桌面端保存在 `log` 目录下的 CSV 文件（`heart_rate_YYYY-MM-DD.csv`），可用 Excel/WPS 打开，也可使用项目自带的分析脚本。详见 [心率日志记录说明.md](心率日志记录说明.md)。

## 相关文档

- [架构文档.md](架构文档.md) —— 系统架构、模块说明、配置项
- [编译EXE指南.md](编译EXE指南.md) —— 从源码编译 Windows 程序
- [心率日志记录说明.md](心率日志记录说明.md) —— 心率日志与分析工具
- [安卓接收端方案.md](安卓接收端方案.md) —— Tailscale + 安卓悬浮窗方案设计

## License

本项目基于 [GPL-3.0](LICENSE) 协议开源。
