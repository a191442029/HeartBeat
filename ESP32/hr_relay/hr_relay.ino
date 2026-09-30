// ============================================================
// HRM-Link ESP32 心率中继节点 v1.1.0
// 角色: 纯执行节点 —— 只做"举手报告"和"服从命令", 永不自主连接。
//       一切连接权归 EXE 中枢 (ws://EXE_IP:8899/relay)
// 流程: 首启SoftAP配网 -> 连WiFi -> WS连EXE -> 待命
//       收 connect{mac} -> 扫描手环 -> 连接 -> 订阅0x2A37 -> 上报hr
// 依赖: Bluedroid BLE库(core自带BLEDevice.h, 与实测正常的"ESP32双机方案"同款栈),
//       WebSockets by Markus Sattler
// 详见 ESP32/设计决策.md
// ============================================================

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <BLERemoteCharacteristic.h>
#include <esp_gap_ble_api.h>
#include <ArduinoOTA.h>
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
enum NodeState { ST_PROVISION, ST_NET_OK, ST_IDLE, ST_CONNECTING, ST_ACTIVE };
NodeState state = ST_PROVISION;

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
BLEAddress target_addr = BLEAddress((uint8_t*)"\0\0\0\0\0\0");

// ---------------- BLE (Bluedroid栈, 与"ESP32双机方案"同款API——该方案同一块板实测扫描手环正常;
//             NimBLE 2.5.1 版同一板子60秒纯BLE基线仅12-14包, 判定栈差异) ----------------
BLEClient* pClient = nullptr;
BLERemoteCharacteristic* pHrChr = nullptr;
volatile bool ble_connected = false;
volatile bool g_scan_on = false;   // Bluedroid无公开isScanning, 自维护标志

// 广播扫描回调: 只关心目标手环, 记录RSSI (广播一对多, 被动收听不抢连接)
class ScanCb : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    adv_seen++;   // 所有广播包计数(不受MAC过滤影响, 用于诊断射频是否工作)
    if (band_mac.length()) {
      // 忽略大小写比较(地址串大小写与EXE config存储可能不一致; Bluedroid toString返回String);
      // c_str零拷贝视图+strcasecmp: 回调高频执行, 避免额外堆分配
      String s = dev.getAddress().toString();
      if (strcasecmp(s.c_str(), band_mac.c_str()) != 0) return;
      band_seen++;
      int r = dev.getRSSI();
      last_band_rssi = r;
      band_heard_ms = millis();
      // probe_best=0表示"没听到"; RSSI恒为负, 首包必须直接写入, 否则r>0永不成立、探测恒报0
      if (scan_dur && millis() - probe_window_start < scan_dur && (probe_best == 0 || r > probe_best)) probe_best = r;
      if (state == ST_CONNECTING) { target_addr = dev.getAddress(); target_seen = true; }
    }
  }
} scanCb;

// 连接回调: 断开只置标志, 收尾统一在loop做(单一写者, 防竞态)
class ClientCb : public BLEClientCallbacks {
  void onDisconnect(BLEClient* c) override {
    ble_connected = false;
  }
} clientCb;

bool ble_start_scan() {
  BLEScan* s = BLEDevice::getScan();
  // 扫描参数与"ESP32双机方案"完全一致: 主动扫描+99%占空比
  // true=重复广播也回调——手环每秒广播, wantDuplicates=false会把同MAC重复包全滤掉,
  // 导致RSSI永不更新/hb.rssi超时归零/仲裁永不触发(与双机方案默认行为保持一致)
  s->setAdvertisedDeviceCallbacks(&scanCb, true);
  s->setActiveScan(true);   // 主动扫描: 连SCAN_RSP一起收, 信号弱时多一倍机会
  s->setInterval(SCAN_INTERVAL_MS);
  s->setWindow(SCAN_WINDOW_MS);
  // 三参数异步版: duration=0持续扫描且不阻塞主循环(ws/OTA需要loop活着).
  // 两参数版start()内部会死等扫描结束信号量, duration=0=永久死锁(挂死根因)
  bool ok = s->start(0, nullptr, false);
  g_scan_on = ok;
  if (!ok) scan_fail++;           // 启动失败不再静默, 心跳带出
  return ok;
}

void ble_stop_scan() { BLEDevice::getScan()->stop(); g_scan_on = false; }

// 连接手环并订阅心率通知; 成功返回true, 失败时 *why 给出阶段原因
bool ble_connect_band(const char** why) {
  ble_stop_scan();
  if (!pClient) {
    pClient = BLEDevice::createClient();
    pClient->setClientCallbacks(&clientCb);
  }
  // 地址类型0xFF=用扫描缓存的真实类型(广播包自带, 无需双试);
  // 显式5s超时: 防广播模式下initiator干等可连接包阻塞30s, 把ws.loop()/OTA饿死心跳导致节点掉线
  if (!pClient->connect(target_addr, 0xFF, 5000)) { *why = "connect"; return false; }
  BLERemoteService* svc = pClient->getService(BLEUUID((uint16_t)HR_SVC_UUID));
  BLERemoteCharacteristic* chr = svc ? svc->getCharacteristic(BLEUUID((uint16_t)HR_CHR_UUID)) : nullptr;
  if (!chr) { *why = svc ? "chr" : "svc"; if (pClient->isConnected()) pClient->disconnect(); return false; }
  bool ok = chr->canNotify();
  if (ok) {
    // 心率通知回调只置待发标志, 不在此直接发WS(跨任务安全)
    chr->registerForNotify([](BLERemoteCharacteristic* c, uint8_t* data, size_t len, bool notif) {
      if (len < 2) return;
      int bpm = (data[0] & 0x01) ? (data[1] | (len > 2 ? (data[2] << 8) : 0)) : data[1];
      if (bpm <= 0 || bpm > 250) return;
      pending_bpm = bpm;
      // 栈任务内严禁getRssi(): Bluedroid版是同步GATT往返, 在回调上下文等自身信号量=死锁
      // → BLE任务被WDT静默复位(连接成功后~30s失联的根因); 用连接前最后一次广播RSSI代替
      pending_rssi = last_band_rssi;
    });
  }
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
    if (ble_connected) ble_disconnect();          // 先清旧连接(理论不会发生)
    if (pClient && pClient->isConnected()) pClient->disconnect();  // 僵尸连接兜底清理
    target_seen = false; probe_best = 0; g_fail_why = "";
    state = ST_CONNECTING;
    connect_start = millis();                     // 减法比较防millis回绕
    if (!g_scan_on) ble_start_scan();
  }
  else if (cmd == "disconnect") {
    if (ble_connected) { ble_disconnect(); state = ST_IDLE; ble_start_scan(); }
    else {
      if (pClient && pClient->isConnected()) pClient->disconnect();  // 僵尸连接兜底清理
      state = ST_IDLE;
    }
  }
  else if (cmd == "scan") {
    // 探测窗: 清零窗口最佳值, loop里到点回包
    long dur = 1000;
    if (json_str((const char*)payload, "dur", val, sizeof(val))) dur = atol(val);
    probe_best = 0;
    probe_window_start = millis();
    scan_dur = (unsigned long)max(dur, 200L);
    if (state != ST_ACTIVE && !g_scan_on) ble_start_scan();
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
<p>EXE电脑地址<br><input name="host" value="%s" style="width:100%%"></p>
<p>EXE端口<br><input name="port" value="%u" style="width:100%%"></p>
<p>令牌Token<br><input name="token" value="%s" style="width:100%%"></p>
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
    snprintf(buf, sizeof(buf), PORTAL_HTML, DEFAULT_EXE_HOST, DEFAULT_EXE_PORT, DEFAULT_TOKEN);
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
  BLEDevice::init(node_name().c_str());
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
  ws.begin(cfg_host.c_str(), cfg_port, path);
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

  // ---- connect命令执行(ST_CONNECTING) ----
  if (state == ST_CONNECTING) {
    if (ble_connected) {
      state = ST_ACTIVE;
      ws_send("{\"type\":\"evt\",\"evt\":\"ble_up\",\"mac\":\"%s\"}", band_mac.c_str());
      Serial.println("[BLE] 已连接手环");
    } else if (target_seen) {
      const char* why = "";
      if (ble_connect_band(&why)) return;  // 下一轮loop确认
      // 失败: 记录阶段原因, 回到扫描继续等(直到总超时)
      g_fail_why = why;
      target_seen = false;
      ble_start_scan();
    } else if (millis() - connect_start >= BLE_CONN_TIMEOUT_S * 1000UL) {
      state = ST_IDLE;
      // 阶段原因优先: 失败后target_seen已被清false, 若再判noadv会丢失最后一次失败阶段
      const char* why = g_fail_why[0] ? g_fail_why : "noadv";
      ws_send("{\"type\":\"evt\",\"evt\":\"ble_fail\",\"why\":\"%s\"}", why);
      Serial.printf("[BLE] 连接失败(%s)\n", why);
    }
  }

  // ---- ACTIVE保活(断开只从loop收尾) ----
  if (state == ST_ACTIVE && !ble_connected) {
    state = ST_IDLE;
    pending_bpm = 0;
    ws_send("{\"type\":\"evt\",\"evt\":\"ble_down\"}");
    Serial.println("[BLE] 手环断开, 回到待命");
    ble_start_scan();
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
    } else if (state == ST_CONNECTING) {
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
