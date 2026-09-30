# HRMLink（PC 端 EXE）功能业务逻辑梳理

> 梳理日期：2026-09-29 ｜ 当前版本：v1.2.16.0 ｜ 技术栈：Python 3.13 + PyQt5 + qasync（Qt/asyncio 融合事件循环）+ bleak（BLE）+ aiohttp
> 打包：PyInstaller（HRMLink.spec），产物 `dist/HRMLink_vX.Y.Z.0.exe`

---

## 1. 项目定位

HRMLink 是"HeartBeat 心率监护系统"的 PC 中枢端：

- 通过 **BLE 直连**或 **ESP32 全屋中继**获取手环心率；
- 本地展示波形/日志，并分发到 **MQTT（Home Assistant）、InfluxDB、CSV 日志**；
- 心率异常/设备断连时进行**多渠道手机推送（MeoW/Bark/ntfy）+ 本地报警音 + 小爱音箱播报**；
- 联动**摄像头**：报警时剪辑报警前 10 秒视频、推送 HLS 实时画面；
- 通过 **Tailscale 数据服务（WS 推送 + HTTP 轮询）**把心率/报警/视频实时推给安卓接收端（HRBubble）。

## 2. 启动流程（[__main__.py](file:///d:/vibecoding/HeartBeat/__main__.py)）

1. 固定工作目录为 EXE 所在目录（防止开机自启时 CWD=System32 导致配置/日志写错位置）。
2. 导入 `system_utils_ascii`（ASCII 版系统工具，规避编码问题），同步版本号 `VER2=(1,2,16,0)`。
3. 解析命令行参数：`-updatemode`（更新模式）、`-endup`（更新收尾）、`-startup`（自启自测）、`-start_`（开机启动，延迟 10 秒）。
4. `check_run()` 单实例检查；frozen 状态下处理更新模式。
5. 非 frozen 时写 `version.json`（更新元数据，供在线更新检查对比）。
6. `init_config()` 初始化/补齐 `config.ini`。
7. 依赖自动补装：pyqt5、qasync、bleak、paho-mqtt、aiohttp（`pip_install_models`），并输出依赖许可清单日志。
8. 创建 `QApplication` + `qasync.QEventLoop`，注册 AppUserModelID（任务栏图标）、全局异常钩子（弹 `verylarge_error` 错误窗），进入主窗口事件循环。

## 3. 总体架构与数据流

```
手环(BLE)
   │                    ┌────────────────────────────┐
   ├─ PC直连(bleak)─────▶│  DeviceConnectionUI(DevCtrl)│
   │                    │  BLEHeartRateMonitor        │
   └─ ESP32节点(RTOS)───▶│  RelayHub 中枢仲裁(:8899/relay)
                        └──────────┬─────────────────┘
                          统一心率入口 on_heart_rate_update
                                   │ 每秒心率 bpm
        ┌──────────┬───────────┬───┴────────┬─────────────┬──────────────┐
        ▼          ▼           ▼            ▼             ▼              ▼
   波形图/浮窗  CSV日志    MQTT(1s定时)  InfluxDB(1s)  Tailscale WS   推送告警链路
   (UI/波形)  HeartRate   MQTTClient   InfluxDBWriter WebPushServer  NotifierManager
              Logger                                  (WS+HTTP轮询)   ├ 本地报警音
                                                                      ├ 手机推送×3渠道
                                                                      └ 摄像头联动
```

主窗口（[UI/\_\_init\_\_.py](file:///d:/vibecoding/HeartBeat/UI/__init__.py)）是组装中心：持有 NotifierManager / MQTTClient / InfluxDBWriter / HeartRateLogger / WebPushServer 实例，通过 Qt 信号把各页面、后台线程、业务模块串联起来。

## 4. 核心业务模块

### 4.1 设备连接与心率采集（[UI/DevCtrl.py](file:///d:/vibecoding/HeartBeat/UI/DevCtrl.py)）

- `DeviceConnectionUI`（L352 起）：设备管理主界面。注册 BLE 心率回调、ESP32 中继桥接、PC 虚拟节点；从 config.ini 读 `auto_connect` / `auto_reconnect` / `last_selected_device` / 收藏设备。
- 扫描与连接（L750-917）：BLE 扫描 → 设备列表 → 选择/自动连接/自动重连（重连最后连接或收藏设备）；蓝牙未开启、扫描错误均有 UI 提示。
- **智能重连**：断连后多轮重试，状态经 `reconnect_status` 信号同步到 Tailscale 服务 `info` 字段（安卓端波形上方显示）；多轮未果放弃时推送"监护已中断"提醒（`notify_reconnect_abandoned`）。
- `_up_set(option, value)` 统一写 config.ini `[GUI]` 节（如 L1200 关闭 auto_reconnect）。
- PC 虚拟节点桥 `PcNodeBridge`：中继模式下 PC 以普通节点身份参与仲裁（可被别的节点抢走手环）。

**BLE 采集层**（[Blegetheartbeat.py](file:///d:/vibecoding/HeartBeat/Blegetheartbeat.py)，`BLEHeartRateMonitor`）：
- 标准 BLE 心率规范：服务 `0x180D` / 测量特征 `0x2A37`，连接后订阅 notify，解析原始心率包并回调。
- bleak 版本兼容：`<1.0` 用 `get_services()`，`>=1.0` 用 `client.services`（服务发现未完成时最多重试 10 秒）。
- 心率数据队列 `deque(maxlen=10000)` 防内存溢出；扫描 RSSI 与上次扫描值取均值平滑（`rssi_map`），`filter_empty` 过滤空值。

### 4.2 ESP32 全屋心率中继中枢（[relay_hub.py](file:///d:/vibecoding/HeartBeat/relay_hub.py)，v1.2.15 手环三态修复）

独立线程 + 自有 asyncio 事件循环 + aiohttp WS server（路由 `/relay`，默认端口 8899，token 认证）。

**节点协议**（详见 ESP32/设计决策.md §6）：
- 节点→EXE：`hello{name,fw,ip}` / `hb{state,rssi,...}` / `hr{bpm,rssi,ts}` / `scan{rssi}` / `evt{ble_up|ble_down|ble_fail|...}`
- EXE→节点：`cfg{mac}` / `connect{mac}` / `disconnect` / `scan{dur}` / `set{name}` / `apply` / `reboot` / `hb_ack{q}` / `wake`

**仲裁时机模型**（连接权只在 EXE，节点永不自主连接）：
| 机制 | 逻辑 | 关键参数 |
|---|---|---|
| 空闲自动申请 | 无数据源 → 选校准 RSSI 最强且 > min_rssi 的节点 | min_rssi=-80 |
| 探测式切换 | 持有者 RSSI < 阈值连续 N 周期 → 断开→广播扫描→候选须强于原持有者迟滞值；无更优则回连并进保持模式（防自我切换死循环） | threshold_drop=-75，hysteresis=10dB，freeze_cycles=3 |
| 心率包率触发 | 连接活着但心率流断流（RSSI 看不出的病）：60s 滑窗包数 <40 连续 2 周期 → 探测切换 | 冷却 600s，ble_up 后 70s 保护期 |
| PC 直连互斥 | PC 连着手环时强制节点释放（pc_as_node 开启则 PC 以节点身份公平竞争） | — |
| PC 兜底（开关关闭时） | 全体节点失聪约 8 秒才由 PC 连接；节点恢复后 PC 让位，防断连-兜底循环 | lost 8 周期，让位窗 600s |
| 报警冻结 | 报警期间禁止质量切换（数据中断接管仍放行） | web_server._state['alarm'] |
| 僵尸清理 | `_decide()` 入口清理超时 pending_connect（v1.2.15 修复：此前多个提前 return 导致僵尸残留、状态矛盾） | connect 超时 18s |

**手环三态**（`band_status()`，L917）：`ok` / `nodata`（已连接但 15s 无心率=可能取下充电）/ `lost`（搜索不到）→ 经 `source_status()` → 主窗口 `hr_source_changed` 信号 → WS 广播给安卓端（通知栏显示"数据源：书房/切换中（书房 次卧）/失联"）。

**信号标定**：不同 ESP32 有 3~8dB 个体偏差，并排同位扫描取中位数求零和 bias 存 config.ini `[esp32_relay] node_biases`；选路比较用校准值，掉线判定仍用原始值。

### 4.3 心率数据下游分发

| 目标 | 模块 | 逻辑 |
|---|---|---|
| CSV 日志 | [heart_rate_logger.py](file:///d:/vibecoding/HeartBeat/heart_rate_logger.py) | 内存缓冲，每 5 分钟（可配 `[Logger] buffer_minutes`）或满 1000 条批量落盘 log/ 下 CSV |
| MQTT | [mqtt_client.py](file:///d:/vibecoding/HeartBeat/mqtt_client.py) + 主窗口 1s 定时器 | Home Assistant 集成：discovery 主题 + state 主题；连接中每秒发心率+connected；断开仅状态翻转时发一次 disconnected（防刷屏）；密码 DPAPI 加密存储 |
| InfluxDB | [influxdb_writer.py](file:///d:/vibecoding/HeartBeat/influxdb_writer.py) + 1s 定时器 | 写 `heart_rate` measurement（device tag + value field），timeout 5s，不阻塞 Qt 主线程 |
| Tailscale 数据服务 | [webpush_server.py](file:///d:/vibecoding/HeartBeat/webpush_server.py) | aiohttp 挂 qasync 循环零新线程：`WS /ws`（实时广播快照）、`GET /api/heartrate`（轮询兜底）、`GET /`（手机浏览器演示页）；默认绑定 Tailscale 网段 100.64.0.0/10，不暴露局域网 |
| 波形/浮窗 | HeartRateWaveform.py / Floatingwin_old.py | Qt 信号直驱；无效值 -1 为断流心跳信号 |

### 4.4 异常检测与推送告警链路

**通知类型全表**（[push_notifier.py](file:///d:/vibecoding/HeartBeat/push_notifier.py)）：

| 通知标题 | 触发函数 | 触发条件 | 冷却 |
|---|---|---|---|
| 心率告警 | `check_heart_rate`（L536） | 心率 > max（过高）或 < min（过低，0=不检测），持续 ≥ sustain 秒 | 每类别（规则×过高/过低）独立计时，冷却内不重复 |
| 疑似心律不齐 | `_check_irregularity`（L566） | 静息心率滑动窗口无序波动（标准差+跳变占比连续 N 窗） | 检测器独立冷却（默认 10 分钟） |
| 设备断开 | `notify_device_lost`（L587） | BLE 连接态防抖 8 秒确认断开 | 独立 5 分钟 |
| 设备恢复 | `notify_device_back`（L593） | 防抖确认重连成功 | 独立 5 分钟 |
| 重连放弃 | `notify_reconnect_abandoned`（L599） | 智能重连多轮未果（监护中断必须送达，不走冷却） | 无 |
| 摄像头移动侦测 | `notify_camera_motion`（L607） | ONVIF 事件/帧差触发，剪辑后推送；`skip_xiaoi=True`（音箱不播），不响铃不推 WS | 侦测侧冷却（默认 60s） |
| 测试推送 | PushSettingUI 测试按钮 | 手动触发 | — |

**判定细节**：`check_heart_rate` 每秒由心率回调调用；`<=0` 视为断流心跳，仅喂给心律不齐检测器清窗口。默认规则：max_hr=150 / min_hr=45 / abnormal_duration=10s / cooldown=300s（L335-338）。

**自定义时段规则**（`_load_periods`/`_active_rule`，L477-534）：`[Push] periods` JSON 数组，每条 `{enabled,start,end,max,min,sustain,cooldown}`，支持跨零点半开区间 `[start,end)`，每条规则独立阈值/持续/冷却；旧"夜间监护"配置（night_*）自动迁移为一条规则。

**[Push] 配置键全表**：`max_hr` `min_hr` `cooldown_seconds` `abnormal_duration` ｜ 渠道：`meow_enabled/meow_nickname`、`bark_enabled/bark_device_key/bark_server/bark_level/bark_sound/bark_group`、`ntfy_enabled/ntfy_topic/ntfy_server/ntfy_priority/ntfy_tags/ntfy_token`、`xiaoi_enabled/xiaoi_user/xiaoi_pass_b64/xiaoi_dids` ｜ 心律不齐：`irregular_enabled/irregular_window_seconds(60)/irregular_sd_threshold(5)/irregular_jump_bpm(5)/irregular_jump_ratio_pct(30)/irregular_rest_max_hr(100)/irregular_sustain_windows(2)/irregular_cooldown_minutes(10)` ｜ 报警：`alarm_local_enabled` `alarm_remote_enabled` `alarm_seconds(10)` ｜ `periods`

**推送执行**（`_push_all`，L426）：每渠道独立线程并行发送（单渠道超时不拖累其他）；连接类错误 2 秒后重试 1 次（防重复推送，非连接类失败直接落记录）；结果写 `log/push_history.json`（保留 200 条，字段：time/channel/ok/title/msg/note/alarm_ts）。

**报警生命周期**：
1. 心率类告警（"心率告警"/"疑似心律不齐"）进入 `_push_all` 时：生成 `alarm_ts` → 按开关触发本地报警音（MCI 播报警.mp3）和远程回调 `on_alarm_hook(alarm_seconds)` → 触发 `on_camera_alarm_hook` 全路剪辑。
2. 远程报警：主窗口 `_on_remote_alarm` → `web_server.trigger_alarm(seconds, cam, room)`（详见第 6 章协议）。
3. 剪辑完成后 `set_alarm_clips(alarm_ts, clips)` 按 alarm_ts 回填推送记录的"视频"列。

**设备断连监视**：主窗口 2 秒定时器轮询 BLE 连接态，防抖 8 秒确认后才推送（防 WinRT 连接态瞬时翻转轰炸）；恢复时同步清空安卓端 info 重连提示。

### 4.5 摄像头子系统（camera/）

| 模块 | 职责 |
|---|---|
| [stream_manager.py](file:///d:/vibecoding/HeartBeat/camera/stream_manager.py) | ffmpeg 持续拉 RTSP 流 + 线程安全环形缓冲保存报警前录像；`cut_clip(10)`/`cut_clips_for_alarm()` 报警剪辑；UI 按需解码实时画面；历史剪辑按策略清理（默认不清理） |
| [motion_watch.py](file:///d:/vibecoding/HeartBeat/camera/motion_watch.py) | 移动侦测双源：ONVIF 事件订阅 + 本地帧差（64x36 采样，灵敏度阈值表）；触发后冷却去重 → 剪辑 10 秒存档 + `notify_camera_motion` 手机推送 |
| [hls_stream.py](file:///d:/vibecoding/HeartBeat/camera/hls_stream.py) | 报警期间将 RTSP 子码流转 HLS（m3u8），供接收端报警面板实时播放 |
| [onvif_client.py](file:///d:/vibecoding/HeartBeat/camera/onvif_client.py) | 手写 SOAP 的 ONVIF 客户端：设备发现、GetProfiles、GetStreamUri、GetSnapshotUri、事件订阅 |

剪辑产物存 captures/；`/camera/clip?name=` HTTP 接口供安卓端拉取回放。

### 4.6 小爱音箱播报（[xiaomi_tts.py](file:///d:/vibecoding/HeartBeat/xiaomi_tts.py) + XiaoiSettingUI）

登录小米账号绑定音箱，心率告警等事件触发 TTS 语音播报，属于推送链路的"家中 audible"通道。

### 4.7 辅助 UI 组件

- **浮动心率窗口**（[UI/Floatingwin_old.py](file:///d:/vibecoding/HeartBeat/UI/Floatingwin_old.py) `FloatingHeartRateWindow`）：桌面悬浮实时心率，半透明背景（透明度/明度/饱和度可调），文字颜色/模板（如"心率: {rate}"）、字号、内边距可配，位置记忆；设置项内嵌于"基本设置"页（`FloatingWindowSettingUI`）。
- **更新下载窗口**（[UI/UpDownloadwin.py](file:///d:/vibecoding/HeartBeat/UI/UpDownloadwin.py) `UpdWindow`）：`DownloadThread`（QThread）8KB 分块下载新版本 EXE，进度条实时显示、可取消，完成后经 `start_update_program()` 以 `-updatemode` 重启替换。
- **应用图标**（[UI/heartratepng.py](file:///d:/vibecoding/HeartBeat/UI/heartratepng.py)）：PNG 以十六进制内嵌代码，`get_icon()` 惰性生成 QIcon，无需外部图标文件。
- **基础控件库**（[UI/basicwidgets.py](file:///d:/vibecoding/HeartBeat/UI/basicwidgets.py)）：全局样式 `GLOBAL_QSS` + `page_layout/group_layout/button_row/hint_label/wrap_scroll/CheackBox_` 等统一布局构件，保证各设置页风格一致。
- **报警视频回放**（[UI/VideoPlayerDialog.py](file:///d:/vibecoding/HeartBeat/UI/VideoPlayerDialog.py)）：推送记录/报警面板点击后在对话框内回放报警剪辑。

## 5. UI 结构（主窗口 9 个 Tab，配置项与业务逻辑）

> 所有设置页遵循统一模式：`load_settings()` 从 config.ini 读初始值 → 用户点"保存设置"校验并写回（敏感字段 DPAPI 加密）→ 发 `*_settings_changed` 信号 → 主窗口重启对应服务。

### 5.1 心率监测 Tab（[UI/DevCtrl.py](file:///d:/vibecoding/HeartBeat/UI/DevCtrl.py) `HeartRateMonitorUI`，L200-286）
- 左列：实时波形图（HeartRateWaveform）+ 心率日志文本框 + 保存日志按钮；右列：推送记录表（PushHistoryUI）。
- `append_heart_rate()` 同时写日志并刷波形；撑满整页，滚动只在日志框与表格内部。
- 波形（[HeartRateWaveform.py](file:///d:/vibecoding/HeartBeat/UI/HeartRateWaveform.py)）：`add_heart_rate()` 有效值追加数据，非正值（-1 断流心跳）画为 NaN 断线（示波器式断点）；渲染含动态 Y 轴、心率区间背景色、填充区域、平均线、当前心率文本、X 轴时间刻度（L170-242）。

### 5.2 基本设置 Tab（设备管理 + 软件设置 + 浮窗设置）
- 设备管理区：刷新按钮、设备列表、自动刷新/自动连接/自动重连复选框（`[GUI]` 节）、收藏设备、连接/断开。
- 软件设置：允许后台运行（use_bg）、开机自启（startup，注册表）、启动时检查更新（update_check）、检查更新按钮。
- 浮动窗口设置：文字颜色/模板、背景透明度/明度/饱和度、字号、内边距、位置。

### 5.3 MQTT 设置 Tab（[MQTTSettingUI.py](file:///d:/vibecoding/HeartBeat/UI/MQTTSettingUI.py)）
| 控件 | config.ini `[MQTT]` 键 | 说明 |
|---|---|---|
| 启用 | enabled | 启用后 1s 定时发布 |
| 服务器/端口/用户名 | broker / port / username | 默认 localhost:1883 |
| 密码 | password | **DPAPI 加密存储** |
| 状态主题 | topic | 默认 `homeassistant/sensor/heartrate/state` |
| HA 自动发现 | discovery_enabled / discovery_topic | 默认开，config 主题 |
- "测试连接"按钮即时验证 broker 可达；保存后 `mqtt_settings_changed` → 主窗口断开重连 + 进度对话框。

### 5.4 InfluxDB 设置 Tab（[InfluxDBSettingUI.py](file:///d:/vibecoding/HeartBeat/UI/InfluxDBSettingUI.py)）
- `[InfluxDB]`：enabled / url / token（**DPAPI 加密**）/ org / bucket；必填校验 → 测试连接 → `influxdb_settings_changed` 重连。

### 5.5 Tailscale Tab（[TailscaleSettingUI.py](file:///d:/vibecoding/HeartBeat/UI/TailscaleSettingUI.py)）
- `[Tailscale]`：enabled / address（`auto`=自动探测 100.64.0.0/10 网段 IP，失败回退 127.0.0.1）/ port（默认 8765）。
- 保存时校验 IP 与端口 → `tailscale_settings_changed` → 服务立即启停（L106-168）；页面显示运行状态与手机访问地址。

### 5.6 消息推送 Tab（[PushSettingUI.py](file:///d:/vibecoding/HeartBeat/UI/PushSettingUI.py)）
- 默认规则区：心率上限/下限、超限持续判定秒数、告警冷却秒数（L28-87）。
- 三渠道区：MeoW（昵称）；Bark（device key、服务器、level/sound/group）；ntfy（主题、服务器、优先级、标签、访问令牌）（L156-193）。
- 小爱渠道区（与 5.7 联动）；心律不齐检测参数区；报警音区：本地响铃/接收端响铃独立开关 + 报警秒数。
- 自定义时段规则编辑（对应 `[Push] periods`）；每渠道/规则有测试入口；保存 → `push_settings_changed` → `notifier.load_config()` 重建渠道。

### 5.7 小爱音箱 Tab（[XiaoiSettingUI.py](file:///d:/vibecoding/HeartBeat/UI/XiaoiSettingUI.py)）
- `[Push]`：xiaoi_enabled / xiaoi_user / xiaoi_pass_b64（**DPAPI 加密**）/ xiaoi_dids（选中音箱 deviceID）。
- 流程：填小米账号 → "登录获取音箱"拉取设备列表 → 勾选音箱 → 测试播报 → 保存（L144-164）。直连小米云端，不经本地网关。

### 5.8 摄像头 Tab（[CameraUI.py](file:///d:/vibecoding/HeartBeat/UI/CameraUI.py)）
- `[Camera]` 配置键：`cameras`（JSON 列表：名称/ONVIF 地址/RTSP 流地址/子码流等，stream_manager.py:L741 读取）、`motion_sensitivity`（帧差灵敏度，0-2）、`motion_cooldown`（默认 60s）、`cleanup_mode`（历史剪辑清理：0=不清理）、`cleanup_value`（默认 30）。
- 页面含实时画面预览（按需解码）；报警环形缓冲在 load_settings 时即启动（与页面是否显示无关）。

### 5.9 ESP32 中继 Tab（[RelaySettingUI.py](file:///d:/vibecoding/HeartBeat/UI/RelaySettingUI.py)）
- `[esp32_relay]` 配置键：`enabled`（勾选才启动 hub）、`port`(8899)、`token`、三种模式（单机 / `pc_as_node`=0 中枢模式 / =1 PC 节点模式）、`pc_node_name`(书房)、仲裁参数 `threshold_drop`(-75) / `hysteresis_db`(10) / `freeze_cycles`(3) / `min_rssi`(-80) / `stale_seconds`(30)、`node_biases`（标定 JSON）、`room_camera_map`（房间→摄像头 JSON）。
- 模式行为差异（L41-80）：单机模式隐藏中继功能；仅中枢模式 PC 不参与竞争（失聪 8 秒 PC 兜底）；PC 节点模式 PC 以普通节点身份公平参与仲裁。

其他 UI 细节：
- 关闭窗口=最小化到托盘（首次弹气泡提示），真正退出走托盘菜单"退出程序"，退出前若设备已连接需确认，确认后才停摄像头流、断 BLE（泵事件驱动 qasync 协程，上限 2 秒）。
- 托盘菜单含"连接/断开 xxx 设备"快捷项。
- 浮动窗口（Floatingwin_old.py）桌面悬浮显示实时心率。
- 视频回放（VideoPlayerDialog.py）：推送记录/报警面板点击回看报警视频；报警取消或结束后剪辑已到手即显示"▶ 回看报警视频"按钮。

## 6. EXE ↔ 安卓端接口协议（Tailscale 数据服务，[webpush_server.py](file:///d:/vibecoding/HeartBeat/webpush_server.py)）

> 安卓接收端/服务端对接 EXE 的全部通道。aiohttp 挂 qasync 事件循环（零新线程），默认绑定 Tailscale 网段 100.64.0.0/10，不暴露局域网。

### 6.1 HTTP 路由表

| 路由 | 参数 | 响应 | 行号 |
|---|---|---|---|
| `GET /` | — | HTML 演示页（手机浏览器可开） | L329 |
| `GET /api/heartrate` | — | 当前 `_state` 快照 JSON + `clients` 数（轮询兜底通道） | L285 |
| `GET /ws` | — | WebSocket（主通道） | L290 |
| `GET /camera/snapshot` | `?cam=摄像头名`（缺省=默认） | JPEG 帧（报警期间 2fps 轮询）；未就绪 503 | L304 |
| `GET /camera/clip` | `?name=xxx.mp4` | 剪辑流式播放，FileResponse 原生支持 Range（拖动/边下边播）；仅允许 captures/ 下 .mp4，防路径穿越 | L316 |
| `GET /camera/live/index.m3u8` | — | 报警 HLS 播放列表 | L164 |
| `GET /camera/live/segment.ts` | — | 报警 HLS 分片 | L165 |

### 6.2 WS 状态快照 `_state` 完整字段（L94-107）

| 字段 | 类型 | 含义 | 更新入口 |
|---|---|---|---|
| `heart_rate` | int | 最新心率（0=无/断流） | `update()`（L200） |
| `timestamp` | str | 心率时间戳 | `update()` |
| `status` | str | `connected` / `disconnected` | `update()` |
| `device` | str | EXE 侧设备名 | 构造时传入 |
| `info` | str | 附加状态文本（智能重连进度等，空=无） | `update_info()`（L217） |
| `alarm` | bool | true=接收端循环响铃 | `trigger_alarm()`（L224） |
| `clip_url` | str | 默认摄像头剪辑地址（旧接收端兼容） | `push_clip()`（L120） |
| `clips` | list | 全量剪辑 `[{"cam","url"}]`（新接收端 tab 切换） | `push_clip()` |
| `hr_source` | dict | 中继数据源状态（房间/PC、phase、三态等） | `push_source()`（L129） |
| `alarm_cam` / `alarm_room` | str | 报警绑定摄像头/房间（空=回退默认） | `trigger_alarm()` |
| `alarm_live` | str | 报警 HLS m3u8 地址（空=未就绪回退快照） | `set_live_url()`（L135） |

广播机制：任何字段变更即向所有 WS 客户端**广播全量快照 JSON**（L272-282，每客户端独立 3s 超时任务，慢客户端不阻塞他人）；新连接即推当前快照；aiohttp `heartbeat=25` 秒保活；客户端上行消息仅用于心跳检测，无下行命令通道（**取消报警等操作目前无 EXE 侧 HTTP 接口，属可完善点**）。

断流保护：中继节点持连手环期间（`hr_source.phase == "active"`），PC 直连的 -1 断流信号被抑制不上播（L203-207），防接收端状态每 2 秒翻转。

### 6.3 报警生命周期（服务端视角）

```
trigger_alarm(seconds=10, cam, room)      L224
  ├─ alarm=true，同时清空 clip_url/clips/alarm_live（防误播旧片段）
  ├─ 广播快照 → 接收端开始响铃、报警面板显示 alarm_cam 房间画面
  ├─ on_alarm_start 回调 → 启动快照流(兜底) + HLS 转码(后台线程就绪后 set_live_url)
  └─ 起自动复位任务（重复报警先 cancel 旧任务，防提前复位）
seconds 后 _alarm_auto_clear:              L243
  ├─ alarm=false 广播 → 接收端停铃
  └─ on_alarm_end 回调 → 停全部快照流与 HLS
```

剪辑就绪（异步，报警后数秒）：`push_clip(clip_url, clips)` 广播 → 接收端显示"▶ 回看报警视频"按钮与相机 tab；取消报警/结束后剪辑到手仍会补推。

### 6.4 中继 WS（/relay，节点用，见 4.2）

ESP32 节点用 token 认证接入 `/relay`，协议 hello/hb/hr/scan/evt ↔ cfg/connect/disconnect/scan/set/apply/reboot/hb_ack/wake。

### 6.5 双向参数同步（2026-09-30 恢复实施）

曾论证"安卓服务端与 EXE 任意一端改参数另一端同步"，2026-09-29 曾以"二选一部署不会共存"为由否决；**2026-09-30 用户改定：两端可以共存，恢复为正式需求——任意一端修改参数，另一端同步生效**。方案沿用原存档设计：

- **同步范围白名单**：推送阈值（上限/下限/持续/冷却）、报警开关与报警秒数（本地/远程）、移动侦测灵敏度、时段规则、房间→摄像头绑定、摄像头启用开关；**不含** MQTT 密码、InfluxDB token 等敏感字段（不进同步快照，也不允许对端写入），不含仲裁参数/Tailscale 端口/srv 绑定等本机参数。
- **冲突策略**：last-write-wins + 版本号 `settings_rev` 单调递增，两端同时修改时后写者胜出，收到更高 rev 才本地应用。
- **通道与发现**：两端各暴露 `POST /api/settings`（带 token 认证，config.ini/Prefs 各存一份）；经 mDNS `_hrmlink._tcp` 实例互发现（两端都已注册），发现到"另一个中枢实例"即互相同步，也支持手填对端地址。
- **本地改动出口统一**：任一端修改参数必须走同一"同步出口"广播（EXE 本地改动也发 settings 快照），确保对端可感知。
- **共存角色约定**：手环/ESP32 节点归属遵循仲裁现实（谁持有连谁）；参数同步与数据归属解耦，两中枢互为参数镜像。

## 7. 配置系统（config.ini，[system_utils.py](file:///d:/vibecoding/HeartBeat/system_utils.py)）

- `init_config()` 缺失时创建并补齐必需节；`gs(section, key, default, type)` / `ups()` 统一读写（带默认值回退与调试标签）。
- 主要节：`[Device]`（last_selected_device 等）、`[GUI]`、`[MQTT]`、`[InfluxDB]`、`[Tailscale]`、`[Push]`、`[Camera]`、`[esp32_relay]`（端口/token/仲裁参数/room_camera_map/node_biases）、`[Logger]`。
- 敏感字段（MQTT 密码、InfluxDB token）经 **Windows DPAPI** 加密存储（dpapi_protect/unprotect）。

## 8. 系统机制

- **开机自启**：注册表项 `HRMLink`（add/remove/check_startup）；检测到启动项被其它程序占用时询问是否覆盖。
- **在线更新**：启动时后台线程 `checkupdate()` 对比 GitHub Releases 的版本元数据 → 弹窗提示 → UpdWindow 下载 → 以 `-updatemode`/`-endup` 参数重启完成 EXE 替换与清理。
- **单实例**：check_run / AppisRunning，重复启动弹"程序已经在运行了"。
- **崩溃兜底**：sys.excepthook → 统一错误弹窗（可选择性退出）。

## 9. 构建与版本

- 版本号三处同步：`__main__.py VER2` + `version.txt`（filevers/prodvers 数字元组 + FileVersion/ProductVersion 字符串）。
- 构建：`pyinstaller HRMLink.spec --clean --noconfirm`（窗口程序 console=False）；备选配置：`HRMLink_standalone.spec`（独立版）、`launcher.spec`（小型启动器 HRMLink_Launcher）。非 frozen 运行时还会自动生成 build.bat（build_bat.py）。
- 发布 tag/下载地址由 Fvname 拼进 version.json。

## 10. 离线脚本与开发辅助（不随 EXE 运行）

- [analyze_heart_rate.py](file:///d:/vibecoding/HeartBeat/analyze_heart_rate.py)：离线分析 log/ 下心率 CSV（统计汇总）。
- [visualize_heart_rate.py](file:///d:/vibecoding/HeartBeat/visualize_heart_rate.py)：matplotlib 绘制心率趋势图（中文字体适配）。
- [run_without_check.py](file:///d:/vibecoding/HeartBeat/run_without_check.py)：设置 `SKIP_DUPLICATE_CHECK=1` 环境变量启动 EXE，绕过单实例检查（调试用）；对应 start.bat / start_no_check.bat 脚本。
- system_utils_ascii.py：system_utils 的 ASCII 兼容版（规避打包环境编码问题），由 `__main__.py` 优先导入并回同步版本属性。

## 11. 关键经验与坑（历史沉淀）

- koa-connect 类包装桥接会丢上下文（历史教训，现为原生实现）。
- 报警瞬间（T+0）剪辑尚在切割转码中，接收端点播放应提示"剪辑生成中，稍候几秒再试"。
- 手环三态同步曾因 `_decide()` 提前 return 未清理 `pending_connect` 导致"切换中/失联"状态矛盾（v1.2.15 修复）。
- qasync 事件循环下退出/断开必须泵事件（processEvents）驱动协程完成，`QThread.msleep` 阻塞的恰是该循环。
- bleak 连接过程 WinRT `is_connected` 会瞬时翻转，一切连接态通知须防抖。
