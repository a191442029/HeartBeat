package com.hrmlink.hrserver;

import android.util.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
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
 * 路由（与 EXE webpush_server 完全一致）:
 * - GET  /                     → HTML 演示页（INDEX_HTML 原样搬运, UTF-8）
 * - GET  /api/heartrate        → _state 全字段快照（含 clients）, 2秒级轮询仅打 verbose 日志
 * - GET  /ws                   → WebSocket 升级（接收端状态推送, 对齐 EXE 路由; 中继节点
 *                                通道已迁独立 RelayWs 监听 0.0.0.0:8899, 见 RelayHub）
 * - GET  /view                 → 远程看板页(P1-6, assets/view.html; WS /view 推快照+fMP4)
 * - GET  /camera/list          → 相机列表 JSON([{name,is_default}]), 看板 tab 渲染用
 * - POST /api/cancel_alarm     → 手动取消报警（EXE 无, 安卓补齐闭环; 兼容 GET）
 * - GET  /camera/snapshot      → 报警快照 JPEG（?cam=相机名, 空=默认; 未就绪 503）
 * - GET  /camera/clip          → 剪辑流式播放（?name=文件名, 支持 HTTP Range）
 * - GET  /camera/live/index.m3u8 → HLS 实时流播放列表（?cam=相机名）
 * - GET  /camera/live/segment.ts → HLS 分片（?cam=&seg=seg_NNNNN.ts, 白名单校验）
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
    private static final String MIME_JPEG = "image/jpeg";
    private static final String MIME_MP4 = "video/mp4";
    private static final String MIME_M3U8 = "application/vnd.apple.mpegurl";
    private static final String MIME_TS = "video/mp2t";

    /** HLS 分片文件名白名单: seg_00012.ts（防路径穿越, 对齐 EXE SEG_RE） */
    private static final String SEG_PATTERN = "seg_\\d{5}\\.ts";

    /**
     * 摄像头能力提供者（由 HeartRateService 注入 CameraManager, 对齐 EXE set_camera_manager）:
     * HeartServer 只管 HTTP, 文件定位/取帧交给提供者。
     */
    public interface CamProvider {
        /** 指定摄像头最新JPEG快照帧; cam空=默认相机; 未就绪返回 null（HTTP 503） */
        byte[] snapshotJpeg(String cam);

        /** 剪辑文件: 仅允许 captures 目录下存在的 .mp4（防路径穿越）; 非法/不存在返回 null */
        File clipFile(String name);

        /** 摄像头 HLS 播放列表文件（cam空=默认相机）; 未运行返回 null（HTTP 404） */
        File livePlaylist(String cam);

        /** HLS 分片文件（seg 已按白名单校验）; 不存在返回 null */
        File liveSegment(String cam, String seg);

        /** 相机列表 JSON([{name,is_default}...]); 未启用/无相机返回 "[]"（看板 tab 渲染用） */
        String camListJson();

        /**
         * 看板 MSE 按需推流（P1-6）: 为指定相机启动 fMP4 推流进程（一客户端一路, 严格按需）。
         * sink=数据块回调（view-reader 线程）; onEnd=流结束回调（ffmpeg 退出/发送过载;
         * 主动 stop 不回调）。返回 null=无法推流（未启用/未知相机/无可用地址）。
         */
        ViewStream startMseStream(String cam, ViewStream.Sink sink, Runnable onEnd);
    }

    private volatile CamProvider camProvider;

    /** 注入摄像头提供者（服务运行前后均可） */
    public void setCamProvider(CamProvider p) {
        camProvider = p;
    }

    /** 注入 assets 访问上下文（HeartRateService 启动时调用, 看板页 view.html 读取用） */
    public void setAssetsContext(android.content.Context c) {
        assetsCtx = c.getApplicationContext();
        viewHtml = null;
    }

    /** 看板页 HTML（assets/view.html, 懒加载缓存） */
    private synchronized String loadViewHtml() {
        if (viewHtml != null) return viewHtml;
        android.content.Context c = assetsCtx;
        if (c == null) return null;
        try {
            InputStream in = c.getAssets().open("view.html");
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            in.close();
            viewHtml = out.toString("UTF-8");
            return viewHtml;
        } catch (Exception ex) {
            Log.w(TAG, "读取 view.html 失败: " + ex);
            return null;
        }
    }

    // ---- 运行状态 ----
    /** 当前 WS 客户端集合（并发安全: 广播线程/各连接读线程都会读写） */
    private final Set<HrSocket> clients = ConcurrentHashMap.newKeySet();

    /**
     * 看板 WS 客户端集合（P1-6, 独立于 /ws 接收端客户端, 不计入 ws_clients——
     * 该计数的语义是"接收端在线数", 看板浏览器不应混入）
     */
    private final Set<ViewSocket> viewClients = ConcurrentHashMap.newKeySet();

    private volatile android.content.Context assetsCtx = null;  // assets 读取上下文(setAssetsContext 注入)
    private volatile String viewHtml = null;                    // 看板页缓存(懒加载)

    private volatile boolean running = false;
    private volatile String error = null;
    private volatile Wsd ws = null;                 // 当前 NanoWSD 实例（每次 start 新建）
    private volatile ScheduledExecutorService exec = null; // 广播+心跳 单线程执行器
    private String host = null;                     // 仅在 synchronized 方法内读写
    private int port = 0;

    /**
     * HeartBus 状态监听: 任意 _state 字段变化（心率/断连/info/报警/剪辑/HLS）都全量广播,
     * 对齐 EXE webpush_server 各 set 方法均 _schedule_broadcast 的语义。
     * 回调在主线程, 此处只往广播队列投递任务, 不做 IO。
     */
    private final HeartBus.StateListener busListener = new HeartBus.StateListener() {
        @Override
        public void onStateChanged() {
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
        HeartBus.get().addStateListener(busListener);
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
            HeartBus.get().removeStateListener(busListener);
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
        HeartBus.get().removeStateListener(busListener);

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

    /** 把当前快照广播给所有客户端(接收端+看板); 发送失败的接收端移除（对齐 EXE _broadcast/_send_to） */
    private void broadcastAll() {
        if (clients.isEmpty() && viewClients.isEmpty()) {
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
        for (ViewSocket c : viewClients) {
            try {
                c.send(payload);
            } catch (Exception ignore) {
                // 看板客户端发送失败不断链: 由心跳超时/视频路径自愈
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
        for (ViewSocket c : viewClients) {
            try {
                c.close(NanoWSD.WebSocketFrame.CloseCode.GoingAway, why, false);
            } catch (Exception ignore) {
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
            String uri = session.getUri();
            return ("/ws".equals(uri) || "/view".equals(uri)) && super.isWebsocketRequested(session);
        }

        @Override
        protected NanoWSD.WebSocket openWebSocket(NanoHTTPD.IHTTPSession handshake) {
            if ("/view".equals(handshake.getUri())) {
                return new ViewSocket(handshake);
            }
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
            if ("/api/cancel_alarm".equals(uri)) {
                // 手动取消报警（EXE 无此接口, 安卓补齐闭环）: 立即复位 alarm 并广播, 接收端停铃
                String method = session.getMethod().name();
                if (!"POST".equals(method) && !"GET".equals(method)) {
                    return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.METHOD_NOT_ALLOWED,
                            MIME_TEXT, "405 Method Not Allowed");
                }
                HeartBus.get().cancelAlarm();
                Log.i(TAG, "报警已手动取消(/api/cancel_alarm)");
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JSON,
                        "{\"ok\":true}");
            }
            if ("/api/settings".equals(uri)) {
                // 与 EXE 双向参数同步(§6.5): GET=全量快照(对账), POST=接收同步包(LWW); token 鉴权
                return handleSettings(session);
            }
            if ("/camera/snapshot".equals(uri)) {
                return handleSnapshot(session);
            }
            if ("/camera/clip".equals(uri)) {
                return handleClip(session);
            }
            if ("/camera/live/index.m3u8".equals(uri)) {
                return handleLivePlaylist(session);
            }
            if ("/camera/live/segment.ts".equals(uri)) {
                return handleLiveSegment(session);
            }
            if ("/view".equals(uri)) {
                // 远程看板页（P1-6）: 心率/状态/报警/实时视频/回看
                String html = loadViewHtml();
                if (html == null) {
                    return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND,
                            MIME_TEXT, "view.html missing");
                }
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_HTML, html);
            }
            if ("/camera/list".equals(uri)) {
                // 相机列表(看板 tab 渲染用)
                CamProvider p = camProvider;
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JSON,
                        p == null ? "[]" : p.camListJson());
            }
            return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND, MIME_TEXT,
                    "404 Not Found");
        }

        // ---------- 摄像头联动路由（对齐 EXE 期3/期5） ----------

        /** 参数同步路由: Bearer 头或 ?token= 鉴权, GET=快照/POST=同步包, 委托 SyncManager */
        private NanoHTTPD.Response handleSettings(NanoHTTPD.IHTTPSession session) {
            String expect = SyncManager.get().syncToken();
            if (expect == null) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.SERVICE_UNAVAILABLE,
                        MIME_TEXT, "sync not running");
            }
            String got = null;
            String hdr = session.getHeaders().get("authorization");
            if (hdr != null && hdr.toLowerCase().startsWith("bearer ")) {
                got = hdr.substring(7).trim();
            }
            if (got == null) {
                java.util.List<String> q = session.getParameters().get("token");
                if (q != null && !q.isEmpty()) got = q.get(0);
            }
            if (!expect.equals(got)) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.UNAUTHORIZED,
                        MIME_TEXT, "401 Unauthorized");
            }
            if ("POST".equals(session.getMethod().name())) {
                java.util.Map<String, String> files = new java.util.HashMap<String, String>();
                try {
                    session.parseBody(files);
                } catch (Exception e) {
                    return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.BAD_REQUEST,
                            MIME_TEXT, "bad body");
                }
                String body = files.get("postData");
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JSON,
                        SyncManager.get().applyPacketJson(body == null ? "{}" : body));
            }
            String snap = SyncManager.get().snapshotJson();
            if (snap == null) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.SERVICE_UNAVAILABLE,
                        MIME_TEXT, "sync not running");
            }
            return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JSON, snap);
        }

        /** 报警期间快照: ?cam= 指定相机(空=默认); 帧未就绪 503（接收端显示等待占位） */
        private NanoHTTPD.Response handleSnapshot(NanoHTTPD.IHTTPSession session) {
            CamProvider p = camProvider;
            if (p == null) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.SERVICE_UNAVAILABLE,
                        MIME_JSON, "{\"error\":\"no snapshot\"}");
            }
            byte[] jpeg = p.snapshotJpeg(session.getParameters().get("cam") == null
                    ? "" : session.getParameters().get("cam").get(0));
            if (jpeg == null || jpeg.length == 0) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.SERVICE_UNAVAILABLE,
                        MIME_JSON, "{\"error\":\"no snapshot\"}");
            }
            return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.OK, MIME_JPEG,
                    new java.io.ByteArrayInputStream(jpeg), jpeg.length);
        }

        /**
         * 剪辑流式播放: 仅允许 captures 目录下 .mp4（防路径穿越）;
         * 支持 HTTP Range（手机播放器拖动/边下边播, 对齐 EXE FileResponse 原生 Range）。
         */
        private NanoHTTPD.Response handleClip(NanoHTTPD.IHTTPSession session) {
            CamProvider p = camProvider;
            java.util.List<String> names = session.getParameters().get("name");
            String name = names == null || names.isEmpty() ? "" : names.get(0);
            File f = p == null ? null : p.clipFile(name);
            if (f == null) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND,
                        MIME_JSON, "{\"error\":\"not found\"}");
            }
            long len = f.length();
            String range = session.getHeaders().get("range");
            long start = 0;
            long end = len - 1;
            NanoHTTPD.Response.Status st = NanoHTTPD.Response.Status.OK;
            if (range != null && range.startsWith("bytes=") && len > 0) {
                // 解析 bytes=start-[end]: 只处理单区间; start 越界返回 416
                String spec = range.substring(6).split(",")[0].trim();
                int dash = spec.indexOf('-');
                try {
                    long s = dash == 0 ? 0 : Long.parseLong(spec.substring(0, dash).trim());
                    long e = dash == spec.length() - 1 ? len - 1
                            : Long.parseLong(spec.substring(dash + 1).trim());
                    if (s >= len) {
                        return NanoHTTPD.newFixedLengthResponse(
                                NanoHTTPD.Response.Status.RANGE_NOT_SATISFIABLE, MIME_TEXT,
                                "416 Range Not Satisfiable");
                    }
                    start = s;
                    end = Math.min(e, len - 1);
                    st = NanoHTTPD.Response.Status.PARTIAL_CONTENT;
                } catch (NumberFormatException ignore) {
                    // 非法 Range 按无 Range 全量返回
                }
            }
            try {
                InputStream in = new FileInputStream(f);
                long skip = in.skip(start);
                if (skip < start) {
                    try { in.close(); } catch (IOException ignored) {}
                    return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.RANGE_NOT_SATISFIABLE,
                            MIME_TEXT, "416 Range Not Satisfiable");
                }
                long contentLen = end - start + 1;
                NanoHTTPD.Response r = NanoHTTPD.newFixedLengthResponse(st, MIME_MP4, in, contentLen);
                if (st == NanoHTTPD.Response.Status.PARTIAL_CONTENT) {
                    r.addHeader("Content-Range", "bytes " + start + "-" + end + "/" + len);
                }
                r.addHeader("Accept-Ranges", "bytes");
                return r;
            } catch (IOException ex) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.INTERNAL_ERROR,
                        MIME_TEXT, "500 read error");
            }
        }

        /** 报警相机 HLS m3u8 播放列表(?cam=); 未运行/未就绪 404（对齐 EXE handle_live_playlist） */
        private NanoHTTPD.Response handleLivePlaylist(NanoHTTPD.IHTTPSession session) {
            CamProvider p = camProvider;
            java.util.List<String> cams = session.getParameters().get("cam");
            File f = p == null ? null
                    : p.livePlaylist(cams == null || cams.isEmpty() ? "" : cams.get(0));
            if (f == null || !f.isFile()) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND,
                        MIME_JSON, "{\"error\":\"no live\"}");
            }
            return fileResponse(f, MIME_M3U8, "no-cache");
        }

        /** HLS 分片(?cam=&seg=seg_00001.ts); 文件名白名单防路径穿越（对齐 EXE handle_live_segment） */
        private NanoHTTPD.Response handleLiveSegment(NanoHTTPD.IHTTPSession session) {
            CamProvider p = camProvider;
            java.util.List<String> cams = session.getParameters().get("cam");
            java.util.List<String> segs = session.getParameters().get("seg");
            String seg = segs == null || segs.isEmpty() ? "" : segs.get(0);
            if (!seg.matches(SEG_PATTERN)) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.BAD_REQUEST,
                        MIME_JSON, "{\"error\":\"bad seg\"}");
            }
            File f = p == null ? null
                    : p.liveSegment(cams == null || cams.isEmpty() ? "" : cams.get(0), seg);
            if (f == null || !f.isFile()) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.NOT_FOUND,
                        MIME_JSON, "{\"error\":\"not found\"}");
            }
            return fileResponse(f, MIME_TS, null);
        }

        private NanoHTTPD.Response fileResponse(File f, String mime, String cacheControl) {
            try {
                InputStream in = new FileInputStream(f);
                NanoHTTPD.Response r = NanoHTTPD.newFixedLengthResponse(
                        NanoHTTPD.Response.Status.OK, mime, in, f.length());
                if (cacheControl != null) {
                    r.addHeader("Cache-Control", cacheControl);
                }
                return r;
            } catch (IOException ex) {
                return NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.INTERNAL_ERROR,
                        MIME_TEXT, "500 read error");
            }
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

    /**
     * 看板 WS 客户端（P1-6）: 文本帧=状态快照（与 /ws 同 payload 广播）,
     * 二进制帧=fMP4 视频块（仅订阅期间）。live 订阅命令严格按需起推流进程,
     * 断开/切换/服务停止即销毁——"浏览器开哪路推哪路"。
     * 视频帧经 per-socket 单线程 vexec 发送（NanoWSD send 同步 IO,
     * 不与广播线程/其他客户端互阻塞）。
     */
    private class ViewSocket extends HrSocket {
        private volatile ViewStream stream = null;
        private final java.util.concurrent.ExecutorService vexec;

        ViewSocket(NanoHTTPD.IHTTPSession handshake) {
            super(handshake);
            vexec = Executors.newSingleThreadExecutor(new ThreadFactory() {
                @Override
                public Thread newThread(Runnable r) {
                    Thread t = new Thread(r, "view-ws");
                    t.setDaemon(true);
                    return t;
                }
            });
        }

        @Override
        protected void onOpen() {
            lastAliveMs = System.currentTimeMillis();
            viewClients.add(this);
            Log.i(TAG, "看板客户端接入, 当前=" + viewClients.size());
            ScheduledExecutorService e = exec;
            if (e != null) {
                e.execute(new Runnable() {
                    @Override
                    public void run() {
                        sendSnapshotTo(ViewSocket.this);
                    }
                });
            }
        }

        @Override
        protected void onClose(NanoWSD.WebSocketFrame.CloseCode code, String reason, boolean initiatedByRemote) {
            stopLive();
            vexec.shutdownNow();
            viewClients.remove(this);
            Log.i(TAG, "看板客户端断开, 当前=" + viewClients.size());
        }

        @Override
        protected void onException(IOException exception) {
            stopLive();
            vexec.shutdownNow();
            viewClients.remove(this);
        }

        @Override
        protected void onMessage(NanoWSD.WebSocketFrame message) {
            lastAliveMs = System.currentTimeMillis();
            String text;
            try {
                text = message.getTextPayload();
            } catch (Exception e) {
                return;
            }
            if (text == null || text.isEmpty()) return;
            org.json.JSONObject cmd;
            try {
                cmd = new org.json.JSONObject(text);
            } catch (Exception e) {
                return;
            }
            String c = cmd.optString("cmd", "");
            if ("live".equals(c)) {
                startLive(cmd.optString("cam", ""));
            } else if ("live_stop".equals(c)) {
                stopLive();
            }
        }

        /** 订阅实时流: 切换相机先停旧; 相机不可用回 live_err 文本提示 */
        private void startLive(String cam) {
            stopLive();
            CamProvider p = camProvider;
            final ViewSocket self = this;
            ViewStream s = p == null ? null : p.startMseStream(cam, new ViewStream.Sink() {
                @Override
                public void onFrame(final byte[] frame) {
                    vexec.execute(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                self.send(frame);   // NanoWSD send(byte[]) = 二进制帧
                            } catch (Exception ignore) {
                            }
                        }
                    });
                }
            }, new Runnable() {
                @Override
                public void run() {
                    // ffmpeg 退出(相机断流)或发送过载: 提示客户端自动重试
                    notifyText(self, "{\"type\":\"live_end\",\"why\":\"stream_ended\"}");
                }
            });
            if (s == null) {
                notifyText(this, "{\"type\":\"live_err\",\"why\":\"camera_unavailable\"}");
                return;
            }
            stream = s;
            Log.i(TAG, "看板实时流订阅: " + cam);
        }

        /** 停止推流（幂等） */
        private void stopLive() {
            ViewStream s = stream;
            stream = null;
            if (s != null) {
                s.stop();
            }
        }
    }

    /** 向单个看板客户端发文本提示（经其发送线程串行, 防与视频帧交错写坏帧） */
    private void notifyText(final ViewSocket c, final String json) {
        try {
            c.vexec.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        c.send(json);
                    } catch (Exception ignore) {
                    }
                }
            });
        } catch (Exception ignore) {
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
