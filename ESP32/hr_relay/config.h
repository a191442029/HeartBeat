#pragma once
// ============================================================
// HRM-Link ESP32 心率中继节点 · 配置常量
// 说明: 大部分参数可在首次配网(SoftAP)或EXE端下发覆盖,
//       这里只放出厂默认值与协议常量。
// ============================================================

#define FW_VERSION        "1.1.0"

// ---- 配网热点 ----
#define AP_SSID_PREFIX    "HRM-Link-"      // 热点名: HRM-Link-XXXXXX(尾缀=MAC后3字节)
#define AP_PASSWORD       "12345678"       // 热点密码(至少8位)

// ---- 出厂默认(EXE连接参数, 配网后以配网页/EXE下发为准) ----
#define DEFAULT_EXE_HOST  "192.168.1.100"  // EXE电脑局域网IP
#define DEFAULT_EXE_PORT  8899             // relay_hub 端口
#define DEFAULT_TOKEN     "HRMLink-ESP32-2025"
#define DEFAULT_NAME      ""               // 节点名留空 -> 自动 HRM-XXXXXX

// ---- 手环心率服务(标准 Heart Rate Service) ----
#define HR_SVC_UUID       0x180D
#define HR_CHR_UUID       0x2A37

// ---- 行为参数(与EXE端协议约定, 勿随意改动) ----
#define HB_INTERVAL_MS      2000   // 心跳周期(与EXE仲裁周期一致)
#define HR_MIN_INTERVAL_MS  5000   // 心率上报最小间隔(节流, EXE端也会兜底节流)
#define SCAN_INTERVAL_MS    256    // 空闲扫描间隔(37%占空比, 共存友好)
#define SCAN_WINDOW_MS      96
#define BLE_CONN_TIMEOUT_S  15     // connect命令总超时(扫描等待+建立)
#define WS_RECONNECT_MS     3000   // WS自动重连间隔
#define BAND_HEARD_FRESH_MS 8000   // "最近听到手环广播"的有效窗口(hb.rssi 新鲜度)
