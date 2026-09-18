package com.hrmlink.hrserver;

import android.util.Log;

import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;

import fi.iki.elonen.NanoHTTPD;
import fi.iki.elonen.NanoWSD;

/**
 * 心率数据 HTTP/WS 服务（1:1 复刻 EXE webpush_server.py, 基于 NanoWSD 2.3.1）
 *
 * 路由（与 EXE 完全一致）:
 * - GET /              → HTML 演示页（INDEX_HTML 原样搬运, UTF-8）
 * - GET /api/heartrate → HeartBus 5字段快照（含 clients）, 2秒级轮询仅打 verbose 日志
 * - GET /ws            → WebSocket 升级（仅此路径允许升级, 对齐 EXE 路由）
 *
 * WS 行为:
 * - 连接建立即推当前 4 字段快照; 之后每次 HeartBus 更新把 snapshotStateJson() 广播给全部客户端
 * - 客户端发来的消息仅作保活信号, 内容忽略
 * - 每 30 秒对所有客户端发 ping; 超过 75 秒无任何活动(pong/消息)判定死链并移除
 *   （Tailscale 网络下 TCP 半开连接无法靠写失败感知, 必须靠 pong 超时探测）
 * - open/close 时把客户端数同步到 HeartBus.setWsClients()
 *
 * 线程模型:
 * - HeartBus.Listener 回调在主线程, 此处只往广播队列投递任务, 不做 IO
 * - 所有对客户端 socket 的写操作（广播/初始快照/ping/关闭帧）统一在单线程
 *   ScheduledExecutor 上串行执行（NanoWSD 的 send 是同步 IO, 避免阻塞主线程/互相交错）
 * - NanoWSD 的连接读写回调（onOpen/onMessage/onPong/onException/onClose）在该连接的读线程上执行
 *
 * 生命周期:
 * - start(host, port): 绑定失败返回 false, 原因用 getError() 取; host 传 null/"" 绑定所有网卡
 * - stop(): 主动发关闭帧断开所有 WS → 停网络线程 → 关服务器
 * - 内部每次 start() 都新建 NanoWSD 实例, 因此 stop() 后可在同一 HeartServer 对象上
 *   换 host/port 直接再次 start(), 无需外部重新 new
 */
public class HeartServer {

    private static final String TAG = "HRServer";

    /**
     * NanoHTTPD socket 读超时。传 0 = 不设置 SO_TIMEOUT（已用 javap 确认 2.3.1:
     * ServerRunnable 中 timeout<=0 跳过 setSoTimeout）。
     * 千万不要传 5000 默认值: WS 连接 30 秒才 ping 一次, 空闲 5 秒就会被读超时踢掉。
     */
    private static final int SOCKET_READ_TIMEOUT_MS = 0;

    /** 心跳周期(秒), 对齐 EXE aiohttp heartbeat=25 的死链探测思路 */
    private static final long PING_INTERVAL_SECONDS = 30;
    /** 无任何活动(pong/客户端消息)超过该时长判定为死链, 2.5 个心跳周期 */
    private static final long PONG_TIMEOUT_MS = 75_000L;

    private static final String MIME_HTML = "text/html; charset=utf-8";
    private static final String MIME_JSON = "application/json; charset=utf-8";
    private static final String MIME_TEXT = "text/plain; charset=utf-8";

    // ---- 运行状态 ----
    /** 当前 WS 客户端集合（并发安全: 广播线程/各连接读线程都会读写） */
    private final Set<HrSocket> clients = ConcurrentHashMap.newKeySet();

    private volatile boolean running = false;
    private volatile String error = null;
    private volatile Wsd ws = null;                 // 当前 NanoWSD 实例（每次 start 新建）
    private volatile ScheduledExecutorService exec = null; // 广播+心跳 单线程执行器
    private String host = null;                     // 仅在 synchronized 方法内读写
    private int port = 0;

    /** HeartBus 监听: 主线程回调, 只投递广播任务不做 IO */
    private final HeartBus.Listener busListener = new HeartBus.Listener() {
        @Override
        public void onHeartRate(int hr, String ts, String status) {
            ScheduledExecutorService e = exec;
            if (running && e != null && !clients.isEmpty()) {
                e.execute(new Runnable() {
                    @Override
                    public void run() {
                        broadcastAll();
                    }
                });
            }
        }
    };

    // ---------- 生命周期 ----------

    /**
     * 启动服务。绑定失败返回 false（原因见 getError()）。
     * host 传 null 或 "" 表示绑定所有网卡; "auto" 的解析（detectTailscaleIp）由调用方完成。
     */
    public synchronized boolean start(String host, int port) {
        if (running) {
            Log.w(TAG, "数据服务已在运行, 忽略重复启动(换地址请先 stop)");
            return true;
        }
        this.host = host;
        this.port = port;
        this.error = null;

        final ScheduledExecutorService e = Executors.newSingleThreadScheduledExecutor(new ThreadFactory() {
            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "HRServer-Net");
                t.setDaemon(true);
                return t;
            }
        });
        HeartBus.get().addListener(busListener);
        try {
            Wsd server = new Wsd(host, port);
            server.start(SOCKET_READ_TIMEOUT_MS, true); // 绑定失败抛 IOException
            this.ws = server;
            this.exec = e;
            e.scheduleWithFixedDelay(new Runnable() {
                @Override
                public void run() {
                    try {
                        pingAll();
                    } catch (Exception ex) {
                        // 定时任务抛异常会被静默取消后续执行, 必须兜底
                        Log.w(TAG, "心跳探测异常: " + ex);
                    }
                }
            }, PING_INTERVAL_SECONDS, PING_INTERVAL_SECONDS, TimeUnit.SECONDS);
            running = true;
            Log.i(TAG, "数据服务已启动: http://" + (host == null || host.isEmpty() ? "0.0.0.0" : host) + ":" + port);
            return true;
        } catch (Exception ex) {
            this.error = ex.getMessage() == null ? ex.getClass().getSimpleName() : ex.getMessage();
            HeartBus.get().removeListener(busListener);
            e.shutdownNow();
            this.ws = null;
            this.exec = null;
            running = false;
            Log.e(TAG, "数据服务启动失败(" + host + ":" + port + "): " + this.error);
            return false;
        }
    }

    /** 停止服务: 主动断开所有 WS 客户端 + 停网络线程 + 关服务器。之后可在同一对象上换端口重新 start()。 */
    public synchronized void stop() {
        if (!running && ws == null && exec == null) {
            return;
        }
        running = false;
        HeartBus.get().removeListener(busListener);

        ScheduledExecutorService e = exec;
        exec = null;
        if (e != null) {
            // 先在广播线程上把关闭帧发完, 再停线程
            e.execute(new Runnable() {
                @Override
                public void run() {
                    closeAllClients("服务停止");
                }
            });
            e.shutdown();
            try {
                if (!e.awaitTermination(3, TimeUnit.SECONDS)) {
                    e.shutdownNow();
                }
            } catch (InterruptedException ie) {
                e.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }

        Wsd s = ws;
        ws = null;
        if (s != null) {
            try {
                s.stop(); // 关服务器监听 socket
            } catch (Exception ex) {
                Log.w(TAG, "关闭服务器异常: " + ex.getMessage());
            }
        }
        clients.clear();
        HeartBus.get().setWsClients(0);
        Log.i(TAG, "数据服务已停止");
    }

    public boolean isRunning() {
        return running;
    }

    /** 最近一次 start() 失败原因, 无错返回 "" */
    public String getError() {
        return error == null ? "" : error;
    }

    public synchronized String getHost() {
        return host == null ? "" : host;
    }

    public synchronized int getPort() {
        return port;
    }

    // ---------- 广播 / 心跳（运行在广播线程） ----------

    /** 把当前 4 字段快照广播给所有客户端; 发送失败的客户端移除（对齐 EXE _broadcast/_send_to） */
    private void broadcastAll() {
        if (clients.isEmpty()) {
            return;
        }
        String payload = HeartBus.get().snapshotStateJson();
        for (HrSocket c : clients) {
            try {
                c.send(payload);
            } catch (Exception ex) {
                dropClient(c, "发送失败: " + ex.getMessage());
            }
        }
    }

    /** 向所有客户端发 ping; 超过 PONG_TIMEOUT_MS 无任何活动的连接判定死链并移除 */
    private void pingAll() {
        if (!running || clients.isEmpty()) {
            return;
        }
        long now = System.currentTimeMillis();
        for (HrSocket c : clients) {
            try {
                c.ping(new byte[0]); // NanoWSD 2.3.1 的 ping 只有 byte[] 重载
            } catch (Exception ex) {
                dropClient(c, "ping失败: " + ex.getMessage());
                continue;
            }
            if (now - c.lastAliveMs > PONG_TIMEOUT_MS) {
                dropClient(c, "心跳超时");
            }
        }
    }

    /** 给单个客户端推当前快照（WS 连接建立后的初始推送, 对齐 EXE handle_ws） */
    private void sendSnapshotTo(HrSocket c) {
        try {
            c.send(HeartBus.get().snapshotStateJson());
        } catch (Exception ex) {
            dropClient(c, "初始快照发送失败: " + ex.getMessage());
        }
    }

    /** 关闭所有客户端连接（发送 GoingAway 关闭帧; 运行在广播线程） */
    private void closeAllClients(String why) {
        for (HrSocket c : clients) {
            try {
                c.close(NanoWSD.WebSocketFrame.CloseCode.GoingAway, why, false);
            } catch (Exception ignore) {
                // 连接可能已死, 忽略
            }
        }
    }

    /** 移除客户端并同步 HeartBus 计数; 顺手补发关闭帧促使对端读线程退出 */
    private void dropClient(HrSocket c, String why) {
        if (clients.remove(c)) {
            HeartBus.get().setWsClients(clients.size());
            Log.i(TAG, "WS客户端移除(" + why + "), 剩余客户端=" + clients.size());
        }
        try {
            c.close(NanoWSD.WebSocketFrame.CloseCode.NormalClosure, "closed by server", false);
        } catch (Exception ignore) {
            // 已关闭/已死, 忽略
        }
    }

    // ---------- NanoWSD 实现 ----------

    /**
     * NanoWSD 服务实例。
     * 非 WS 升级请求由 NanoWSD.serve() 回调 serveHttp() 处理（已用 javap 确认 2.3.1 分发逻辑）;
     * isWebsocketRequested() 限定仅 /ws 路径允许升级, 其余路径对齐 EXE 返回 404。
     */
    private class Wsd extends NanoWSD {

        Wsd(String hostname, int port) {
            super(hostname, port);
        }

        @Override
        protected boolean isWebsocketRequested(NanoHTTPD.IHTTPSession session) {
            return "/ws".equals(session.getUri()) && super.isWebsocketRequested(session);
        }

        @Override
        protected NanoWSD.WebSocket openWebSocket(NanoHTTPD.IHTTPSession handshake) {
            return new HrSocket(handshake);
        }

        @Override
        protected NanoHTTPD.Response serveHttp(NanoHTTPD.IHTTPSession session) {
            String uri = session.getUri();
            if ("/".equals(uri)) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_HTML, INDEX_HTML);
            }
            if ("/api/heartrate".equals(uri)) {
                // 2 秒级高频轮询, 仅 verbose 避免刷屏（对齐 EXE access_log=None）
                Log.v(TAG, "HTTP轮询 /api/heartrate");
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JSON,
                        HeartBus.get().snapshotApiJson());
            }
            return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND, MIME_TEXT,
                    "404 Not Found");
        }
    }

    /**
     * 单个 WS 客户端连接。
     * 各回调都在该连接的读线程上执行; 发送类操作一律投递到广播线程串行执行。
     */
    private class HrSocket extends NanoWSD.WebSocket {

        /** 最近一次活动时间(收到 pong 或任何客户端消息都刷新), 心跳超时判定依据 */
        volatile long lastAliveMs;

        HrSocket(NanoHTTPD.IHTTPSession handshake) {
            super(handshake);
        }

        @Override
        protected void onOpen() {
            lastAliveMs = System.currentTimeMillis();
            clients.add(this);
            HeartBus.get().setWsClients(clients.size());
            Log.i(TAG, "WS客户端接入, 当前客户端=" + clients.size());
            // 连接建立即推当前快照（经广播线程串行发送）
            ScheduledExecutorService e = exec;
            if (e != null) {
                e.execute(new Runnable() {
                    @Override
                    public void run() {
                        sendSnapshotTo(HrSocket.this);
                    }
                });
            }
        }

        @Override
        protected void onClose(NanoWSD.WebSocketFrame.CloseCode code, String reason, boolean initiatedByRemote) {
            dropClient(this, "客户端断开(code=" + (code == null ? -1 : code.getValue())
                    + (initiatedByRemote ? ", 远端主动)" : ", 本端主动)")
                    + (reason == null || reason.isEmpty() ? "" : " " + reason));
        }

        @Override
        protected void onMessage(NanoWSD.WebSocketFrame message) {
            // 客户端消息仅用于保活, 忽略内容（对齐 EXE async for _msg in ws: pass）
            lastAliveMs = System.currentTimeMillis();
        }

        @Override
        protected void onPong(NanoWSD.WebSocketFrame pong) {
            lastAliveMs = System.currentTimeMillis();
        }

        @Override
        protected void onException(IOException exception) {
            dropClient(this, "连接异常: " + exception.getMessage());
        }
    }

    // ---------- 静态辅助 ----------

    /**
     * 探测本机 Tailscale IP（100.64.0.0/10 网段）, 找不到返回 null。
     * 对标 EXE detect_tailscale_ip: 首字节 100 且次字节 64~127。
     */
    public static String detectTailscaleIp() {
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis != null && nis.hasMoreElements()) {
                Enumeration<InetAddress> addrs = nis.nextElement().getInetAddresses();
                while (addrs != null && addrs.hasMoreElements()) {
                    InetAddress addr = addrs.nextElement();
                    if (!(addr instanceof Inet4Address)) {
                        continue; // 只要 IPv4
                    }
                    byte[] b = addr.getAddress();
                    if (b.length == 4 && (b[0] & 0xFF) == 100
                            && (b[1] & 0xFF) >= 64 && (b[1] & 0xFF) <= 127) {
                        return addr.getHostAddress();
                    }
                }
            }
        } catch (Exception ex) {
            Log.w(TAG, "探测Tailscale IP失败: " + ex.getMessage());
        }
        return null;
    }

    // ---------- HTML 演示页（原样搬运 EXE webpush_server.py 的 INDEX_HTML, UTF-8） ----------

    private static final String INDEX_HTML =
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">\n"
            + "<title>HRMLink 心率</title>\n"
            + "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            + "<style>\n"
            + "body{font-family:sans-serif;display:flex;flex-direction:column;align-items:center;\n"
            + "justify-content:center;height:100vh;margin:0;background:#111;color:#eee}\n"
            + "#hr{font-size:20vw;font-weight:bold;color:#ff5a5a}\n"
            + "#st{color:#888}\n"
            + "#dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px}\n"
            + "</style></head>\n"
            + "<body><div id=\"hr\">--</div><div id=\"st\"><span id=\"dot\"></span><span id=\"txt\">连接中...</span></div>\n"
            + "<script>\n"
            + "var ws;\n"
            + "function conn(){\n"
            + "  ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');\n"
            + "  ws.onmessage = function(e){\n"
            + "    var d = JSON.parse(e.data);\n"
            + "    document.getElementById('hr').textContent = d.heart_rate > 0 ? d.heart_rate : '--';\n"
            + "    var ok = d.status === 'connected';\n"
            + "    document.getElementById('dot').style.background = ok ? '#4caf50' : '#f44336';\n"
            + "    document.getElementById('txt').textContent =\n"
            + "      (ok ? '已连接' : '设备未连接') + ' | ' + d.timestamp + (d.device ? ' | ' + d.device : '');\n"
            + "  };\n"
            + "  ws.onclose = function(){\n"
            + "    document.getElementById('txt').textContent = '连接断开, 3秒后重连';\n"
            + "    setTimeout(conn, 3000);\n"
            + "  };\n"
            + "}\n"
            + "conn();\n"
            + "</script></body></html>";
}
