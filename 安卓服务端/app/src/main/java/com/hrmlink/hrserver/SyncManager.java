package com.hrmlink.hrserver;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.Arrays;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 与 EXE 的双向参数同步（方案: HRMLink功能业务逻辑梳理.md §6.5, 2026-09-30 共存拍板恢复）
 *
 * 原理:
 * - 发现: NsdManager 浏览 _hrmlink._tcp, 排除本端注册名(HRMLink-Hub)与本机IP后取对端;
 *   HTTP 端点 = 对端IP + 本机 srv_port(两端同为 8765 口径)
 * - 通道: GET  /api/settings = 全量快照(对账); POST /api/settings = 接收同步包;
 *   Bearer token 鉴权(sync_token, 两端各存同值)
 * - 冲突: LWW —— (settings_rev, ts) 字典序高者胜; 本地改动批次 rev+1;
 *   应用远端包后不再回发(echo 抑制防回环)
 * - 本地出口: SettingsActivity.saveAll 末尾 notifyLocalChange() → 与基线快照差分,
 *   有变化才 rev+1 并推送(专有参数不在白名单, 天然不同步)
 * - 离线兜底: 对端发现/周期对账时握手互报 rev, 低者拉取高者全量 → 离线改动不丢
 * - 生效: 各模块快照式读配置, 应用远端包后重启监测服务(与设置页保存同语义)
 */
public final class SyncManager {

    private static final String TAG = "HRServer";
    private static final String MDNS_TYPE = "_hrmlink._tcp.";
    private static final String SELF_SERVICE_NAME = "HRMLink-Hub";  // 本端 RelayHub 的注册名(排除自己)
    private static final String DEFAULT_TOKEN = "HRMLink-Sync-2025";
    private static final long HANDSHAKE_INTERVAL_MS = 300_000;      // 对端已知时的对账周期
    private static final long LOOP_INTERVAL_MS = 30_000;            // 调度循环步进

    private static final SyncManager sInstance = new SyncManager();

    public static SyncManager get() {
        return sInstance;
    }

    private SyncManager() {
    }

    // ---- 白名单: 键 → 类型('i'int/'b'bool/'s'str) ----
    // 判定标准: 业务语义(对人的策略/环境事实)同步; 本机硬件网络/本机角色/凭证不同步
    private static final Map<String, Character> WHITELIST = new HashMap<String, Character>();

    static {
        String[] ints = {
                Prefs.PUSH_MAX_HR, Prefs.PUSH_MIN_HR, Prefs.PUSH_ABNORMAL_DURATION,
                Prefs.PUSH_COOLDOWN_SECONDS, Prefs.ALARM_SECONDS,
                Prefs.IRR_WINDOW_SECONDS, Prefs.IRR_SD_THRESHOLD, Prefs.IRR_JUMP_BPM,
                Prefs.IRR_JUMP_RATIO_PCT, Prefs.IRR_REST_MAX_HR, Prefs.IRR_SUSTAIN_WINDOWS,
                Prefs.IRR_COOLDOWN_MINUTES,
                Prefs.RELAY_THRESHOLD_DROP, Prefs.RELAY_HYSTERESIS_DB, Prefs.RELAY_MIN_RSSI,
                Prefs.RELAY_STALE_SECONDS, Prefs.RELAY_FREEZE_CYCLES,
                Prefs.MQTT_PORT, Prefs.NTFY_PRIORITY,
        };
        String[] bools = {
                Prefs.LOCAL_ALARM_ENABLED, Prefs.REMOTE_ALARM_ENABLED, Prefs.IRR_ENABLED,
                Prefs.BARK_ENABLED, Prefs.NTFY_ENABLED, Prefs.MEOW_ENABLED,
                Prefs.MQTT_DISCOVERY_ENABLED,
        };
        String[] strs = {
                Prefs.PUSH_PERIODS, Prefs.ROOM_CAMERA_MAP, Prefs.RELAY_NODE_BIASES,
                Prefs.CAMERAS_JSON,
                Prefs.BARK_SERVER, Prefs.BARK_LEVEL, Prefs.BARK_SOUND, Prefs.BARK_GROUP,
                Prefs.NTFY_TOPIC, Prefs.NTFY_SERVER, Prefs.NTFY_TAGS,
                Prefs.MEOW_NICKNAME,
                Prefs.MQTT_BROKER, Prefs.MQTT_USERNAME, Prefs.MQTT_TOPIC,
                Prefs.MQTT_DISCOVERY_TOPIC,
                Prefs.INFLUX_URL, Prefs.INFLUX_ORG, Prefs.INFLUX_BUCKET,
        };
        for (String k : ints) WHITELIST.put(k, 'i');
        for (String k : bools) WHITELIST.put(k, 'b');
        for (String k : strs) WHITELIST.put(k, 's');
    }

    private Context app;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private NsdManager nsd;
    private volatile boolean discovering = false;
    private volatile String peerHost = null;    // 对端 EXE 的 IP; null=未发现
    private volatile long lastHandshake = 0;
    private volatile boolean restartQueued = false;  // 应用远端包后的服务重启去抖

    // ---------- 生命周期(随监测服务启停) ----------

    public synchronized void start(Context c) {
        if (app != null) return;
        app = c.getApplicationContext();
        ensureBaseline(app);
        startDiscovery();
        main.postDelayed(loop, LOOP_INTERVAL_MS);
        Log.i(TAG, "参数同步已启动(mDNS " + MDNS_TYPE + " 发现对端)");
    }

    public synchronized void stop() {
        if (app == null) return;
        main.removeCallbacks(loop);
        stopDiscovery();
        peerHost = null;
        app = null;
        Log.i(TAG, "参数同步已停止");
    }

    // ---------- 本地改动统一出口 ----------

    /** 设置页保存后调用: 差分检测 → rev+1 → 推送(对端未在线则待发现后对账补推) */
    public void notifyLocalChange(Context c) {
        final Context cc = c.getApplicationContext();
        io.execute(new Runnable() {
            @Override
            public void run() {
                pushIfChanged(cc);
            }
        });
    }

    private void pushIfChanged(Context c) {
        if (!diffFromBaseline(c)) return;
        int rev = Prefs.getInt(c, Prefs.SETTINGS_REV, 0) + 1;
        Prefs.putInt(c, Prefs.SETTINGS_REV, rev);
        String ts = String.valueOf(System.currentTimeMillis());
        Prefs.putStr(c, Prefs.SYNC_TS, ts);
        if (push(c, rev, ts)) {
            storeBaseline(c);
        }
    }

    // ---------- HTTP 同步路由给 HeartServer 调用的入口 ----------

    /** GET /api/settings: 全量快照(对账用); 服务未跑返回 null → 503 */
    public String snapshotJson() {
        Context c = app;
        if (c == null) return null;
        try {
            JSONObject o = new JSONObject();
            o.put("settings_rev", Prefs.getInt(c, Prefs.SETTINGS_REV, 0));
            o.put("ts", Long.parseLong(Prefs.getStr(c, Prefs.SYNC_TS, "0")));
            o.put("settings", snapshot(c));
            return o.toString();
        } catch (Exception e) {
            return null;
        }
    }

    /** POST /api/settings: 接收远端同步包(LWW 仲裁), 返回结果+本端 rev */
    public String applyPacketJson(String body) {
        Context c = app;
        if (c == null) return "{\"ok\":false,\"err\":\"service stopped\"}";
        try {
            String r = applyPacket(c, new JSONObject(body), "http");
            JSONObject o = new JSONObject();
            o.put("ok", "applied".equals(r) || "stale".equals(r));  // stale=旧包重放, 幂等成功
            o.put("result", r);
            o.put("settings_rev", Prefs.getInt(c, Prefs.SETTINGS_REV, 0));
            return o.toString();
        } catch (Exception e) {
            return "{\"ok\":false,\"err\":\"bad packet\"}";
        }
    }

    /** 路由鉴权用: 读同步令牌; 服务未跑返回 null */
    public String syncToken() {
        Context c = app;
        return c == null ? null : Prefs.getStr(c, Prefs.SYNC_TOKEN, DEFAULT_TOKEN);
    }

    // ---------- LWW 核心 ----------

    /** 应用远端同步包: (rev, ts) 字典序高者胜; 应用后不回发(防回环); 类型不符的键丢弃 */
    private String applyPacket(Context c, JSONObject pkt, String from) {
        try {
            int rev = pkt.optInt("settings_rev", 0);
            long ts = pkt.optLong("ts", 0);
            int myRev = Prefs.getInt(c, Prefs.SETTINGS_REV, 0);
            long myTs = Long.parseLong(Prefs.getStr(c, Prefs.SYNC_TS, "0"));
            if (rev < myRev || (rev == myRev && ts <= myTs)) return "stale";
            JSONObject settings = pkt.optJSONObject("settings");
            if (settings == null) return "bad";
            int applied = 0;
            Iterator<String> it = settings.keys();
            while (it.hasNext()) {
                String k = it.next();
                Character t = WHITELIST.get(k);
                if (t == null) continue;               // 专有键不在白名单, 丢弃
                Object v = settings.opt(k);
                try {
                    if (t == 'i' && v instanceof Integer) {
                        Prefs.putInt(c, k, (Integer) v);
                    } else if (t == 'b' && v instanceof Boolean) {
                        Prefs.putBool(c, k, (Boolean) v);
                    } else if (t == 's' && v instanceof String) {
                        Prefs.putStr(c, k, (String) v);
                    } else {
                        continue;                      // 类型不符, 跳过不整包拒绝
                    }
                    applied++;
                } catch (Exception ignore) {
                }
            }
            Prefs.putInt(c, Prefs.SETTINGS_REV, rev);
            Prefs.putStr(c, Prefs.SYNC_TS, String.valueOf(ts));
            storeBaseline(c);                          // 防止被误判为本地改动再次推送
            Log.i(TAG, "已应用远端参数 " + applied + " 项(rev=" + rev + ", 来自 " + from + ")");
            queueServiceRestart(c);
            return "applied";
        } catch (Exception e) {
            Log.w(TAG, "应用远端参数失败: " + e);
            return "error";
        }
    }

    /** 快照式读配置的模块需重启生效(与设置页保存同语义), 去抖 1.5s */
    private void queueServiceRestart(final Context c) {
        if (restartQueued || !HeartRateService.isRunning()) return;
        restartQueued = true;
        main.postDelayed(new Runnable() {
            @Override
            public void run() {
                restartQueued = false;
                if (HeartRateService.isRunning()) {
                    Log.i(TAG, "远端参数生效: 重启监测服务");
                    HeartRateService.stop(c);
                    HeartRateService.start(c);
                }
            }
        }, 1500);
    }

    // ---------- 对账握手 ----------

    private void handshake() {
        Context c = app;
        String host = peerHost;
        if (c == null || host == null) return;
        lastHandshake = System.currentTimeMillis();
        try {
            JSONObject resp = httpJson(c, "GET", host, null);
            if (resp == null) return;
            int peerRev = resp.optInt("settings_rev", 0);
            long peerTs = resp.optLong("ts", 0);
            int myRev = Prefs.getInt(c, Prefs.SETTINGS_REV, 0);
            long myTs = Long.parseLong(Prefs.getStr(c, Prefs.SYNC_TS, "0"));
            boolean peerNewer = peerRev > myRev || (peerRev == myRev && peerTs > myTs);
            if (peerNewer) {
                applyPacket(c, resp, host);            // 低者拉取高者
            } else if (peerRev < myRev || diffFromBaseline(c)) {
                String ts = Prefs.getStr(c, Prefs.SYNC_TS,
                        String.valueOf(System.currentTimeMillis()));
                push(c, myRev, ts);                    // 我更新 → 强推全量
            }
            // rev/ts 全等且无本地差分 → 一致, 不动作
        } catch (Exception e) {
            Log.w(TAG, "对账失败: " + e);
        }
    }

    // ---------- 快照与基线 ----------

    /** 当前白名单配置快照(专有键天然不在包里) */
    private JSONObject snapshot(Context c) {
        JSONObject o = new JSONObject();
        Map<String, ?> all = Prefs.sp(c).getAll();
        for (Map.Entry<String, ?> e : all.entrySet()) {
            Character t = WHITELIST.get(e.getKey());
            if (t == null) continue;
            Object v = e.getValue();
            try {
                if (t == 'i' && v instanceof Integer) o.put(e.getKey(), v);
                else if (t == 'b' && v instanceof Boolean) o.put(e.getKey(), v);
                else if (t == 's' && v instanceof String) o.put(e.getKey(), v);
            } catch (Exception ignore) {
            }
        }
        return o;
    }

    private void ensureBaseline(Context c) {
        if (!Prefs.getStr(c, Prefs.SYNC_PUSHED_JSON, "").isEmpty()) return;
        storeBaseline(c);   // 首次: 以当前配置为基线(之后靠差分检测改动)
    }

    private void storeBaseline(Context c) {
        try {
            Prefs.putStr(c, Prefs.SYNC_PUSHED_JSON, snapshot(c).toString());
        } catch (Exception ignore) {
        }
    }

    private boolean diffFromBaseline(Context c) {
        try {
            JSONObject base = new JSONObject(Prefs.getStr(c, Prefs.SYNC_PUSHED_JSON, "{}"));
            JSONObject cur = snapshot(c);
            if (base.length() != cur.length()) return true;
            Iterator<String> it = cur.keys();
            while (it.hasNext()) {
                String k = it.next();
                Object a = cur.opt(k);
                Object b = base.opt(k);
                String sa = a == null ? null : a.toString();
                String sb = b == null ? null : b.toString();
                if (sa == null ? sb != null : !sa.equals(sb)) return true;
            }
            return false;
        } catch (Exception e) {
            return true;
        }
    }

    // ---------- 网络 ----------

    /** 推送同步包; 对端未在线/失败返回 false(基线保持旧值, 待补推) */
    private boolean push(Context c, int rev, String ts) {
        String host = peerHost;
        if (host == null) {
            Log.i(TAG, "参数变更已记 rev=" + rev + ", 对端未在线, 待发现后对账补推");
            return false;
        }
        try {
            JSONObject body = new JSONObject();
            body.put("settings_rev", rev);
            body.put("ts", Long.parseLong(ts));
            body.put("settings", snapshot(c));
            JSONObject resp = httpJson(c, "POST", host, body);
            boolean ok = resp != null && resp.optBoolean("ok", false);
            Log.i(TAG, "参数推送 → " + host + (ok ? " 成功" : " 被拒(对端更新)"));
            return ok;
        } catch (Exception e) {
            Log.w(TAG, "参数推送失败: " + e);
            return false;
        }
    }

    private JSONObject httpJson(Context c, String method, String host, JSONObject body) {
        HttpURLConnection conn = null;
        try {
            // 两端 HTTP 数据服务同端口口径(默认 8765)
            int port = Prefs.getInt(c, Prefs.SRV_PORT, 8765);
            URL url = new URL("http://" + host + ":" + port + "/api/settings");
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(4000);
            conn.setRequestMethod(method);
            conn.setRequestProperty("Authorization",
                    "Bearer " + Prefs.getStr(c, Prefs.SYNC_TOKEN, DEFAULT_TOKEN));
            if (body != null) {
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json");
                conn.getOutputStream().write(body.toString().getBytes("UTF-8"));
                conn.getOutputStream().close();
            }
            if (conn.getResponseCode() != 200) return null;
            BufferedReader r = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line);
            r.close();
            return new JSONObject(sb.toString());
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) {
                try {
                    conn.disconnect();
                } catch (Exception ignore) {
                }
            }
        }
    }

    // ---------- mDNS 发现 ----------

    private void startDiscovery() {
        if (app == null || discovering) return;
        try {
            if (nsd == null) {
                nsd = (NsdManager) app.getSystemService(Context.NSD_SERVICE);
            }
            nsd.discoverServices(MDNS_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener);
            discovering = true;
        } catch (Exception e) {
            Log.w(TAG, "mDNS 发现启动失败: " + e);
        }
    }

    private void stopDiscovery() {
        if (nsd != null && discovering) {
            try {
                nsd.stopServiceDiscovery(discoveryListener);
            } catch (Exception ignore) {
            }
        }
        discovering = false;
    }

    /** 调度循环: 未发现对端→重发现; 已发现→按周期对账 */
    private final Runnable loop = new Runnable() {
        @Override
        public void run() {
            if (app == null) return;
            if (peerHost == null) {
                startDiscovery();
            } else if (System.currentTimeMillis() - lastHandshake > HANDSHAKE_INTERVAL_MS) {
                io.execute(SyncManager.this::handshake);
            }
            main.postDelayed(this, LOOP_INTERVAL_MS);
        }
    };

    private final NsdManager.DiscoveryListener discoveryListener = new NsdManager.DiscoveryListener() {
        @Override
        public void onDiscoveryStarted(String t) {
        }

        @Override
        public void onStartDiscoveryFailed(String t, int e) {
            discovering = false;   // 循环稍后重试
        }

        @Override
        public void onServiceFound(NsdServiceInfo info) {
            String name = info.getServiceName();
            if (name != null && name.contains(SELF_SERVICE_NAME)) return;  // 本端注册的
            if (nsd != null) {
                try {
                    nsd.resolveService(info, resolveListener);
                } catch (Exception ignore) {
                }
            }
        }

        @Override
        public void onServiceLost(NsdServiceInfo info) {
            String name = info.getServiceName();
            if (name == null || !name.contains(SELF_SERVICE_NAME)) {
                peerHost = null;   // 对端下线, 回到重发现循环
            }
        }

        @Override
        public void onDiscoveryStopped(String t) {
            discovering = false;
        }

        @Override
        public void onStopDiscoveryFailed(String t, int e) {
            discovering = false;
        }
    };

    private final NsdManager.ResolveListener resolveListener = new NsdManager.ResolveListener() {
        @Override
        public void onResolveFailed(NsdServiceInfo info, int e) {
            // 循环会重新发现并再试
        }

        @Override
        public void onServiceResolved(NsdServiceInfo info) {
            InetAddress h = info.getHost();
            String name = info.getServiceName();
            if (h == null || h.getHostAddress() == null) return;
            if (name != null && name.contains(SELF_SERVICE_NAME)) return;
            if (isLocalIp(h)) return;                 // 另一网卡上的自己
            peerHost = h.getHostAddress();
            Log.i(TAG, "发现同步对端(EXE): " + peerHost);
            stopDiscovery();
            lastHandshake = 0;
            io.execute(SyncManager.this::handshake);
        }
    };

    private static boolean isLocalIp(InetAddress addr) {
        try {
            byte[] want = addr.getAddress();
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis.hasMoreElements()) {
                Enumeration<InetAddress> as = nis.nextElement().getInetAddresses();
                while (as.hasMoreElements()) {
                    if (Arrays.equals(as.nextElement().getAddress(), want)) return true;
                }
            }
        } catch (Exception ignore) {
        }
        return false;
    }
}
