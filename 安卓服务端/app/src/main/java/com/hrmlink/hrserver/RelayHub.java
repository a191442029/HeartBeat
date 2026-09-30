package com.hrmlink.hrserver;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import fi.iki.elonen.NanoHTTPD;
import fi.iki.elonen.NanoWSD;

/**
 * ESP32 全屋心率中继中枢（1:1 移植 EXE relay_hub.py v1.2.15, Java 线程版）
 *
 * 挂载: 独立 NanoWSD 监听 0.0.0.0:8899, WS 路由 /relay（端口对齐 EXE 8899; ESP32 节点全在
 * 局域网无 Tailscale, 必须独立于数据服务的 LAN 可达端口; 启动时经 NsdManager 通告
 * _hrmlink._tcp:8899, 节点 MDNS.queryService 自动发现 → 换中枢/换板子零重配）。
 *
 * 节点协议(详见 EXE ESP32/设计决策.md §6):
 *   节点→中枢: hello{name,fw,ip} / hb{state,rssi,up,adv,at,sc,sf} / hr{bpm,rssi,ts}
 *             scan{rssi} / evt{evt: ble_up|ble_down|ble_fail{why}|no_band_mac|name_ok|apply_ok|heard{rssi}}
 *   中枢→节点: cfg{mac} / connect{mac} / disconnect / scan{dur} / set{name} / apply{...} / reboot
 *             hb_ack{q}(q=1=手环被正常持有, 固件据此2%待命) / wake(失聪扫描令)
 *
 * 仲裁时机模型(两个永不 + 三类触发):
 *   - 永不: 节点自主连接(连接权只在中枢); 报警期间质量切换(数据中断接管仍放行)
 *   - 空闲自动申请: 无数据源 → 选平滑RSSI最强且>min_rssi的节点(必须在场: 窗口内听到过手环广播)
 *   - 探测式切换: 持有者RSSI<threshold 连续freeze_cycles个周期 → disconnect → 广播scan →
 *                 候选须为"其他节点"且校准值强于原持有者hysteresis_db以上 → 无更优则回连并进入
 *                 保持模式(no_better_hold, 防自我切换死循环)
 *   - 连接进行中(pending_connect/connecting)不评估弱信号(防陈旧conn_rssi二次触发探测)
 *   - 本机直连优先/互斥: 板载BLE连着手环时强制节点释放(对齐 EXE PC直连)
 *   - 本机兜底(对齐 EXE PC兜底): 全体节点失聪约8个周期 → 本机BLE连接; 节点恢复后让位(带冷却)
 *   - 放手规则: 节点WS断开 → 立即视为放弃连接权(防占坑)
 *
 * 与 EXE 的差异(安卓侧收敛, 均为有意裁剪):
 *   - PC虚拟节点(pc_as_node)不移植: 板载BLE即"直连", 兜底经 LocalBridge 下发, 无需自注册为节点
 *   - 信号标定协程不移植(P1): 节点偏差 biases 仅存储/参与选路(空=中性, 等价无标定), 标定UI待后续
 *   - 节点仲裁参数经 Prefs [RELAY_*] 配置(默认值对齐 EXE 代码默认)
 *
 * v1.2.15 修复语义保持(2026-09-29 实锤):
 *   - 僵尸连接清理前移到 _decide() 入口: 标定冻结/探测推进/节点全下线等任何早退路径都不允许
 *     pending_connect 滞留(否则接收端永远显示"切换中")
 *   - source_status() 兜底校验: pending_connect 必须 state=connecting、probe_stage 必须在时限内
 *     才报"切换中", 防状态冻结滞留误报
 *   - _probe_cleanup() 清 origin 残值: probe_old_name 不清会永远顶着旧来源名
 *   - _best_idle() 在场校验: 必须窗口内听到过手环广播才派活(rssi_ema 永不衰减,
 *     手环外出后残值会驱动对"聋"节点循环下发注定失败的 connect)
 *   - ble_fail 回收持有权+解除保持模式(否则 _decide 在保持模式处每周期提前 return, 仲裁器锁死)
 *
 * 线程模型:
 *   - WS 回调(open/message/close)在各连接读线程; 仲裁器独立线程 1s/2s 轮询; 发送统一在
 *     单线程 net 执行器串行执行(NanoWSD send 是同步 IO)
 *   - 所有共享状态(节点表/仲裁状态)统一在 lock 对象内读写; 回调(hr/source/band)一律在
 *     锁外调用, 防止回调方再入死锁
 */
public class RelayHub {

    private static final String TAG = "HRServer";

    // ---------- 常量(对齐 relay_hub.py) ----------
    public static final String TOKEN_DEFAULT = "HRMLink-ESP32-2025";
    public static final int RELAY_PORT = 8899;               // 中继专用端口(对齐 EXE; mDNS 注册同端口)
    private static final long ARB_INTERVAL_MS = 2000;        // 仲裁周期, 与节点心跳一致
    private static final long ARB_INTERVAL_LOST_MS = 1000;   // 手环失联态加快接管/兜底响应(P1-6)
    private static final long HR_MIN_INTERVAL_MS = 900;      // 中枢侧心率节流(与固件1s节流双保险)
    private static final long HR_NODATA_S = 15;              // 已连接但无心率判定(可能取下充电/未佩戴)
    private static final long BAND_HEARD_WINDOW_S = 10;      // 未连接时"搜索到手环"窗口(固件rssi新鲜度8s+余量)
    private static final double EMA_ALPHA = 0.4;             // RSSI指数平滑系数(≈最近10次)
    private static final long PROBE_DISCONNECT_S = 3;        // 探测·断开阶段超时
    private static final long PROBE_SCAN_S = 3;              // 探测·扫描窗等待(节点1s扫描+回包余量; py 2.5s取整向上)
    private static final long CONNECT_DEADLINE_S = 18;       // connect命令总超时(与固件15s对齐+余量)

    // 心率包率触发(P1-7): 连接活着但心率流断流, RSSI看不出来的病
    private static final long HR_RATE_WINDOW_S = 60;         // 包率统计滑窗
    private static final int HR_RATE_LOW = 40;               // 窗内包数低于此值=断流(手环1包/s, 满窗~60)
    private static final int HR_RATE_STREAK = 2;             // 连续N个周期低于才触发
    private static final long HR_RATE_WARMUP_S = 70;         // ble_up后保护期
    private static final long HR_PROBE_COOLDOWN_S = 600;     // 包率触发冷却(防手环真无数据时无限换手)

    private static final long FAIL_COOLDOWN_S = 30;          // 节点ble_fail达2次后降权时长(P1-2)

    // 本机兜底(对齐 EXE PC兜底): 全体节点失聪才由本机BLE连接, 节点恢复后让位
    private static final int PC_FB_AFTER_CYCLES = 8;         // lost持续N个周期(lost态1s/周期≈8秒)才兜底
    private static final long PC_FB_RETRY_S = 30;            // 兜底连接命令重试间隔
    private static final int PC_FB_HEARD_STREAK = 3;         // 让位条件: 节点持续N个周期重新听到手环
    private static final long PC_FB_HOLD_S = 600;            // 兜底连接的自动让位有效窗
    private static final long PC_FB_RELEASE_COOLDOWN_S = 120;// 让位后冷却(防信号边缘断连-兜底循环)

    // ---------- 回调/桥接口 ----------
    /** 统一心率入口(fn(ts, bpm); 对齐 EXE set_hr_callback → on_heart_rate_update) */
    public interface HrCallback { void onHr(String ts, int bpm); }

    /** 报警状态探测(对齐 set_alarm_check) */
    public interface Check { boolean check(); }

    /** 数据源状态变化回调(fn(state JSON), 仲裁线程调用; 对齐 set_source_callback) */
    public interface SourceCallback { void onSource(JSONObject st); }

    /** 手环三态变化回调(fn(status JSON), hub线程调用; 对齐 set_band_status_callback) */
    public interface BandStatusCallback { void onBandStatus(JSONObject st); }

    /** 本机BLE命令桥(对齐 EXE _pc_bridge.on_command): connect{mac} / disconnect */
    public interface LocalBridge { void onCommand(JSONObject cmd); }

    // ---------- 可调参数(start 时读 Prefs) ----------
    private String token = TOKEN_DEFAULT;
    private int thresholdDrop = -75;
    private int hysteresisDb = 10;
    private int minRssi = -80;
    private int staleSeconds = 30;
    private int freezeCycles = 3;

    // ---------- 运行状态(除注明外均在 lock 内读写) ----------
    private final Object lock = new Object();
    private final Object sendLock = new Object();
    private Context app;
    private volatile boolean running = false;
    private volatile RelayWs relayWs = null;                 // 中继专用 WS 监听(0.0.0.0:8899)
    private android.net.nsd.NsdManager nsd = null;           // mDNS 服务注册(节点自动发现)
    private android.net.nsd.NsdManager.RegistrationListener nsdListener = null;
    private Thread arbiter;
    private java.util.concurrent.ScheduledExecutorService net;

    /** 节点表: name -> Node */
    private final LinkedHashMap<String, Node> nodes = new LinkedHashMap<>();

    // 仲裁状态
    private String activeNode = null;      // 当前持有手环连接的节点名
    private String pendingConnect = null;  // 已下发connect待ble_up确认的节点名
    private int lowStreak = 0;             // 持有者弱信号连续周期数
    private boolean noBetterHold = false;  // 保持模式: 探测已确认当前持有者即最优
    private String probeStage = null;      // null/'disconnecting'/'scanning'
    private long probeDeadline = 0;
    private int probePreRssi = 0;
    private String probeOldName = null;
    private final LinkedHashMap<String, Integer> scanResults = new LinkedHashMap<>(); // 探测窗 name->rssi

    // 心率包率状态(P1-7): 当前持有者hr到达时间滑窗(仅active的hr计入, ble_up清空)
    private final List<Long> hrRate = new ArrayList<>();
    private int hrLowStreak = 0;
    private long hrProbeTs = 0;

    // 信号标定/个体偏差: bias>0表示该节点读数偏强, 选路用 calibrated = raw - bias
    private final Map<String, Integer> biases = new LinkedHashMap<>();

    // 本机兜底状态
    private int fbLostStreak = 0;
    private int fbHeardStreak = 0;
    private long fbLastTry = 0;
    private long fbCmdTs = 0;           // 最近一次兜底连接确认成功时间(让位判定窗)
    private long fbReleaseTs = 0;       // 上次让位时间(冷却)

    // 注入的钩子
    private volatile HrCallback hrCallback;
    private volatile Check alarmCheck;
    private volatile Check directCheck;
    private volatile SourceCallback sourceCallback;
    private volatile BandStatusCallback bandStatusCallback;
    private volatile LocalBridge localBridge;
    private String lastSourceSig = null;
    private String lastBandSig = null;
    private boolean warnedNoMac = false;   // 未配置手环的告警只打一次(防仲裁循环刷日志)

    private static final RelayHub sInstance = new RelayHub();

    public static RelayHub get() {
        return sInstance;
    }

    private RelayHub() {
    }

    /** 单个节点条目(字段对齐 relay_hub.py _nodes dict) */
    private static final class Node {
        NodeSocket ws;                 // WS连接; null=离线
        String ip = "";
        String fw = "";
        String state = "idle";         // idle/connecting/active/offline
        double rssiEma = 0;
        double connRssi = 0;
        long lastSeen = 0;
        long lastDataTs = 0;
        long lastEvtTs = 0;
        long bandHeardTs = 0;          // 未连接时最近听到手环广播时刻
        long connectedAt = 0;
        long connectDeadline = 0;
        int failCount = 0;
        long lastFailTs = 0;
        long lastHrTs = 0;
        // hb 遥测
        int advDelta = 0;
        boolean advLogged = false;
        boolean quiesce = false;       // 供adv=0诊断区分"待命静默"与"射频聋"
        int scanOn = -1;
        int scanFail = 0;
        boolean scWarned = false;
        long advTotal = -1;
        long atLogTs = 0;
    }

    /** 单个节点 WS 连接(name 在 hello 确认后写入; 回调在该连接的读线程执行) */
    private class NodeSocket extends NanoWSD.WebSocket {
        volatile String name = null;   // 已确认的节点名(hello 前 null)
        volatile long lastAliveMs = System.currentTimeMillis();
        final String peer;             // 对端IP(构造时取定, 供节点表展示)

        NodeSocket(NanoHTTPD.IHTTPSession handshake) {
            super(handshake);
            String p;
            try {
                p = handshake.getRemoteIpAddress();
            } catch (Exception e) {
                p = null;
            }
            peer = p == null ? "" : p;
        }

        @Override
        protected void onOpen() {
            // 等 hello 再登记(对齐 py async-for 内首个 hello 才入表)
        }

        @Override
        protected void onMessage(NanoWSD.WebSocketFrame message) {
            lastAliveMs = System.currentTimeMillis();
            if (message == null) return;
            String text;
            try {
                text = message.getTextPayload();
            } catch (Exception e) {
                return;
            }
            if (text == null || text.isEmpty()) return;
            JSONObject data;
            try {
                data = new JSONObject(text);
            } catch (JSONException e) {
                return;
            }
            try {
                dispatch(this, data);
            } catch (Exception e) {
                Log.e(TAG, "RelayHub 消息处理异常: " + e);
            }
        }

        @Override
        protected void onPong(NanoWSD.WebSocketFrame pong) {
            lastAliveMs = System.currentTimeMillis();
        }

        @Override
        protected void onClose(NanoWSD.WebSocketFrame.CloseCode code, String reason, boolean initiatedByRemote) {
            nodeOffline(this);
        }

        @Override
        protected void onException(IOException exception) {
            nodeOffline(this);
        }
    }

    // ================================ 生命周期 ================================

    /** 注册统一心率入口(中继与直连同一下游 HeartBus) */
    public void setHrCallback(HrCallback fn) {
        hrCallback = fn;
    }

    /** 注册报警状态探测 */
    public void setAlarmCheck(Check fn) {
        alarmCheck = fn;
    }

    /** 注册本机直连状态探测(板载BLE.isConnected) */
    public void setDirectCheck(Check fn) {
        directCheck = fn;
    }

    /** 注册数据源状态变化回调(phase/source/target/rssi JSON) */
    public void setSourceCallback(SourceCallback fn) {
        sourceCallback = fn;
    }

    /** 注册手环三态变化回调(ok/nodata/lost JSON) */
    public void setBandStatusCallback(BandStatusCallback fn) {
        bandStatusCallback = fn;
        lastBandSig = null;
        notifyBandStatus();   // 注册时立即补发当前状态(对齐 py)
    }

    /** 注入本机BLE命令桥(兜底 connect/disconnect) */
    public void setLocalBridge(LocalBridge bridge) {
        localBridge = bridge;
    }

    /** 启动中继服务(幂等); Prefs RELAY_ENABLED=true 时由 HeartRateService 触发 */
    public synchronized boolean start(Context c) {
        if (running) return true;
        app = c.getApplicationContext();
        // 仲裁参数(保存设置后重启服务生效; 默认值对齐 EXE 代码默认)
        token = Prefs.getStr(app, Prefs.RELAY_TOKEN, TOKEN_DEFAULT);
        if (token == null || token.isEmpty()) token = TOKEN_DEFAULT;
        thresholdDrop = Prefs.getInt(app, Prefs.RELAY_THRESHOLD_DROP, -75);
        hysteresisDb = Prefs.getInt(app, Prefs.RELAY_HYSTERESIS_DB, 10);
        minRssi = Prefs.getInt(app, Prefs.RELAY_MIN_RSSI, -80);
        staleSeconds = Prefs.getInt(app, Prefs.RELAY_STALE_SECONDS, 30);
        freezeCycles = Prefs.getInt(app, Prefs.RELAY_FREEZE_CYCLES, 3);
        loadBiases();

        synchronized (lock) {
            nodes.clear();
            activeNode = null;
            pendingConnect = null;
            probeStage = null;
            probeOldName = null;
            noBetterHold = false;
            fbLostStreak = 0;
            fbHeardStreak = 0;
            warnedNoMac = false;
        }
        net = java.util.concurrent.Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "relay-net");
            t.setDaemon(true);
            return t;
        });
        // 心跳探测(对齐 aiohttp heartbeat=30): 30s ping; 75s 无任何活动判定死链(半开TCP探测)
        net.scheduleWithFixedDelay(() -> {
            try {
                pingAll();
            } catch (Exception e) {
                Log.w(TAG, "RelayHub 心跳探测异常: " + e);
            }
        }, 30, 30, java.util.concurrent.TimeUnit.SECONDS);
        // 独立监听 0.0.0.0:8899(ESP32 节点在局域网无 Tailscale, 必须与数据服务分开绑定;
        // 端口对齐 EXE, mDNS 通告同端口)。读超时 0: WS 长连接, 死链靠 30s ping 探测
        try {
            RelayWs ws = new RelayWs();
            ws.start(0, true);
            relayWs = ws;
        } catch (java.io.IOException e) {
            Log.e(TAG, "中继端口 " + RELAY_PORT + " 监听失败: " + e.getMessage());
            relayWs = null;
        }
        registerNsd(app);
        running = true;
        arbiter = new Thread(() -> {
            while (running) {
                try {
                    Thread.sleep(bandLostNow() ? ARB_INTERVAL_LOST_MS : ARB_INTERVAL_MS);
                    decide();
                } catch (InterruptedException e) {
                    break;
                } catch (Exception e) {
                    Log.e(TAG, "RelayHub 仲裁异常: " + e);
                }
            }
        }, "relay-hub");
        arbiter.setDaemon(true);
        arbiter.start();
        Log.i(TAG, "RelayHub 已启动: /relay (token鉴权), 仲裁周期"
                + ARB_INTERVAL_MS / 1000 + "s(失联" + ARB_INTERVAL_LOST_MS / 1000 + "s)");
        return true;
    }

    /** 停止中继服务(断开全部节点) */
    public synchronized void stop() {
        running = false;
        unregisterNsd();
        RelayWs ws = relayWs;
        relayWs = null;
        if (ws != null) {
            try {
                ws.stop();
            } catch (Exception e) {
                Log.w(TAG, "关闭中继监听异常: " + e.getMessage());
            }
        }
        Thread a = arbiter;
        arbiter = null;
        if (a != null) a.interrupt();
        java.util.concurrent.ScheduledExecutorService n = net;
        net = null;
        if (n != null) {
            n.shutdown();
            try {
                if (!n.awaitTermination(2, java.util.concurrent.TimeUnit.SECONDS)) n.shutdownNow();
            } catch (InterruptedException e) {
                n.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }
        synchronized (lock) {
            for (Node node : nodes.values()) {
                final NodeSocket s = node.ws;
                if (s != null) {
                    try {
                        s.close(NanoWSD.WebSocketFrame.CloseCode.GoingAway, "hub stopped", false);
                    } catch (Exception ignore) {
                    }
                }
            }
            nodes.clear();
            activeNode = null;
            pendingConnect = null;
            probeStage = null;
            probeOldName = null;
            noBetterHold = false;
            scanResults.clear();
        }
        Log.i(TAG, "RelayHub 已停止");
    }

    public boolean isRunning() {
        return running;
    }

    /** 节点在线/总数（大屏"中继节点"卡片用; online=WS 连接存活） */
    public int[] nodeCounts() {
        int online = 0;
        synchronized (lock) {
            for (Node node : nodes.values()) {
                if (node.ws != null) online++;
            }
            return new int[]{online, nodes.size()};
        }
    }

    /**
     * 中继节点专用 WS 服务: 独立于数据服务绑定 0.0.0.0:8899(ESP32 全在局域网, 无 Tailscale;
     * 端口对齐 EXE 8899)。仅 /relay 路径允许升级, token 鉴权失败/其他请求一律 401。
     */
    private static final class RelayWs extends NanoWSD {
        RelayWs() {
            super("0.0.0.0", RELAY_PORT);
        }

        @Override
        protected boolean isWebsocketRequested(NanoHTTPD.IHTTPSession session) {
            return "/relay".equals(session.getUri()) && RelayHub.get().checkToken(session);
        }

        @Override
        protected NanoWSD.WebSocket openWebSocket(NanoHTTPD.IHTTPSession handshake) {
            return RelayHub.get().newNodeSocket(handshake);
        }

        @Override
        protected NanoHTTPD.Response serveHttp(NanoHTTPD.IHTTPSession session) {
            // token 缺失/错误的升级请求, 或纯 HTTP 访问(对齐 EXE 8899 未带 token 的行为)
            return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.UNAUTHORIZED,
                    "text/plain; charset=utf-8", "401 Unauthorized");
        }
    }

    /**
     * 经 NsdManager 通告 _hrmlink._tcp:8899, ESP32 节点 MDNS.queryService 自动发现
     * (换中枢/换板子零重配; EXE 侧由 python-zeroconf 通告同名服务, 二选一部署不冲突)。
     * mDNS 应答按请求接口回对应网卡的 LAN IP, 节点发现的必然是局域网地址。
     */
    private void registerNsd(Context c) {
        if (nsdListener != null) return;   // 幂等(重复 start 防御)
        try {
            nsd = (android.net.nsd.NsdManager) c.getSystemService(Context.NSD_SERVICE);
            android.net.nsd.NsdServiceInfo info = new android.net.nsd.NsdServiceInfo();
            info.setServiceName("HRMLink-Hub");
            info.setServiceType("_hrmlink._tcp.");
            info.setPort(RELAY_PORT);
            nsdListener = new android.net.nsd.NsdManager.RegistrationListener() {
                @Override
                public void onServiceRegistered(android.net.nsd.NsdServiceInfo si) {
                    Log.i(TAG, "mDNS 已注册: " + si.getServiceName() + " (_hrmlink._tcp:" + RELAY_PORT + ")");
                }

                @Override
                public void onRegistrationFailed(android.net.nsd.NsdServiceInfo si, int err) {
                    Log.w(TAG, "mDNS 注册失败: err=" + err + "(节点可手动填中枢IP, 不影响中继功能)");
                }

                @Override
                public void onServiceUnregistered(android.net.nsd.NsdServiceInfo si) {
                }

                @Override
                public void onUnregistrationFailed(android.net.nsd.NsdServiceInfo si, int err) {
                    Log.w(TAG, "mDNS 注销失败: err=" + err);
                }
            };
            nsd.registerService(info, android.net.nsd.NsdManager.PROTOCOL_DNS_SD, nsdListener);
        } catch (Exception e) {
            Log.w(TAG, "mDNS 注册异常: " + e);
            nsdListener = null;
            nsd = null;
        }
    }

    /** 停止 mDNS 通告(与 RelayHub 生命周期对称) */
    private void unregisterNsd() {
        if (nsd != null && nsdListener != null) {
            try {
                nsd.unregisterService(nsdListener);
            } catch (Exception ignore) {
            }
        }
        nsdListener = null;
        nsd = null;
    }

    /**
     * /relay 升级请求 token 鉴权(RelayWs 在 isWebsocketRequested 阶段调用):
     * Authorization: Bearer xxx 或 ?token=xxx(对齐 py _ws_handler)。
     */
    public boolean checkToken(NanoHTTPD.IHTTPSession session) {
        if (!running) return false;
        String auth = session.getHeaders().get("authorization");
        if (("Bearer " + token).equals(auth)) return true;
        String q = session.getQueryParameterString();
        if (q != null) {
            for (String kv : q.split("&")) {
                int i = kv.indexOf('=');
                if (i > 0 && "token".equals(kv.substring(0, i))) {
                    try {
                        if (token.equals(java.net.URLDecoder.decode(kv.substring(i + 1), "UTF-8"))) {
                            return true;
                        }
                    } catch (Exception ignore) {
                    }
                }
            }
        }
        return false;
    }

    /** HeartServer openWebSocket(/relay) 入口: 创建节点 Socket */
    public NodeSocket newNodeSocket(NanoHTTPD.IHTTPSession handshake) {
        return new NodeSocket(handshake);
    }

    // ================================ 消息分发 ================================

    /** 消息分发(连接读线程); hello 确认后写入 socket.name */
    private void dispatch(NodeSocket wsr, JSONObject data) {
        String mtype = data.optString("type", "");
        if (mtype.equals("hello")) {
            String nname = data.optString("name", "").trim();
            if (nname.length() > 24) nname = nname.substring(0, 24);
            if (nname.isEmpty()) nname = "unnamed";
            String peer = wsr.peer;   // 构造时取定的对端IP
            boolean renamed = false;
            Node prev = null;
            synchronized (lock) {
                Node old = nodes.get(nname);
                if (old != null && old.ws != null && old.ws != wsr && old.ws.isOpen()) {
                    // 重名冲突: 拒绝新节点(旧条目ws已死则允许接管, 异常断线自恢复)
                    final NodeSocket reject = wsr;
                    netSend(() -> {
                        safeSend(reject, "{\"type\":\"evt\",\"evt\":\"name_conflict\"}");
                        try {
                            reject.close(NanoWSD.WebSocketFrame.CloseCode.NormalClosure,
                                    "name conflict", false);
                        } catch (Exception ignore) {
                        }
                    });
                    return;
                }
                // 同连接改名换绑(rename_node下发set后固件补发hello): 迁移统计与持有状态, 删旧名防僵尸
                if (wsr.name != null && !wsr.name.equals(nname)) {
                    Node stale = nodes.get(wsr.name);
                    if (stale != null && stale.ws == wsr) {
                        nodes.remove(wsr.name);
                        prev = stale;
                        renamed = true;
                    }
                }
                Node entry;
                if (old != null && old.ws == wsr) {
                    entry = old;               // 同连接重复hello: 保留原条目
                } else if (prev != null) {
                    entry = prev;              // 改名: 保留统计与持有状态
                } else {
                    entry = new Node();
                    entry.ip = peer;
                    entry.state = "idle";
                    entry.lastSeen = nowS();
                    entry.lastEvtTs = nowS();
                }
                entry.ws = wsr;
                entry.ip = peer;
                entry.fw = data.optString("fw", "");
                entry.lastSeen = nowS();
                entry.lastEvtTs = nowS();
                nodes.put(nname, entry);
                if (renamed && wsr.name != null) {
                    if (pendingConnect != null && pendingConnect.equals(wsr.name)) {
                        pendingConnect = nname;
                    }
                    if (activeNode != null && activeNode.equals(wsr.name)) {
                        activeNode = nname;    // 改名后保留持有权
                    }
                }
            }
            Log.i(TAG, "RelayHub 节点上线: " + nname + " (" + peer + ", fw=" + data.optString("fw", "")
                    + (renamed ? " (由 " + wsr.name + " 改名)" : "") + ")");
            wsr.name = nname;
            sendToName(nname, cfgCmd());
            return;
        }

        if (wsr.name == null) return;   // hello 前的其余消息一律忽略(对齐 py if not name: return)
        String name = wsr.name;
        Node node;
        synchronized (lock) {
            node = nodes.get(name);
        }
        if (node == null || node.ws != wsr) return;
        node.lastSeen = nowS();

        switch (mtype) {
            case "hb":
                onHb(node, name, wsr, data);
                break;
            case "hr":
                onHr(node, name, data);
                break;
            case "scan": {
                int r = data.optInt("rssi", 0);
                if (r < 0) {
                    synchronized (lock) {
                        scanResults.put(name, r);   // 探测窗采样(标定P1不移植)
                    }
                }
                break;
            }
            case "evt":
                onEvt(node, name, wsr, data);
                break;
            default:
                break;
        }
    }

    /**
     * hello 确认后首条回执: 下发手环MAC(对齐 py _send_cfg)。
     * 节点收到后写入NVS, 扫描/连接目标全靠它; MAC为空时省略字段
     * (此时 issueConnect 处会拦截派活并告警)。
     */
    private JSONObject cfgCmd() {
        JSONObject o = cmd("cfg");
        String mac = bandMac();
        try {
            if (!mac.isEmpty()) o.put("mac", mac);
        } catch (JSONException ignore) {
        }
        return o;
    }

    /** hb 心跳: 状态/RSSI平滑/广播遥测/扫描遥测 + hb_ack{q}(待命标志) */
    private void onHb(Node node, String name, NodeSocket wsr, JSONObject data) {
        int r = data.optInt("rssi", 0);
        String st = data.optString("state", "idle");
        long nowMs = System.currentTimeMillis();
        boolean notify;
        int q;
        synchronized (lock) {
            node.state = st.isEmpty() ? node.state : st;
            node.lastEvtTs = nowS();
            int adv = data.optInt("adv", 0);
            if (data.has("adv")) {
                node.advDelta = adv;
                // 沿触发日志: 开始听到/从听到变聋 各打一条, 持续听到的节点不刷屏
                if (adv > 0 && !node.advLogged) {
                    node.advLogged = true;
                    Log.i(TAG, "RelayHub 节点 " + name + " 听到BLE广播: " + adv + "包/2s (hb.rssi=" + r + ")");
                } else if (adv == 0 && node.advLogged) {
                    node.advLogged = false;
                    if (!node.quiesce) {   // 2%待命期adv=0是常态, 非射频聋(P3-15)
                        Log.w(TAG, "RelayHub 节点 " + name + " BLE广播消失 (adv=0, hb.rssi=" + r + ")");
                    }
                }
            }
            if (r < 0) {
                double ema = node.rssiEma;
                node.rssiEma = ema == 0 ? r : ema * (1 - EMA_ALPHA) + r * EMA_ALPHA;
                if (node.state.equals("active")) {
                    node.connRssi = r;
                    node.lastDataTs = nowS();
                } else {
                    node.bandHeardTs = nowS();   // 未连接时hb.rssi<0=最近听到手环广播
                }
            }
            // BLE扫描状态遥测: sc=0且非active态=扫描真挂了
            if (data.has("sc")) {
                node.scanOn = data.optInt("sc", 0);
                node.scanFail = data.optInt("sf", 0);
                if (node.scanOn == 0 && !node.state.equals("active") && !node.scWarned) {
                    node.scWarned = true;
                    Log.w(TAG, "RelayHub 节点 " + name + " BLE扫描未运行(sc=0 sf=" + node.scanFail + ")!");
                } else if (node.scanOn == 1 && node.scWarned) {
                    node.scWarned = false;
                    Log.i(TAG, "RelayHub 节点 " + name + " BLE扫描已恢复");
                }
            }
            // 广播包累计(at=启动以来广播包总数, 首条hb≈纯BLE基线期包数)
            if (data.has("at")) {
                int at = data.optInt("at", 0);
                boolean first = node.advTotal < 0;
                node.advTotal = at;
                if (first || nowMs - node.atLogTs > 60_000) {
                    node.atLogTs = nowMs;
                    Log.i(TAG, "RelayHub 节点 " + name + " BLE广播累计: " + at + "包"
                            + (first ? " (启动基线,含60s纯BLE期)" : ""));
                }
            }
            notify = true;
            // hb_ack: q=1=手环被正常持有 → 空闲节点2%占空比待命(固件仅在idle非探测窗应用)
            q = 0;
            Node an = activeNode == null ? null : nodes.get(activeNode);
            if (an != null && an.state.equals("active") && probeStage == null) q = 1;
            node.quiesce = q == 1;
        }
        if (notify) notifyBandStatus();   // hb每2s评估手环三态(签名去重,无变化零开销)
        final NodeSocket sock = wsr;
        final int qf = q;
        netSend(() -> safeSend(sock, "{\"type\":\"hb_ack\",\"q\":" + qf + "}"));
    }

    /** hr 心率: 0.9s节流 + active归属 + 包率统计 + 统一回调入口 */
    private void onHr(Node node, String name, JSONObject data) {
        int bpm = data.optInt("bpm", 0);
        if (bpm <= 0 || bpm > 250) return;
        boolean fire = false;
        synchronized (lock) {
            int r = data.optInt("rssi", 0);
            node.connRssi = r != 0 ? r : node.connRssi;
            node.lastDataTs = nowS();
            node.lastEvtTs = nowS();
            if (activeNode == null || activeNode.equals(name)) {
                activeNode = name;
            }
            if (activeNode.equals(name)) {
                hrRate.add(System.currentTimeMillis());   // 包率统计(节流前记录, 真实到达率)
            }
            if (System.currentTimeMillis() - node.lastHrTs >= HR_MIN_INTERVAL_MS) {
                node.lastHrTs = System.currentTimeMillis();
                fire = true;
            }
        }
        if (fire) {
            notifyBandStatus();   // 心率到货 → 立即转"正常"态
            HrCallback cb = hrCallback;
            if (cb != null) {
                try {
                    // 时间戳与直连同格式(字符串), 避免下游按str处理时显示epoch数字
                    cb.onHr(now(), bpm);
                } catch (Exception e) {
                    Log.e(TAG, "RelayHub 心率回调异常: " + e);
                }
            }
        }
    }

    /** evt 事件: ble_up/heard/ble_down/ble_fail(未知事件仅刷新 last_evt_ts) */
    private void onEvt(Node node, String name, NodeSocket wsr, JSONObject data) {
        String evt = data.optString("evt", "");
        boolean notify = true;
        synchronized (lock) {
            node.lastEvtTs = nowS();
            if (evt.equals("ble_up")) {
                node.state = "active";
                node.connectedAt = nowS();
                node.connectDeadline = 0;
                String oldPending = pendingConnect;
                activeNode = name;
                pendingConnect = null;
                lowStreak = 0;
                node.failCount = 0;      // 连接成功: P1-2失败降权清零
                node.lastFailTs = 0;
                hrRate.clear();          // 新持有者: 包率窗口重建(warmup期保护)
                hrLowStreak = 0;
                node.lastHrTs = 0;       // 首条hr立即过节流, 状态条先显"无数据"再转正常
                // 自愈回连抢先上手环: 在途派活的节点立即撤回, 防双持有抖动(手环单连接)
                if (oldPending != null && !oldPending.equals(name)) {
                    Log.i(TAG, "RelayHub " + name + " 已持有手环, 撤销在途派活 " + oldPending);
                    sendToName(oldPending, cmd("disconnect"));
                }
            } else if (evt.equals("heard")) {
                // idle节点"重新听到手环广播"边沿即时上报: 断联接管/回家发现不再等2s心跳
                int r = data.optInt("rssi", 0);
                node.bandHeardTs = nowS();
                if (r < 0) {
                    node.rssiEma = node.rssiEma == 0 ? r
                            : node.rssiEma * (1 - EMA_ALPHA) + r * EMA_ALPHA;
                }
                boolean anyConn = anyConnectingLocked();
                if ((activeNode == null || activeNode.isEmpty()) && !anyConn) {
                    netSend(this::decide);   // 手环无人持有: 立即评估派活, 不等下一仲裁周期
                }
            } else if (evt.equals("ble_down")) {
                node.state = "idle";
                node.connRssi = 0;
                if (activeNode != null && activeNode.equals(name)) {
                    Log.i(TAG, "RelayHub 节点 " + name + " 手环断开, 连接权回收");
                    activeNode = null;
                    noBetterHold = false;    // 连接权回收, 保持模式随之解除(重新仲裁)
                }
            } else if (evt.equals("ble_fail")) {
                node.state = "idle";
                node.failCount++;
                node.lastFailTs = nowS();    // P1-2: 降权冷却起点
                node.connectDeadline = 0;
                node.connRssi = 0;           // 清残留: 防保持模式/迟滞判定吃陈旧读数
                if (pendingConnect != null && pendingConnect.equals(name)) {
                    pendingConnect = null;
                }
                if (activeNode != null && activeNode.equals(name)) {
                    // 持有者连接失败=持有权实际已空: 回收+解除保持模式, 让空闲申请按当前
                    // 广播EMA重新派活。否则_decide会在保持模式处每周期提前return, 仲裁器锁死
                    activeNode = null;
                    noBetterHold = false;
                    lowStreak = 0;
                    hrLowStreak = 0;
                }
                Log.w(TAG, "RelayHub 节点 " + name + " 连接手环失败(" + node.failCount
                        + "次 原因:" + data.optString("why", "?") + ")");
            }
        }
        if (notify) notifyBandStatus();
    }

    /** 节点WS断开: 放手规则——立即回收连接权(防占坑) */
    private void nodeOffline(NodeSocket wsr) {
        String name = wsr.name;
        if (name == null) return;
        boolean notify = false;
        synchronized (lock) {
            Node node = nodes.get(name);
            if (node != null && node.ws == wsr) {
                node.ws = null;
                node.state = "offline";
                node.connectDeadline = 0;
                if (activeNode != null && activeNode.equals(name)) {
                    activeNode = null;
                    Log.i(TAG, "RelayHub 节点 " + name + " 失联, 连接权回收");
                }
                if (pendingConnect != null && pendingConnect.equals(name)) {
                    pendingConnect = null;
                }
                notify = true;
            }
        }
        if (notify) notifyBandStatus();   // 节点掉线可能改变手环可见性
    }

    // ================================ 三态/数据源状态 ================================

    /** 手环失联快速判定(P1-6: lost期间仲裁周期2s→1s) */
    private boolean bandLostNow() {
        try {
            JSONObject st = bandStatus();
            return "lost".equals(st.optString("state"));
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 手环三态(状态条/日志用): ok=已连接且心率正常; nodata=有信号无心率(已连接无数据=可能取下充电/
     * 未佩戴; 仅听到广播=未连接); lost=所有节点均搜索不到(外出/关机/被本机占用)。
     * 本机直连优先: 手环被本机占用时节点按互斥规则已释放且手环停止广播, 节点侧必然"全聋",
     * 不能据此汇报失联。
     */
    public JSONObject bandStatus() {
        if (directNow()) {
            return st("ok", "本机直连", 0, null);
        }
        long now = nowS();
        synchronized (lock) {
            Node n = activeNode == null ? null : nodes.get(activeNode);
            if (n != null && n.state.equals("active")) {
                if (now - n.lastHrTs <= HR_NODATA_S) {
                    return st("ok", activeNode, (int) n.connRssi, null);
                }
                return st("nodata", activeNode, (int) n.connRssi, "已连接但无心率数据(可能取下充电/未佩戴)");
            }
            String bestName = null;
            double bestEma = -999;
            for (Map.Entry<String, Node> e : nodes.entrySet()) {
                Node nd = e.getValue();
                if (now - nd.bandHeardTs <= BAND_HEARD_WINDOW_S && nd.rssiEma > bestEma) {
                    bestEma = nd.rssiEma;
                    bestName = e.getKey();
                }
            }
            if (bestName != null) {
                return st("nodata", bestName, (int) bestEma, "已搜索到手环但未连接");
            }
        }
        return st("lost", "", 0, "所有节点均未搜索到手环");
    }

    private static JSONObject st(String state, String node, int rssi, String reason) {
        JSONObject o = new JSONObject();
        try {
            o.put("state", state);
            o.put("node", node == null ? "" : node);
            o.put("rssi", rssi);
            if (reason != null) o.put("reason", reason);
        } catch (JSONException ignore) {
        }
        return o;
    }

    /** 手环三态变化推送(签名去重); 转入失聪即广播wake, 2%待命的空闲节点立即恢复全速 */
    private void notifyBandStatus() {
        JSONObject st;
        try {
            st = bandStatus();
        } catch (Exception e) {
            Log.e(TAG, "RelayHub 手环状态评估异常: " + e);
            return;
        }
        String sig = st.optString("state") + "|" + st.optString("node") + "|"
                + st.optString("reason", "");
        synchronized (sendLock) {
            if (sig.equals(lastBandSig)) return;
            lastBandSig = sig;
        }
        if ("lost".equals(st.optString("state"))) {
            try {
                wakeIdleNodes();
            } catch (Exception e) {
                Log.w(TAG, "RelayHub 扫描令下发失败: " + e);
            }
        }
        BandStatusCallback cb = bandStatusCallback;
        if (cb != null) {
            try {
                cb.onBandStatus(st);
            } catch (Exception e) {
                Log.e(TAG, "RelayHub 手环状态回调异常: " + e);
            }
        }
    }

    /**
     * 当前心率数据源状态(接收端通知栏显示用):
     * {"phase": active|switching|direct|none, "source": 名字, "target": 切换目标(仅switching), "rssi"}
     */
    public JSONObject sourceStatus() {
        if (directNow()) {
            JSONObject o = new JSONObject();
            try {
                o.put("phase", "direct");
                o.put("source", "本机");
                o.put("rssi", 0);
            } catch (JSONException ignore) {
            }
            return o;
        }
        long now = nowS();
        boolean probeLive;
        boolean connectingLive;
        String pending = null;
        synchronized (lock) {
            // 目标节点必须真的处于connecting态才报switching(防冻结滞留误报, v1.2.15兜底校验);
            // probe_stage同样只在其时限内可信——节点全下线时_decide在"无节点"处早退不再推进
            // 探测状态机, probe_stage会冻结, 没有deadline兜底接收端会永远显示"切换中"
            probeLive = probeStage != null && now <= probeDeadline + 30;
            Node pc = pendingConnect == null ? null : nodes.get(pendingConnect);
            connectingLive = pc != null && "connecting".equals(pc.state);
            pending = pendingConnect;
        }
        if (probeLive || connectingLive) {
            JSONObject o = new JSONObject();
            try {
                o.put("phase", "switching");
                o.put("source", probeOldName == null ? (activeNode == null ? "" : activeNode) : probeOldName);
                o.put("target", pending == null ? "" : pending);
                o.put("rssi", 0);
            } catch (JSONException ignore) {
            }
            return o;
        }
        synchronized (lock) {
            Node n = activeNode == null ? null : nodes.get(activeNode);
            if (n != null && n.state.equals("active")) {
                JSONObject o = new JSONObject();
                try {
                    o.put("phase", "active");
                    o.put("source", activeNode);
                    o.put("rssi", (int) n.connRssi);
                } catch (JSONException ignore) {
                }
                return o;
            }
        }
        JSONObject o = new JSONObject();
        try {
            o.put("phase", "none");
            o.put("source", "");
            o.put("rssi", 0);
        } catch (JSONException ignore) {
        }
        return o;
    }

    /** 数据源状态变化推送(签名去重; rssi按5dB分档, 防通知抖动) */
    private void notifySource() {
        JSONObject st = sourceStatus();
        int rssi = st.optInt("rssi", 0);
        String sig = st.optString("phase") + "|" + st.optString("source") + "|"
                + st.optString("target", "") + "|" + (rssi != 0 ? rssi / 5 : 0);
        synchronized (sendLock) {
            if (sig.equals(lastSourceSig)) return;
            lastSourceSig = sig;
        }
        SourceCallback cb = sourceCallback;
        if (cb != null) {
            try {
                cb.onSource(st);
            } catch (Exception e) {
                Log.e(TAG, "RelayHub 数据源回调异常: " + e);
            }
        }
    }

    // ================================ 仲裁决策 ================================

    private boolean directNow() {
        Check fn = directCheck;
        try {
            return fn != null && fn.check();
        } catch (Exception e) {
            return false;
        }
    }

    private boolean alarmNow() {
        Check fn = alarmCheck;
        try {
            return fn != null && fn.check();
        } catch (Exception e) {
            return false;
        }
    }

    /** 是否有节点处于连接进行中(deadline未过期) */
    private boolean anyConnectingLocked() {
        long now = nowS();
        for (Node n : nodes.values()) {
            if ("connecting".equals(n.state) && n.connectDeadline > now) return true;
        }
        return false;
    }

    /**
     * 空闲节点中选校准RSSI最强者(失败次数少优先)。
     * 双轨规则: min_rssi门槛用原始rssi_ema, 排名比较用校准值 raw - bias。
     * P1-2降权: 30s内失败≥2次的节点暂缓派活(信号边缘反复失败的刹车); 全员冷却时放宽。
     * 在场校验(v1.2.15): 必须窗口内听到过手环广播才派活——rssi_ema永不衰减,
     * 手环外出/关机后残值会驱动对"聋"节点循环下发注定失败的connect(18s一轮),
     * pending_connect常年connecting → 接收端永远显示"切换中"(中枢侧却报失联)。
     */
    private String bestIdle() {
        long now = nowS();
        List<Map.Entry<String, Node>> cands = new ArrayList<>();
        for (Map.Entry<String, Node> e : nodes.entrySet()) {
            Node n = e.getValue();
            if ("idle".equals(n.state) && n.ws != null && n.rssiEma < 0
                    && n.rssiEma > minRssi && hearing(n, now) && !cooling(n, now)) {
                cands.add(e);
            }
        }
        if (cands.isEmpty()) {
            // 全员冷却中: 降权是软惩罚, 有数据源比没数据源重要, 放宽冷却再选(在场校验不放宽)
            for (Map.Entry<String, Node> e : nodes.entrySet()) {
                Node n = e.getValue();
                if ("idle".equals(n.state) && n.ws != null && n.rssiEma < 0
                        && n.rssiEma > minRssi && hearing(n, now)) {
                    cands.add(e);
                }
            }
        }
        if (cands.isEmpty()) return null;
        cands.sort((a, b) -> {
            int fa = a.getValue().failCount, fb = b.getValue().failCount;
            if (fa != fb) return Integer.compare(fa, fb);
            int ca = (int) a.getValue().rssiEma - biases.getOrDefault(a.getKey(), 0);
            int cb = (int) b.getValue().rssiEma - biases.getOrDefault(b.getKey(), 0);
            return Integer.compare(cb, ca);   // 校准值强者优先
        });
        return cands.get(0).getKey();
    }

    private boolean hearing(Node n, long now) {
        return now - n.bandHeardTs <= BAND_HEARD_WINDOW_S;
    }

    private boolean cooling(Node n, long now) {
        return n.failCount >= 2 && now - n.lastFailTs < FAIL_COOLDOWN_S;
    }

    /** 下发连接命令(pending_connect/connecting登记; 未配置手环只告警一次) */
    private void issueConnect(String name) {
        String mac = bandMac();
        if (mac == null || mac.isEmpty()) {
            if (!warnedNoMac) {
                warnedNoMac = true;
                Log.w(TAG, "RelayHub 未配置手环(dev_address), 无法下发连接命令");
            }
            return;
        }
        warnedNoMac = false;
        Node node;
        synchronized (lock) {
            node = nodes.get(name);
            if (node == null || node.ws == null) return;
            node.state = "connecting";
            node.connectDeadline = nowS() + CONNECT_DEADLINE_S;
            pendingConnect = name;
        }
        JSONObject cmd = cmd("connect");
        try {
            cmd.put("mac", mac);
        } catch (JSONException ignore) {
        }
        sendToName(name, cmd);
        Log.i(TAG, "RelayHub 下发连接命令 -> " + name + " (mac=" + mac + ")");
    }

    private String bandMac() {
        if (app == null) return "";
        return Prefs.getStr(app, Prefs.DEV_ADDRESS, "").trim();
    }

    /** 仲裁器主决策(每2秒/失联1秒一次; 时机模型见类注释) */
    private void decide() {
        notifySource();   // 数据源状态变化推送(2s仲裁周期即节流)
        long now = nowS();
        // 僵尸连接清理(必须最先执行, v1.2.15): 标定冻结/探测推进/节点全下线等任何早退路径
        // 都不能让pending_connect滞留, 否则接收端永远显示"切换中"
        synchronized (lock) {
            for (Map.Entry<String, Node> e : nodes.entrySet()) {
                Node n = e.getValue();
                if ("connecting".equals(n.state) && n.ws != null
                        && n.connectDeadline > 0 && n.connectDeadline <= now) {
                    n.state = "idle";
                    Log.w(TAG, "RelayHub 节点 " + e.getKey() + " connect超时未回报, 重置为idle");
                }
            }
            if (pendingConnect != null) {
                Node pn = nodes.get(pendingConnect);
                if (pn == null || !"connecting".equals(pn.state)) {
                    pendingConnect = null;
                }
            }
        }

        // 0) 本机直连互斥: 本机BLE连着手环 → 节点必须放手(永不并存)
        if (directNow()) {
            String an;
            synchronized (lock) {
                an = activeNode;
            }
            if (an != null) {
                sendToName(an, cmd("disconnect"));
                Log.i(TAG, "RelayHub 本机直连生效, 命令节点释放手环");
                synchronized (lock) {
                    if (an.equals(activeNode)) activeNode = null;
                }
            }
            // 兜底让位: 手环是中枢派本机去兜底连的(非手动直连), 且节点已持续重新听到
            // 手环 → 本机放手交还节点(带冷却, 防信号边缘"断连-兜底"循环)
            if (now - fbCmdTs < PC_FB_HOLD_S) {
                boolean heard;
                synchronized (lock) {
                    heard = nodesHeardLocked(now);
                }
                if (heard) {
                    fbHeardStreak++;
                } else {
                    fbHeardStreak = 0;
                }
                if (fbHeardStreak >= PC_FB_HEARD_STREAK
                        && now - fbReleaseTs >= PC_FB_RELEASE_COOLDOWN_S) {
                    Log.i(TAG, "RelayHub 节点已持续" + fbHeardStreak + "周期听到手环, 本机兜底让位(交还节点)");
                    fbReleaseTs = now;
                    fbHeardStreak = 0;
                    LocalBridge b = localBridge;
                    if (b != null) {
                        try {
                            b.onCommand(cmd("disconnect"));
                        } catch (Exception e) {
                            Log.w(TAG, "RelayHub 本机兜底让位命令下发失败: " + e);
                        }
                    }
                }
            }
            return;
        }

        // 0.5) 本机兜底: 全体节点持续失聪(无人持有且窗口内都听不到广播) → 本机BLE连接;
        //      节点恢复后由step0的让位逻辑交还
        if (localBridge != null) {
            boolean lost = "lost".equals(bandStatus().optString("state"));
            if (lost) {
                fbLostStreak++;
            } else {
                fbLostStreak = 0;
            }
            if (fbLostStreak >= PC_FB_AFTER_CYCLES && now - fbLastTry >= PC_FB_RETRY_S) {
                String mac = bandMac();
                if (!mac.isEmpty()) {
                    fbLastTry = now;
                    Log.i(TAG, "RelayHub 全部节点" + fbLostStreak + "周期未听到手环, 本机兜底连接(mac=" + mac + ")");
                    JSONObject cmd = cmd("connect");
                    try {
                        cmd.put("mac", mac);
                    } catch (JSONException ignore) {
                    }
                    try {
                        localBridge.onCommand(cmd);
                    } catch (Exception e) {
                        Log.w(TAG, "RelayHub 本机兜底命令下发失败: " + e);
                    }
                }
            }
        }

        synchronized (lock) {
            if (nodes.isEmpty()) return;

            // 1) 探测状态机推进(不受报警冻结影响的机械推进, 决策点受冻结)
            if ("disconnecting".equals(probeStage)) {
                if (activeNode == null || now > probeDeadline) {
                    // 已断开(或超时): 开窗扫描
                    scanResults.clear();
                    JSONObject scanCmd = cmd("scan");
                    try {
                        scanCmd.put("dur", 1000);
                    } catch (JSONException ignore) {
                    }
                    broadcast(scanCmd);
                    probeStage = "scanning";
                    probeDeadline = now + PROBE_SCAN_S;
                }
                return;
            }
            if ("scanning".equals(probeStage)) {
                if (now > probeDeadline) {
                    resolveProbeLocked();
                }
                return;
            }

            // 2) 无数据源 → 空闲自动申请(连接超时僵尸清理已前移到入口)
            Node active = activeNode == null ? null : nodes.get(activeNode);
            if (active == null || active.ws == null) {
                if (activeNode != null) {
                    activeNode = null;         // 持有者失联, 回收
                }
                lowStreak = 0;
                noBetterHold = false;          // 持有权易主/回收, 保持模式解除
                if (!anyConnectingLocked()) {
                    String best = bestIdle();
                    if (best != null) {
                        issueConnect(best);
                    }
                }
                return;
            }
            final String actName = activeNode;

            // 3) 接管判定: 持有者心跳失联(按心跳而非数据判定, 避免"手环停止测量心率"
            //    时在节点间反复切换)
            if (now - active.lastSeen > staleSeconds) {
                Log.w(TAG, "RelayHub 节点 " + actName + " 超过" + staleSeconds + "s无心跳, 接管");
                sendToName(actName, cmd("disconnect"));
                activeNode = null;
                return;
            }
        }

        // 4) 报警冻结: 只保数据连续, 不做质量切换
        if (alarmNow()) {
            return;
        }

        // 5) 迟滞切换判定: 持有者RSSI跌破阈值连续N个周期
        //    连接进行中不评估: 命令式断开固件不上报ble_down, conn_rssi残留旧值,
        //    此时会用陈旧读数继续累计low_streak并二次触发探测, 打断在途的connect
        boolean probeTriggered = false;
        synchronized (lock) {
            if (pendingConnect != null || anyConnectingLocked()) {
                return;
            }
            Node active = activeNode == null ? null : nodes.get(activeNode);
            if (active == null) return;
            int connRssi = (int) (active.connRssi != 0 ? active.connRssi : active.rssiEma);
            long nowMs = System.currentTimeMillis();

            // 4.5) 心率包率触发(P1-7): RSSI正常但心率流断流(链路卡死/深衰落丢包)。
            //      放在保持模式之前: 包率断流是链路级疾病, 值得打破"RSSI回升才解除"的保持;
            //      探测无更优则回连重开链路自愈。防循环: warmup保护 + 10分钟冷却
            if (connRssi != 0 && connRssi >= thresholdDrop
                    && "active".equals(active.state)
                    && nowMs - hrProbeTs >= HR_PROBE_COOLDOWN_S * 1000
                    && nowS() - active.connectedAt >= HR_RATE_WARMUP_S) {
                long cutoff = System.currentTimeMillis() - HR_RATE_WINDOW_S * 1000;
                hrRate.removeIf(t -> t <= cutoff);
                if (hrRate.size() < HR_RATE_LOW) {
                    hrLowStreak++;
                    if (hrLowStreak >= HR_RATE_STREAK) {
                        Log.i(TAG, "RelayHub 触发探测切换: " + activeNode + " 心率包率过低("
                                + hrRate.size() + "包/" + HR_RATE_WINDOW_S + "s, 期望~60) 连续"
                                + hrLowStreak + "周期");
                        hrProbeTs = nowMs;
                        hrLowStreak = 0;
                        beginProbeLocked(connRssi);
                        probeTriggered = true;
                    }
                } else {
                    hrLowStreak = 0;
                }
            }

            if (!probeTriggered) {
                // 保持模式: 已探测确认当前持有者就是最优, RSSI回升越过阈值前不再因弱信号探测
                // (真正断开走ble_down→空闲申请, 心跳失联走接管, 均不受保持影响)
                if (noBetterHold) {
                    if (connRssi != 0 && connRssi >= thresholdDrop) {
                        noBetterHold = false;
                        Log.i(TAG, "RelayHub 节点 " + activeNode + " 信号回升至" + connRssi + ", 解除保持模式");
                    } else {
                        return;
                    }
                }
                if (connRssi != 0 && connRssi < thresholdDrop) {
                    lowStreak++;
                    if (lowStreak >= Math.max(freezeCycles, 1)) {
                        Log.i(TAG, "RelayHub 触发探测切换: " + activeNode + " rssi=" + connRssi
                                + " 连续" + lowStreak + "周期低于" + thresholdDrop);
                        beginProbeLocked(connRssi);
                        probeTriggered = true;
                    }
                } else {
                    lowStreak = 0;
                }
            }
        }
    }

    /** 进入探测·断开阶段(disconnect 命令经 net 线程发出; 须持锁调用) */
    private void beginProbeLocked(int connRssi) {
        String old = activeNode;
        probeStage = "disconnecting";
        probeDeadline = nowS() + PROBE_DISCONNECT_S;
        probePreRssi = connRssi;
        probeOldName = old;
        if (old != null) sendToName(old, cmd("disconnect"));
    }

    /** 是否有ESP32节点当前窗口内听到手环广播(兜底让位判定用; 须持锁) */
    private boolean nodesHeardLocked(long now) {
        for (Node n : nodes.values()) {
            if (n.ws != null && now - n.bandHeardTs < BAND_HEARD_WINDOW_S
                    && n.rssiEma < 0 && n.rssiEma > minRssi) {
                return true;
            }
        }
        return false;
    }

    /**
     * 探测窗收口(须持锁): 只有"其他节点"强于原持有者hysteresis_db以上才切, 否则回连原持有者
     * 并进入保持模式。双轨规则: min_rssi门槛用原始RSSI, 强弱比较双方都用校准值 raw - bias。
     */
    private void resolveProbeLocked() {
        probeStage = null;
        String oldName = probeOldName;
        int oldCal = probePreRssi != 0 && oldName != null
                ? probePreRssi - biases.getOrDefault(oldName, 0) : 0;
        String bestName = null;
        int bestRaw = 0, bestCal = 0;
        long now = nowS();
        for (Map.Entry<String, Integer> e : scanResults.entrySet()) {
            int r = e.getValue();
            if (r < 0 && r > minRssi) {   // 门槛: 原始链路质量
                // P1-2降权: 30s内失败≥2次的候选暂缓(否则信号边缘会"探测→派活→再失败"循环)
                Node n = nodes.get(e.getKey());
                if (n != null && cooling(n, now)) continue;
                int cal = r - biases.getOrDefault(e.getKey(), 0);
                if (bestName == null || cal > bestCal) {   // 排名: 校准值
                    bestName = e.getKey();
                    bestRaw = r;
                    bestCal = cal;
                }
            }
        }
        scanResults.clear();
        // 候选必须是"别的节点"且校准值强于原持有者hysteresis_db以上才切换
        // (候选=原持有者自己时不可比: 连接态RSSI与广播RSSI量纲差~10dB, 直接比较必然"显著更优",
        //  会自己切自己, 造成断连→重连死循环)
        if (bestName != null && oldName != null && !bestName.equals(oldName)
                && probePreRssi != 0 && bestCal >= oldCal + hysteresisDb) {
            Log.i(TAG, "RelayHub 漫游切换: " + oldName + "(" + probePreRssi + ") -> "
                    + bestName + "(" + bestRaw + ", 校准" + bestCal + ")");
            issueConnect(bestName);
            probeCleanupLocked();
            return;
        }
        if (bestName != null && oldName != null && !bestName.equals(oldName)) {
            Log.i(TAG, "RelayHub 探测无显著更优(候选" + bestName + "校准" + bestCal
                    + " vs 原持有者校准" + oldCal + "), 回连原持有者");
        } else if (bestName != null && bestName.equals(oldName)) {
            Log.i(TAG, "RelayHub 探测确认: 最优仍是原持有者 " + oldName + "(广播校准"
                    + bestCal + "), 回连并进入保持模式");
        } else {
            Log.i(TAG, "RelayHub 探测无候选达标(≥" + minRssi + "), 回连原持有者 "
                    + (oldName == null ? "" : oldName));
        }
        if (oldName != null) issueConnect(oldName);   // 回连原持有者
        // 保持模式: 本次探测已确认现状即最优; 无保持会形成"弱信号→探测→无更优→回连"死循环
        noBetterHold = true;
        probeCleanupLocked();
    }

    /** 探测收尾(v1.2.15): 清origin残值——probe_old_name不清会永远顶着旧来源名, 后续任何
     *  "切换中"显示都误标成早已失效的旧持有者 */
    private void probeCleanupLocked() {
        probeOldName = null;
        probePreRssi = 0;
    }

    // ================================ 对外查询/操作(UI用) ================================

    /** 节点快照列表(UI表格用) */
    public JSONArray nodeTable() {
        JSONArray arr = new JSONArray();
        long now = nowS();
        synchronized (lock) {
            for (Map.Entry<String, Node> e : nodes.entrySet()) {
                Node n = e.getValue();
                JSONObject r = new JSONObject();
                try {
                    r.put("name", e.getKey());
                    r.put("ip", n.ip);
                    r.put("fw", n.fw);
                    r.put("state", n.state);
                    r.put("rssi", "active".equals(n.state) ? (int) n.connRssi : (int) n.rssiEma);
                    r.put("bias", biases.getOrDefault(e.getKey(), 0));
                    r.put("age", (int) (now - n.lastSeen));
                } catch (JSONException ignore) {
                }
                arr.put(r);
            }
        }
        return arr;
    }

    /**
     * 节点改名入口: 下发set命令, 固件存NVS后补发hello → 本hub同连接换绑(不断WS不断BLE)。
     * 返回(是否受理, 提示文案)
     */
    public String[] renameNode(String oldName, String newName) {
        String nn = newName == null ? "" : newName.replace("\"", "").replace("\\", "").trim();
        if (nn.length() > 24) nn = nn.substring(0, 24);
        if (nn.isEmpty()) return new String[]{"false", "名字不能为空(引号会被自动过滤)"};
        String on = oldName == null ? "" : oldName.trim();
        if (nn.equals(on)) return new String[]{"true", "名字未变化"};
        Node node;
        boolean wsUp;
        synchronized (lock) {
            node = nodes.get(on);
            if (node == null) return new String[]{"false", "节点 " + on + " 不存在(可能刚离线, 请刷新)"};
            if (nodes.containsKey(nn) && !nn.equals(on)) {
                return new String[]{"false", "名字 [" + nn + "] 已被占用, 换一个"};
            }
            wsUp = node.ws != null;
        }
        if (!wsUp) {
            return new String[]{"false", "节点离线, 无法远程改名(等节点上线, 或连它的热点走配网页改名)"};
        }
        JSONObject cmd = cmd("set");
        try {
            cmd.put("name", nn);
        } catch (JSONException ignore) {
        }
        sendToName(on, cmd);
        return new String[]{"true", "改名命令已下发, 节点数秒内生效, 列表自动刷新。\n"
                + "若报警视频联动绑定了 [" + on + "], 请同步更新绑定房间。"};
    }

    /** 远程重启单个节点: 下发reboot命令, 固件delay后ESP.restart, 重连自动重新登记 */
    public String[] rebootNode(String name) {
        String n = name == null ? "" : name.trim();
        boolean wsUp;
        synchronized (lock) {
            Node node = nodes.get(n);
            if (node == null) return new String[]{"false", "节点 " + n + " 不存在(可能刚离线, 请刷新)"};
            wsUp = node.ws != null;
        }
        if (!wsUp) {
            return new String[]{"false", "节点离线, 无法远程重启(等节点上线或现场断电重启)"};
        }
        sendToName(n, cmd("reboot"));
        return new String[]{"true", "重启命令已下发到 [" + n + "], 节点数秒内重启。\n"
                + "重启期间该节点短暂离线, 重连后会自动重新登记; "
                + "若它正持有手环, 中枢会自动仲裁切换到其他节点。"};
    }

    /**
     * 删除节点登记(UI用): 仅移除中枢表项, 不关连接不通知固件(对齐 EXE forget 语义)。
     * 在线节点重连/补发 hello 时会被重新登记; 彻底移除需现场断电重启。
     */
    public String[] forgetNode(String name) {
        String n = name == null ? "" : name.trim();
        synchronized (lock) {
            if (nodes.remove(n) == null) {
                return new String[]{"false", "节点 " + n + " 不存在(可能刚离线, 请刷新)"};
            }
        }
        Log.i(TAG, "RelayHub 节点已删除: " + n);
        return new String[]{"true", "已移除 [" + n + "]。\n"
                + "若节点仍在线, 重连后会被重新登记; 彻底移除请现场断电重启该节点。"};
    }

    /** 本机兜底连接结果回报(bridge适配层在手环实际连上后调用, 记录让位判定窗起点) */
    public void fallbackEvt(boolean ok, String why) {
        if (ok) {
            fbCmdTs = nowS();
            Log.i(TAG, "RelayHub 本机兜底连接已建立");
        } else {
            Log.w(TAG, "RelayHub 本机兜底连接失败: " + (why == null || why.isEmpty() ? "?" : why));
        }
    }

    // ================================ 发送/工具 ================================

    /** P3-15 扫描令(wake): 2%待命的空闲节点立即恢复99%全速扫描(只发idle且WS在线的节点) */
    private void wakeIdleNodes() {
        List<String> targets = new ArrayList<>();
        synchronized (lock) {
            for (Map.Entry<String, Node> e : nodes.entrySet()) {
                if ("idle".equals(e.getValue().state) && e.getValue().ws != null) {
                    targets.add(e.getKey());
                }
            }
        }
        for (String nm : targets) {
            sendToName(nm, cmd("wake"));
        }
    }

    private void sendToName(String name, final JSONObject obj) {
        final NodeSocket s;
        synchronized (lock) {
            Node n = name == null ? null : nodes.get(name);
            s = n == null ? null : n.ws;
        }
        if (s == null) return;
        netSend(() -> safeSend(s, obj.toString()));
    }

    private void broadcast(final JSONObject obj) {
        List<NodeSocket> socks = new ArrayList<>();
        synchronized (lock) {
            for (Node n : nodes.values()) {
                if (n.ws != null) socks.add(n.ws);
            }
        }
        for (final NodeSocket s : socks) {
            netSend(() -> safeSend(s, obj.toString()));
        }
    }

    /** 所有发送统一经 net 单线程串行执行(NanoWSD send 同步 IO, 防交错/防阻塞调用方) */
    private void netSend(Runnable r) {
        java.util.concurrent.ScheduledExecutorService n = net;
        if (n == null || n.isShutdown()) return;
        try {
            n.execute(r);
        } catch (Exception ignore) {
        }
    }

    private void safeSend(NodeSocket ws, String json) {
        try {
            if (ws != null && ws.isOpen()) {
                ws.send(json);
            }
        } catch (Exception ignore) {
        }
    }

    private void pingAll() {
        List<NodeSocket> socks = new ArrayList<>();
        synchronized (lock) {
            for (Node n : nodes.values()) {
                if (n.ws != null) socks.add(n.ws);
            }
        }
        long now = System.currentTimeMillis();
        for (final NodeSocket s : socks) {
            if (now - s.lastAliveMs > 75_000) {   // 2.5个心跳周期无活动=死链
                netSend(() -> {
                    try {
                        s.close(NanoWSD.WebSocketFrame.CloseCode.GoingAway, "ping timeout", false);
                    } catch (Exception ignore) {
                    }
                });
                continue;
            }
            netSend(() -> {
                try {
                    s.ping(new byte[0]);
                } catch (Exception ignore) {
                }
            });
        }
    }

    private static JSONObject cmd(String type) {
        JSONObject o = new JSONObject();
        try {
            o.put("type", type);
        } catch (JSONException ignore) {
        }
        return o;
    }

    /** 节点偏差载入(对齐 py _load_biases: JSON {name: int dB}) */
    private void loadBiases() {
        biases.clear();
        try {
            String raw = Prefs.getStr(app, Prefs.RELAY_NODE_BIASES, "");
            if (raw == null || raw.isEmpty()) return;
            JSONObject d = new JSONObject(raw);
            java.util.Iterator<String> it = d.keys();
            while (it.hasNext()) {
                String k = it.next();
                Object v = d.opt(k);
                if (v instanceof Number) {
                    biases.put(k, (int) Math.round(((Number) v).doubleValue()));
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "RelayHub 节点偏差载入失败, 已清零: " + e);
            biases.clear();
        }
    }

    private static long nowS() {
        return System.currentTimeMillis() / 1000;
    }

    private static String now() {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(new Date());
    }
}
