#pragma once
// ============================================================
// HRM-Link ESP32 心率中继节点 · 配置常量
// 说明: 大部分参数可在首次配网(SoftAP)或EXE端下发覆盖,
//       这里只放出厂默认值与协议常量。
// ============================================================

#define FW_VERSION        "1.1.10n"

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

// ---- WiFi OTA 升级(ArduinoOTA; 主机名=hr-relay-<MAC后3字节>, 端口3232) ----
#define OTA_PASSWORD      "HRMLink-OTA-2025"

// ---- 行为参数(与EXE端协议约定, 勿随意改动) ----
#define HB_INTERVAL_MS      2000   // 心跳周期(与EXE仲裁周期一致)
#define HR_MIN_INTERVAL_MS  1000   // 心率上报最小间隔(手环通知1Hz全量转发; EXE端兜底0.9s)
#define SCAN_INTERVAL_MS    100    // 扫描间隔ms(对齐"ESP32双机方案"实测成功参数)
#define SCAN_WINDOW_MS      99     // 扫描窗口ms ≈99%占空比(双机方案扫描手环正常)
#define BLE_CONN_TIMEOUT_S  30     // connect命令总超时(扫描等待+建立); 手环1s广播必撞见, 深衰落靠快速失败上报由中枢换人
#define WS_RECONNECT_MS     3000   // WS自动重连间隔
#define BAND_HEARD_FRESH_MS 8000   // "最近听到手环广播"的有效窗口(hb.rssi 新鲜度)

// ---- v1.1.8 行为参数(P1-1自愈 / P3-19快速失败 / P3-15待命占空比) ----
#define CONN_MAX_ATTEMPTS   2      // 每轮连接最多尝试次数(防深衰落硬撑总超时, 快速报ble_fail由中枢换人)
#define HEAL_MAX_ATTEMPTS   5      // 断开自愈最大轮数(1/2/4/8s指数退避, 总窗约23s)
#define QUIESCE_INTERVAL_MS 5000   // 2%待命扫描间隔ms(手环被正常持有期间, hub经hb_ack.q下发)
#define QUIESCE_WINDOW_MS   100    // 2%待命扫描窗口ms(5s醒0.1s; 全员并听, 系统级发现速度≈单点全速一半)

// ---- v1.1.9 中枢自动发现(mDNS) ----
#define HUB_MDNS_TYPE       "_hrmlink"  // 服务类型: _hrmlink._tcp (中枢经zeroconf/NsdManager注册)
#define HUB_REDISCOVER_MS   60000UL     // WS断连后每60s重新mDNS发现一次(中枢换机/换IP自愈)
