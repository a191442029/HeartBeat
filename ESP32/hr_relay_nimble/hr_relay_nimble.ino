// ============================================================
// HRM-Link ESP32 心率中继节点 v1.1.9
// 角色: 纯执行节点 —— 只做"举手报告"和"服从命令", 永不自主连接。
//       一切连接权归 EXE 中枢 (ws://EXE_IP:8899/relay)
//       (唯一例外 v1.1.8 P1-1: 非命令式断开后的自愈回连, 命令式断开绝不回连)
// 流程: 首启SoftAP配网 -> 连WiFi -> mDNS发现中枢 -> WS连EXE -> 待命
//       收 connect{mac} -> 扫描手环 -> 连接 -> 订阅0x2A37 -> 上报hr
// v1.1.9 新增: 中枢自动发现(mDNS枚举_hrmlink._tcp, 兜底NVS缓存->配网地址;
//              WS断连60s重新发现, 换中枢/换IP零重配)
// v1.1.8 新增: P1-1 断开自愈(指数退避回连) / P3-19 连接快速失败(2次即报ble_fail)
//              P1-6② idle重听到手环边沿即时上报evt heard / P3-15 2%待命占空比(hb_ack.q+wake)
// 依赖: NimBLE-Arduino 2.5.1 (h2zero维护, 全异步API, 内存占用低, getRssi直读缓存;
//       Bluedroid对照版在ESP32/hr_relay), WebSockets by Markus Sattler
// 详见 ESP32/设计决策.md
// ============================================================

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <NimBLEDevice.h>   // 伞头文件: scan/client/remote char 全包含
#include <ArduinoOTA.h>
#include <ESPmDNS.h>       // v1.1.9: 查询_hrmlink._tcp自动发现中枢
#include "config.h"

// ---------------- 配置(NVS持久化) ----------------
Preferences prefs;
String cfg_ssid, cfg_pass, cfg_host, cfg_name, cfg_token;
uint16_t cfg_port = DEFAULT_EXE_PORT;
String band_mac = "";              // 手环MAC(EXE通过cfg/connect命令下发, 不持久化)

// UTF-8安全截断到maxb字节: 中文3字节/字, 剥除截断产生的残尾字节,
// 防止非法UTF-8进入hello/set消息导致EXE端json解析失败(节点永远无法上线)
String utf8_cut(const String& s, size_t maxb) {
  if (s.length() <= maxb) return s;
  String r = s.substring(0, maxb);
  while (r.length() && ((uint8_t)r[r.length()-1] & 0xC0) == 0x80)
    r.remove(r.length()-1);   // 剥尾部continuation字节
  if (r.length()) {           // 剩下的起始字节若声称的多字节长度不完整则一并剥除
    uint8_t b = (uint8_t)r[r.length()-1];
    int need = ((b & 0x80) == 0) ? 1 : ((b & 0xE0) == 0xC0) ? 2 : ((b & 0xF0) == 0xE0) ? 3 : 4;
    if (need > 1) r.remove(r.length()-1);   // 起始字节已无continuation跟随(上面剥光) → 必不完整
  }
  return r;
}

String node_name() {
  String n = cfg_name.length() ? cfg_name : ("HRM-" + WiFi.macAddress().substring(9));  // 默认名=MAC后3字节
  // 剥除引号/反斜杠: 名字会进手拼的hello JSON, 带引号会让消息残缺致节点无法上线;
  // 在唯一出口清洗, NVS里已存的名字也一并自愈, 无需重新配网
  n.replace("\"", "");
  n.replace("\\", "");
  return n;
}

// ---------------- 状态机 ----------------
enum NodeState { ST_PROVISION, ST_NET_OK, ST_IDLE, ST_CONNECTING, ST_ACTIVE, ST_HEAL };
NodeState state = ST_PROVISION;
// ST_HEAL(v1.1.8 P1-1): 非命令式断开后的自愈退避等待态; 到点转ST_CONNECTING复用连接流程
// (hb按"connecting"上报, 中枢视为连接中不派活; 自愈成功ble_up抢回, 耗尽报ble_fail{heal}回IDLE)

unsigned long last_hb_ms = 0, last_hr_ms = 0;
unsigned long connect_start = 0;       // connect命令起始时刻(减法比较, 防millis回绕)
unsigned long probe_window_start = 0;  // 探测窗起点(配合scan_dur; scan_dur=0表示无窗口; 勿用scan_start, 与libnet80211符号冲突)
unsigned long scan_dur = 0;            // 探测窗时长ms
int probe_best = 0;                    // 扫描窗口内最佳RSSI(0=没听到)
volatile int last_band_rssi = 0;       // 最近听到的手环广播RSSI
volatile unsigned long band_heard_ms = 0;
volatile unsigned long adv_seen = 0;   // 收到的所有BLE广播包计数(诊断: 增量0=射频全聋)
volatile unsigned long band_seen = 0;  // 目标手环广播包计数
volatile unsigned long scan_fail = 0;  // 扫描启动失败计数(诊断: sc=0或sf>0=扫描没在收包)
const char* g_fail_why = "";           // 最近一次连接失败原因(conn/svc/chr/sub/noadv)
volatile int pending_bpm = 0;          // BLE通知线程 -> loop 的待发心率
volatile int pending_rssi = 0;
volatile bool target_seen = false;
NimBLEAddress target_addr = NimBLEAddress();   // 默认全零地址, 扫描命中后由广播包写入(自带地址类型)

// v1.1.8 新增状态
volatile bool quiesce = false;    // P3-15: hub心跳应答下发的待命标志(idle时改2%占空比扫描)
bool healing = false;             // P1-1: 自愈回连进行中(仅非命令式断开)
int heal_attempts = 0;            // 自愈已用轮数(退避1/2/4/8s, 封顶8s, 超HEAL_MAX_ATTEMPTS放弃)
unsigned long heal_next_ms = 0;   // 下轮自愈发起时刻
int conn_attempts = 0;            // P3-19: 本轮connect已尝试次数(达CONN_MAX_ATTEMPTS快速失败)
bool cmd_disc = false;            // 命令式断开标志(disconnect命令置位; 自愈的禁区)
volatile bool heard_edge = false; // P1-6②: idle态"从聋到听到"边沿待上报(loop统一发送)
volatile int heard_edge_rssi = 0;

// v1.1.9 中枢自动发现
String hub_host = "";             // 当前使用的中枢地址(mDNS发现/NVS缓存/配网值三者之一)
uint16_t hub_port = 0;
unsigned long ws_down_since = 0;  // WS连续断连起点(减法比较防回绕; 0=在线)

// ---------------- BLE (NimBLE-Arduino 2.5.1: 全异步扫描, getRssi直读连接RSSI缓存, 内存占用低;
//             与Bluedroid版协议/行为完全一致, 仅BLE层API替换) ----------------
NimBLEClient* pClient = nullptr;
NimBLERemoteCharacteristic* pHrChr = nullptr;
volatile bool ble_connected = false;
volatile bool g_scan_on = false;   // NimBLE虽有isScanning(), 保持与Bluedroid版同构的自维护标志

// 广播扫描回调: 只关心目标手环, 记录RSSI (广播一对多, 被动收听不抢连接)
// NimBLE注意: maxResults=0时device指针仅在回调内有效, 本回调只读不存, 天然满足
class ScanCb : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    adv_seen++;   // 所有广播包计数(不受MAC过滤影响, 用于诊断射频是否工作)
    if (band_mac.length()) {
      // 忽略大小写比较(地址串大小写与EXE config存储可能不一致);
      // c_str视图+strcasecmp: 回调高频执行, 避免额外堆分配
      std::string s = dev->getAddress().toString();
      if (strcasecmp(s.c_str(), band_mac.c_str()) != 0) return;
      band_seen++;
      int r = dev->getRSSI();
      last_band_rssi = r;
      unsigned long prev_heard = band_heard_ms;
      band_heard_ms = millis();
      // P1-6②: idle态"从聋到听到"边沿(超过新鲜窗=刚重新听到) → 置标志由loop即时上报,
      // 断联接管/回家发现不必等2s心跳; 仅idle态报(连接中的目标追踪走connect流程)
      if (state == ST_IDLE && millis() - prev_heard > BAND_HEARD_FRESH_MS) {
        heard_edge = true;
        heard_edge_rssi = r;
      }
      // probe_best=0表示"没听到"; RSSI恒为负, 首包必须直接写入, 否则r>0永不成立、探测恒报0
      if (scan_dur && millis() - probe_window_start < scan_dur && (probe_best == 0 || r > probe_best)) probe_best = r;
      if (state == ST_CONNECTING) { target_addr = dev->getAddress(); target_seen = true; }
    }
  }
} scanCb;

// 连接回调: 断开只置标志, 收尾统一在loop做(单一写者, 防竞态)
class ClientCb : public NimBLEClientCallbacks {
  void onDisconnect(NimBLEClient* c, int reason) override {
    ble_connected = false;
  }
} clientCb;

bool ble_start_scan() {
  NimBLEScan* s = NimBLEDevice::getScan();
  // 扫描参数与"ESP32双机方案"/Bluedroid版完全一致: 主动扫描+99%占空比
  // true=重复广播也回调——手环每秒广播, wantDuplicates=false会把同MAC重复包全滤掉,
  // 导致RSSI永不更新/hb.rssi超时归零/仲裁永不触发
  s->setScanCallbacks(&scanCb, true);
  s->setActiveScan(true);   // 主动扫描: 连SCAN_RSP一起收, 信号弱时多一倍机会
  // P3-15: 待命2%占空比(hub下发q=1且本节点idle且非探测窗); 探测窗/连接/常态全速不变
  bool standby = quiesce && state == ST_IDLE && !scan_dur;
  s->setInterval(standby ? QUIESCE_INTERVAL_MS : SCAN_INTERVAL_MS);
  s->setWindow(standby ? QUIESCE_WINDOW_MS : SCAN_WINDOW_MS);
  s->setMaxResults(0);   // 无限扫描防结果向量无限膨胀; 0=只走回调不存列表
  // duration=0持续扫描; NimBLE的start()原生异步(ble_gap_disc直接返回), 无Bluedroid阻塞陷阱
  bool ok = s->start(0, false);
  g_scan_on = ok;
  if (!ok) scan_fail++;           // 启动失败不再静默, 心跳带出
  return ok;
}

void ble_stop_scan() { NimBLEDevice::getScan()->stop(); g_scan_on = false; }

// 连接手环并订阅心率通知; 成功返回true, 失败时 *why 给出阶段原因
bool ble_connect_band(const char** why) {
  ble_stop_scan();
  if (!pClient) {
    pClient = NimBLEDevice::createClient();
    pClient->setClientCallbacks(&clientCb, false);   // false=回调对象是静态存储, 防库delete全局对象
    // 显式5s超时(ms): 防广播模式下initiator干等可连接包阻塞30s, 把ws.loop()/OTA饿死心跳导致节点掉线
    pClient->setConnectTimeout(5000);
  }
  // NimBLEAddress自带地址类型(取自广播包, 无需Bluedroid的0xFF双试); asyncConnect=false阻塞式
  if (!pClient->connect(target_addr)) { *why = "connect"; return false; }
  NimBLERemoteService* svc = pClient->getService(NimBLEUUID((uint16_t)HR_SVC_UUID));
  NimBLERemoteCharacteristic* chr = svc ? svc->getCharacteristic(NimBLEUUID((uint16_t)HR_CHR_UUID)) : nullptr;
  if (!chr) { *why = svc ? "chr" : "svc"; if (pClient->isConnected()) pClient->disconnect(); return false; }
  bool ok = chr->canNotify();
  if (ok) ok = chr->subscribe(true, [](NimBLERemoteCharacteristic* c, uint8_t* data, size_t len, bool notif) {
    if (len < 2) return;
    int bpm = (data[0] & 0x01) ? (data[1] | (len > 2 ? (data[2] << 8) : 0)) : data[1];
    if (bpm <= 0 || bpm > 250) return;
    pending_bpm = bpm;
    // NimBLE的getRssi()直读连接RSSI缓存(无同步往返), 栈回调里调也安全;
    // 仍只置待发标志由loop统一发送(跨任务安全); 兜底用最后一次广播RSSI, hr发送时优先getRssi()实时值
    pending_rssi = last_band_rssi;
  });
  if (!ok) {
    // 服务发现/订阅失败但BLE链路已建立: 必须清残留连接, 否则僵尸连接占坑
    // (手环被隐形占用不广播, 全屋失联且WS断开的放手规则因ble_connected=false而失效)
    *why = "sub";
    if (pClient->isConnected()) pClient->disconnect();
    pHrChr = nullptr;   // 防悬空: disconnect后旧属性对象已销毁
    return false;
  }
  pHrChr = chr;
  ble_connected = true;
  return true;
}

void ble_disconnect() {
  if (pClient && pClient->isConnected()) pClient->disconnect();
  ble_connected = false;
  pHrChr = nullptr;
  pending_bpm = 0;
}

// ---------------- v1.1.9 中枢自动发现 ----------------
// mDNS枚举 _hrmlink._tcp, 取第一个有效应答; 仅当结果与NVS缓存不同才写NVS(防60s重试磨损flash)
bool discover_hub(String* host, uint16_t* port) {
  int n = MDNS.queryService(HUB_MDNS_TYPE, "_tcp");
  for (int i = 0; i < n; i++) {
    IPAddress ip = MDNS.address(i);
    uint16_t p = (uint16_t)MDNS.port(i);
    if (ip == IPAddress() || p == 0) continue;
    *host = ip.toString();
    *port = p;
    String ch = prefs.getString("host_last", "");
    if (ch != *host || prefs.getUShort("port_last", 0) != *port) {
      prefs.putString("host_last", *host);
      prefs.putUShort("port_last", *port);
    }
    Serial.printf("[mDNS] 发现中枢: %s:%u (%s)\n", host->c_str(), *port, MDNS.hostname(i).c_str());
    return true;
  }
  return false;
}

// ---------------- WebSocket ----------------
WebSocketsClient ws;
bool ws_up = false;

void ws_send(const char* fmt, ...) {
  char buf[256];
  va_list args; va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  ws.sendTXT(buf);
}

void send_hello() {
  ws_send("{\"type\":\"hello\",\"name\":\"%s\",\"fw\":\"%s\",\"ip\":\"%s\"}",
          node_name().c_str(), FW_VERSION, WiFi.localIP().toString().c_str());
}

// 极简JSON值提取(字符串与数值; 只处理扁平{"k":"v"/n}形态, 足够本协议使用)
bool json_str(const char* j, const char* key, char* out, size_t outlen) {
  char pat[24];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(j, pat);
  if (!p) return false;
  p = strchr(p + strlen(pat), ':');
  if (!p) return false;
  p++;
  while (*p == ' ') p++;
  if (*p == '"') {                       // 字符串值
    p++;
    const char* e = strchr(p, '"');
    if (!e || (size_t)(e - p) >= outlen) return false;
    memcpy(out, p, e - p);
    out[e - p] = 0;
    return true;
  }
  // 数值/字面量: 拷到分隔符为止(供atol/atoi解析, 兼容"dur":1000等无引号字段)
  const char* e = p;
  while (*e && *e != ',' && *e != '}' && *e != ' ') e++;
  if (e == p || (size_t)(e - p) >= outlen) return false;
  memcpy(out, p, e - p);
  out[e - p] = 0;
  return true;
}

void handle_cmd(uint8_t* payload) {
  char val[64];
  // EXE→节点消息以"type"为命令字段(与节点→EXE方向对称): cfg/connect/disconnect/scan/set/apply/reboot
  if (!json_str((const char*)payload, "type", val, sizeof(val))) return;
  String cmd = String(val);

  if (cmd == "cfg" && json_str((const char*)payload, "mac", val, sizeof(val))) {
    band_mac = String(val);
  }
  else if (cmd == "connect") {
    // 连接权在EXE手里: 收到命令才允许发起BLE连接
    if (json_str((const char*)payload, "mac", val, sizeof(val))) band_mac = String(val);
    if (band_mac.isEmpty()) { ws_send("{\"type\":\"evt\",\"evt\":\"no_band_mac\"}"); return; }
    healing = false; heal_attempts = 0; conn_attempts = 0; cmd_disc = false;   // 命令派活优先于自愈
    if (ble_connected) ble_disconnect();          // 先清旧连接(理论不会发生)
    if (pClient && pClient->isConnected()) pClient->disconnect();  // 僵尸连接兜底清理
    target_seen = false; probe_best = 0; g_fail_why = "";
    state = ST_CONNECTING;
    connect_start = millis();                     // 减法比较防millis回绕
    if (!g_scan_on) ble_start_scan();
  }
  else if (cmd == "disconnect") {
    cmd_disc = true;                              // 命令式断开: 自愈禁区(仲裁意志, 回连=抗命)
    healing = false; heal_attempts = 0; conn_attempts = 0;
    if (ble_connected) { ble_disconnect(); state = ST_IDLE; ble_start_scan(); }
    else {
      if (pClient && pClient->isConnected()) pClient->disconnect();  // 僵尸连接兜底清理
      state = ST_IDLE;
    }
  }
  else if (cmd == "hb_ack") {
    // P3-15: 心跳应答携带待命标志(q=1=手环被正常持有, 空闲节点可低占空比待命)
    int q = 0;
    if (json_str((const char*)payload, "q", val, sizeof(val))) q = atoi(val) ? 1 : 0;
    if (q != (int)quiesce) {
      quiesce = q;
      // 占空比参数对"下次扫描启动"生效: idle非探测窗时重启扫描即时应用
      if (state == ST_IDLE && !scan_dur) {
        if (g_scan_on) ble_stop_scan();
        ble_start_scan();
      }
      Serial.printf("[BLE] 待命模式(2%%占空比): %d\n", quiesce);
    }
  }
  else if (cmd == "wake") {
    // P3-15: 扫描令(ble_down/lost时中枢下发): 立即恢复99%全速扫描
    quiesce = false;
    if (state == ST_IDLE && !scan_dur) {
      if (g_scan_on) ble_stop_scan();
      ble_start_scan();
    }
  }
  else if (cmd == "scan") {
    // 探测窗: 清零窗口最佳值, loop里到点回包
    long dur = 1000;
    if (json_str((const char*)payload, "dur", val, sizeof(val))) dur = atol(val);
    probe_best = 0;
    probe_window_start = millis();
    scan_dur = (unsigned long)max(dur, 200L);
    if (state != ST_ACTIVE) {
      // 探测窗强制全速(覆盖2%待命参数; scan_dur已置位, start自动取全速分支)
      if (g_scan_on) ble_stop_scan();
      ble_start_scan();
    }
  }
  else if (cmd == "set" && json_str((const char*)payload, "name", val, sizeof(val))) {
    cfg_name = utf8_cut(String(val), 48);   // 48字节≈16汉字, 保证hello JSON在ws_send缓冲区内
    prefs.putString("name", cfg_name);   // 3.x put内部已自动nvs_commit
    ws_send("{\"type\":\"evt\",\"evt\":\"name_ok\"}");
    send_hello();   // 立即用新名重报: hub据此无缝换绑(不断WS不断BLE), EXE端改名即时生效
  }
  else if (cmd == "apply") {
    char host[48], tok[48]; int port = cfg_port;
    if (json_str((const char*)payload, "host", host, sizeof(host))) {
      if (json_str((const char*)payload, "port", val, sizeof(val))) port = atoi(val);
      if (json_str((const char*)payload, "token", tok, sizeof(tok))) prefs.putString("token", String(tok));
      prefs.putString("host", String(host));
      prefs.putUShort("port", (uint16_t)port);
      prefs.putBool("ok", true);   // 3.x put内部已自动nvs_commit
      ws_send("{\"type\":\"evt\",\"evt\":\"apply_ok\"}");
      delay(300);
      ESP.restart();
    }
  }
  else if (cmd == "reboot") { delay(200); ESP.restart(); }
}

void onWsEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      ws_up = true;
      send_hello();
      break;
    case WStype_DISCONNECTED:
      // 放手规则: WS断了 = 失去指挥链, 立即放弃手环连接(防占坑), 空手待命
      // isConnected兜底: 覆盖"已连上但订阅失败"的僵尸态(此时ble_connected为false)
      ws_up = false;
      if (ble_connected || (pClient && pClient->isConnected())) ble_disconnect();
      state = ST_IDLE;
      healing = false; heal_attempts = 0; conn_attempts = 0;   // 自愈依赖指挥链, WS断则作废
      quiesce = false;   // 中枢不在: 无待命概念, 默认全速(重连后由hb_ack重新同步)
      // 必须恢复扫描: hub仲裁靠心跳RSSI, 断线后不扫描=听不到手环=永远等不到派活
      if (!g_scan_on) ble_start_scan();
      break;
    case WStype_TEXT:
      handle_cmd(payload);
      break;
    default: break;
  }
}

// ---------------- 配网门户(SoftAP + Captive Portal) ----------------
WebServer portal(80);
DNSServer dns;

const char PORTAL_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HRM-Link 节点配网</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:24px auto">
<h2>HRM-Link ESP32 节点配网</h2>
<form action="/save" method="POST">
<p>节点名称(如: 主卧)<br><input name="name" style="width:100%%"></p>
<p>WiFi名称<br><input name="ssid" required style="width:100%%"></p>
<p>WiFi密码<br><input name="pass" type="password" style="width:100%%"></p>
<!-- v1.1.10n: EXE地址/端口/Token 三字段已移除 — 中枢发现走 mDNS->NVS缓存->编译默认值 三级链,
     token 用编译内置默认(DEFAULT_TOKEN), /save 处理器的 hasArg 兜底逻辑保留兼容旧表单 -->
<p><input type="submit" value="保存并重启" style="width:100%%;padding:10px"></p>
</form></body></html>
)rawliteral";

void start_provision() {
  String ssid = String(AP_SSID_PREFIX) + node_name();
  WiFi.mode(WIFI_AP);
  WiFi.softAP(ssid.c_str(), AP_PASSWORD);
  dns.start(53, "*", WiFi.softAPIP());
  portal.on("/", []() {
    char buf[1600];
    snprintf(buf, sizeof(buf), PORTAL_HTML);
    portal.send(200, "text/html", buf);
  });
  portal.on("/save", []() {
    if (portal.hasArg("ssid") && portal.arg("ssid").length()) {
      prefs.putString("ssid", portal.arg("ssid"));
      prefs.putString("pass", portal.arg("pass"));
      prefs.putString("host", portal.hasArg("host") ? portal.arg("host") : String(DEFAULT_EXE_HOST));
      prefs.putUShort("port", (uint16_t)(portal.hasArg("port") ? portal.arg("port").toInt() : DEFAULT_EXE_PORT));
      prefs.putString("token", portal.hasArg("token") && portal.arg("token").length() ? portal.arg("token") : String(DEFAULT_TOKEN));
      if (portal.hasArg("name")) prefs.putString("name", utf8_cut(portal.arg("name"), 48));
      prefs.putBool("ok", true);   // 3.x put内部已自动nvs_commit
      portal.send(200, "text/html; charset=utf-8", "<meta charset='utf-8'>已保存, 节点重启中...");
      delay(800);
      ESP.restart();
    } else portal.send(400, "text/html; charset=utf-8", "WiFi名称不能为空");
  });
  portal.onNotFound([]() { portal.sendHeader("Location", "http://" + WiFi.softAPIP().toString()); portal.send(302, "", ""); });
  portal.begin();
  Serial.printf("[Provision] AP: %s pass: %s, 门户: http://%s\n", ssid.c_str(), AP_PASSWORD, WiFi.softAPIP().toString().c_str());
}

// ---------------- 初始化 ----------------
void setup() {
  Serial.begin(115200);
  prefs.begin("hrmrelay", false);
  cfg_ssid  = prefs.getString("ssid", "");
  cfg_pass  = prefs.getString("pass", "");
  cfg_host  = prefs.getString("host", DEFAULT_EXE_HOST);
  cfg_port  = prefs.getUShort("port", DEFAULT_EXE_PORT);
  cfg_token = prefs.getString("token", DEFAULT_TOKEN);
  cfg_name  = utf8_cut(prefs.getString("name", DEFAULT_NAME), 48);   // 兜底: 旧NVS里可能存了超长名

  if (cfg_ssid.isEmpty()) { start_provision(); state = ST_PROVISION; return; }

  // ---- 短基线期: BLE先于WiFi初始化并扫描10秒, 保持"BLE先占射频"的初始化顺序;
  //      广播包累计数可从启动后的hb.at观察(增量0=射频全聋) ----
  NimBLEDevice::init(node_name().c_str());
  ble_start_scan();   // 回调注册已并入ble_start_scan
  delay(10000);
  Serial.printf("[BLE] 基线期累计广播包: %lu\n", adv_seen);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(cfg_ssid.c_str(), cfg_pass.c_str());
  Serial.printf("[WiFi] 连接 %s", cfg_ssid.c_str());
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(250); Serial.print("."); }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" 失败, 进入配网模式");
    start_provision(); state = ST_PROVISION; return;
  }
  Serial.printf(" OK, IP=%s\n", WiFi.localIP().toString().c_str());

  // ---- v1.1.9 中枢地址解析链: mDNS发现 -> NVS上次缓存 -> 配网值 ----
  MDNS.begin(("hr-relay-" + WiFi.macAddress().substring(9)).c_str());  // mDNS栈初始化(纯查询也需要)
  hub_host = cfg_host; hub_port = cfg_port;
  if (!discover_hub(&hub_host, &hub_port)) {
    String last = prefs.getString("host_last", "");
    if (last.length()) {
      hub_host = last;
      hub_port = prefs.getUShort("port_last", cfg_port);
      Serial.printf("[mDNS] 未发现中枢, 用NVS缓存: %s:%u\n", hub_host.c_str(), hub_port);
    } else {
      Serial.printf("[mDNS] 未发现中枢, 用配网地址: %s:%u\n", hub_host.c_str(), hub_port);
    }
  }

  // ---- WiFi OTA(首次USB烧录后, 后续升级无需拆机) ----
  {
    String mac = WiFi.macAddress(); mac.replace(":", "");
    String host = "hr-relay-" + mac.substring(6);      // ASCII主机名(mDNS不允许中文)
    ArduinoOTA.setHostname(host.c_str());
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]() {   // 释放射频, 避免BLE扫描干扰升级
      ble_stop_scan();
      if (pClient && pClient->isConnected()) pClient->disconnect();
      ble_connected = false; state = ST_IDLE;
      Serial.println("[OTA] 开始升级");
    });
    ArduinoOTA.onEnd([]() { Serial.println("[OTA] 升级完成, 重启"); });
    ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[OTA] 错误 %u\n", e); });
    ArduinoOTA.begin();
    Serial.printf("[OTA] 就绪: %s@%s:3232\n", host.c_str(), WiFi.localIP().toString().c_str());
  }

  // (BLE init+扫描已在基线期提前完成, 扫描持续运行跨WiFi共存)

  char path[16];
  snprintf(path, sizeof(path), "/relay");
  ws.begin(hub_host.c_str(), hub_port, path);
  String auth = "Authorization: Bearer " + cfg_token;
  ws.setExtraHeaders(auth.c_str());
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(WS_RECONNECT_MS);
  ws.enableHeartbeat(15000, 3000, 2);
  state = ST_NET_OK;
  Serial.println("[Node] 就绪, 等待EXE指令");
}

// ---------------- 主循环 ----------------
void loop() {
  if (state == ST_PROVISION) { dns.processNextRequest(); portal.handleClient(); return; }

  ws.loop();
  ArduinoOTA.handle();

  // ---- v1.1.9: WS断连超阈值 → 重新mDNS发现(中枢可能换机/换IP; 到点重定向) ----
  if (!ws_up) {
    if (ws_down_since == 0) {
      ws_down_since = millis();
    } else if (millis() - ws_down_since >= HUB_REDISCOVER_MS) {
      ws_down_since = millis();
      String h; uint16_t p;
      if (discover_hub(&h, &p) && (h != hub_host || p != hub_port)) {
        hub_host = h; hub_port = p;
        ws.disconnect();
        char path[16];
        snprintf(path, sizeof(path), "/relay");
        ws.begin(hub_host.c_str(), hub_port, path);
        Serial.printf("[mDNS] 中枢已变更, 重定向: %s:%u\n", hub_host.c_str(), hub_port);
      }
    }
  } else {
    ws_down_since = 0;
  }

  // ---- connect命令执行(ST_CONNECTING; 含自愈回连轮次) ----
  if (state == ST_CONNECTING) {
    if (ble_connected) {
      healing = false; heal_attempts = 0; conn_attempts = 0;
      state = ST_ACTIVE;
      ws_send("{\"type\":\"evt\",\"evt\":\"ble_up\",\"mac\":\"%s\"}", band_mac.c_str());
      Serial.println("[BLE] 已连接手环");
    } else if (target_seen) {
      const char* why = "";
      if (ble_connect_band(&why)) return;  // 下一轮loop确认
      // 失败: 记录阶段原因, 回扫描; P3-19: 达CONN_MAX_ATTEMPTS即快速失败上报,
      // 不硬撑总超时(深衰落挣扎从90s缩到~15s, 中枢收到ble_fail立即降权换人)
      g_fail_why = why;
      target_seen = false;
      ble_start_scan();
      conn_attempts++;
      if (conn_attempts >= CONN_MAX_ATTEMPTS) {
        conn_attempts = 0;
        if (healing) {
          healing = false;
          if (heal_attempts < HEAL_MAX_ATTEMPTS) {
            // P1-1: 指数退避后再试(2/4/8s封顶); ST_HEAL期间hb报connecting, 中枢不派本节点
            heal_next_ms = millis() + (1000UL << min(heal_attempts, 3));
            state = ST_HEAL;
            Serial.printf("[BLE] 自愈第%d轮失败, 退避重试\n", heal_attempts);
          } else {
            state = ST_IDLE;
            ws_send("{\"type\":\"evt\",\"evt\":\"ble_fail\",\"why\":\"heal\"}");
            Serial.println("[BLE] 自愈耗尽, 回到待命");
          }
        } else {
          state = ST_IDLE;
          ws_send("{\"type\":\"evt\",\"evt\":\"ble_fail\",\"why\":\"%s\"}",
                  g_fail_why[0] ? g_fail_why : "connect");
          Serial.printf("[BLE] 连接快速失败(%s)\n", g_fail_why);
        }
      }
    } else if (millis() - connect_start >= BLE_CONN_TIMEOUT_S * 1000UL) {
      // 总超时: 多为搜不到广播(手环被别人连上/超距); 自愈场景直接放弃回IDLE
      if (healing) {
        healing = false;
        conn_attempts = 0;
        heal_attempts = 0;
        state = ST_IDLE;
        ws_send("{\"type\":\"evt\",\"evt\":\"ble_fail\",\"why\":\"heal\"}");
      } else {
        state = ST_IDLE;
        conn_attempts = 0;
        // 阶段原因优先: 失败后target_seen已被清false, 若再判noadv会丢失最后一次失败阶段
        const char* why = g_fail_why[0] ? g_fail_why : "noadv";
        ws_send("{\"type\":\"evt\",\"evt\":\"ble_fail\",\"why\":\"%s\"}", why);
      }
      Serial.printf("[BLE] 连接超时(%ds)\n", BLE_CONN_TIMEOUT_S);
    }
  }

  // ---- P1-1 自愈退避到点: 转CONNECTING复用连接流程(减法比较防millis回绕) ----
  if (state == ST_HEAL && (long)(millis() - heal_next_ms) >= 0) {
    state = ST_CONNECTING;
    connect_start = millis();
    target_seen = false;
    g_fail_why = "";
    if (!g_scan_on) ble_start_scan();
  }

  // ---- ACTIVE保活(断开只从loop收尾) ----
  if (state == ST_ACTIVE && !ble_connected) {
    pending_bpm = 0;
    ble_start_scan();
    if (cmd_disc || !ws_up) {
      // 命令式断开(仲裁意志)或WS断(失去指挥链): 绝不自愈, 回待命
      cmd_disc = false;
      state = ST_IDLE;
      ws_send("{\"type\":\"evt\",\"evt\":\"ble_down\"}");
      Serial.println("[BLE] 手环断开, 回到待命");
    } else {
      // P1-1: 非命令式意外断开 → 自愈回连。立即报ble_down让中枢照走接管流程,
      // 本节点以connecting自处(中枢不会重复派它), 回连成功ble_up抢回, 失败耗尽回IDLE
      healing = true;
      heal_attempts = 1;
      conn_attempts = 0;
      heal_next_ms = millis() + 1000UL;   // 首轮退避1s(给中枢接管协调留窗口)
      state = ST_HEAL;
      ws_send("{\"type\":\"evt\",\"evt\":\"ble_down\"}");
      Serial.println("[BLE] 手环意外断开, 进入自愈回连");
    }
  }

  // ---- P1-6②: idle态重新听到手环广播 → 即时上报(不等2s心跳; 中枢据此提前派活) ----
  if (heard_edge) {
    heard_edge = false;
    if (ws_up && state == ST_IDLE)
      ws_send("{\"type\":\"evt\",\"evt\":\"heard\",\"rssi\":%d}", heard_edge_rssi);
  }

  // ---- 待发心率(节流>=1s; RSSI用连接实时值, 兜底用最后一次广播RSSI) ----
  if (pending_bpm > 0 && state == ST_ACTIVE && millis() - last_hr_ms >= HR_MIN_INTERVAL_MS) {
    last_hr_ms = millis();
    int rssi = pending_rssi;
    // loop任务里调getRssi()安全(非栈任务, 不存在自己等自己); 0=取不到则用广播RSSI兜底
    if (pClient && pClient->isConnected()) { int r = pClient->getRssi(); if (r) rssi = r; }
    ws_send("{\"type\":\"hr\",\"bpm\":%d,\"rssi\":%d,\"ts\":%lu}",
            pending_bpm, rssi, (unsigned long)(millis() / 1000));
    Serial.printf("[HR] %d bpm rssi=%d\n", pending_bpm, rssi);
    pending_bpm = 0;
  }

  // ---- 扫描窗口到点回包(EXE探测用; 减法比较防millis回绕) ----
  if (scan_dur && millis() - probe_window_start >= scan_dur) {
    scan_dur = 0;
    ws_send("{\"type\":\"scan\",\"rssi\":%d}", probe_best);
    probe_best = 0;
  }

  // ---- 心跳(2s): 名字/状态/RSSI/在线时长/广播包计数增量 ----
  if (millis() - last_hb_ms >= HB_INTERVAL_MS) {
    last_hb_ms = millis();
    int rssi = 0;
    const char* st = "idle";
    if (state == ST_ACTIVE && pClient && pClient->isConnected()) {
      rssi = pClient->getRssi(); st = "active";
    } else if (state == ST_CONNECTING || state == ST_HEAL) {
      // ST_HEAL按connecting上报: 中枢视为连接中, 不派本节点也不对它评估弱信号
      st = "connecting";
      if (millis() - band_heard_ms < BAND_HEARD_FRESH_MS) rssi = last_band_rssi;
    } else if (millis() - band_heard_ms < BAND_HEARD_FRESH_MS) {
      rssi = last_band_rssi;
    }
    static unsigned long s_adv_reported = 0;
    unsigned long adv_delta = adv_seen - s_adv_reported;   // 增量0=本周期射频一个包都没收到
    s_adv_reported = adv_seen;
    ws_send("{\"type\":\"hb\",\"state\":\"%s\",\"rssi\":%d,\"up\":%lu,\"adv\":%lu,\"at\":%lu,\"sc\":%d,\"sf\":%lu}",
            st, rssi, (unsigned long)(millis() / 1000), adv_delta, adv_seen,
            g_scan_on ? 1 : 0, scan_fail);
  }
}
