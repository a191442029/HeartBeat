package com.hrmlink.hrbubble;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;

import org.json.JSONObject;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * 心率接收前台服务(Android 8.0+ 必须前台服务保活)
 * - 主通道: WebSocket ws://<电脑Tailscale IP>:8765/ws 实时推送
 * - 兜底: WS断开>5秒自动降级为每2秒 HTTP轮询 /api/heartrate, WS恢复即停
 * - 数据>10秒未更新 → 悬浮窗状态点变灰(超时看门狗)
 */
public class HeartRateService extends Service {

    private static final String CHANNEL_ID = "hr_monitor";
    public static final String ACTION_STATUS = "com.hrmlink.hrbubble.STATUS";
    public static final String ACTION_DATA = "com.hrmlink.hrbubble.DATA";     // 每条心率数据(首页用)
    public static final String ACTION_OVERLAY = "com.hrmlink.hrbubble.OVERLAY"; // 悬浮窗独立开关指令
    private static final long POLL_START_DELAY_MS = 5000; // WS断开5秒后才起轮询, 避免与重连抢跑

    // 首页免绑定读取的运行态(旋转重建Activity后也能正确恢复按钮文案)
    public static volatile boolean sRunning = false;    // 服务存活
    public static volatile boolean sOverlayOn = false;  // 悬浮窗开关状态

    private OkHttpClient wsClient;   // WS长连接: 无读超时 + 20秒ping保活
    private OkHttpClient pollClient; // HTTP轮询: 4秒超时防挂死(由wsClient派生, 共享线程池)
    private volatile WebSocket ws;
    private volatile boolean wsHealthy = false;
    private volatile boolean pollOnly = false;
    private String host = "";
    private String port = "8765";

    private OverlayManager overlay;
    private final Handler main = new Handler(Looper.getMainLooper());
    private ScheduledExecutorService poller;
    private volatile ScheduledFuture<?> pollTask;
    private ScheduledFuture<?> pollDelayTask;
    private volatile long lastDataMs = 0;      // volatile: 32位ART上防long撕裂
    private volatile long lastFailNotifyMs = 0; // 失败广播节流: 状态变化立即报, 持续失败30秒一次
    private boolean watchdogRunning = false;
    private int backoffIdx = 0;
    private boolean connStarted = false; // 连接是否已建立(悬浮窗单独开启时据此决定是否顺带建连)
    private static final long[] BACKOFFS = {2000, 5000, 10000, 30000};

    // 超时看门狗: 每5秒检查一次, 数据>10秒未更新则状态点变灰
    private final Runnable watchdog = new Runnable() {
        @Override
        public void run() {
            if (lastDataMs > 0 && System.currentTimeMillis() - lastDataMs > 10000) {
                overlay.timeout();
            }
            main.postDelayed(this, 5000);
        }
    };

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private static final String TAG = "HRBubbleSvc";

    @Override
    public void onCreate() {
        super.onCreate();
        sRunning = true;
        // 通知渠道在 buildNotification() 内创建(Android 8.0+ 前台服务必需)
        overlay = new OverlayManager(this);
        wsClient = new OkHttpClient.Builder()
                .pingInterval(20, TimeUnit.SECONDS)
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS) // WS长连接不读超时
                .build();
        // newBuilder()派生: 共享连接池与调度线程池, 1GB内存设备少养一套
        pollClient = wsClient.newBuilder()
                .connectTimeout(4, TimeUnit.SECONDS)
                .readTimeout(4, TimeUnit.SECONDS)
                .build();
        poller = Executors.newSingleThreadScheduledExecutor();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        SharedPreferences sp = getSharedPreferences("hrbubble", MODE_PRIVATE);
        host = sp.getString("server", "");
        port = sp.getString("port", "8765");
        pollOnly = sp.getBoolean("poll_only", false);

        startForeground(1, buildNotification(host + ":" + port));

        // 悬浮窗独立开关: 带ACTION_OVERLAY的startService只切换浮窗显示, 不打断连接
        if (intent != null && ACTION_OVERLAY.equals(intent.getAction())) {
            sOverlayOn = intent.getBooleanExtra("on", true);
            sp.edit().putBoolean("overlay_on", sOverlayOn).apply();
            if (sOverlayOn) overlay.show(); else overlay.remove();
            sendStatus(sOverlayOn ? "悬浮窗已开启" : "悬浮窗已关闭");
            if (sOverlayOn && !connStarted) startConnection(); // 未监测时只开浮窗 → 顺带建连
            return START_STICKY;
        }

        // 常规启动(开始监测): 悬浮窗按上次开关状态恢复
        sOverlayOn = sp.getBoolean("overlay_on", false);
        if (sOverlayOn) overlay.show();

        if (host.isEmpty()) {
            sendStatus("未配置服务器地址, 请点⚙设置填写");
            return START_STICKY;
        }

        startWatchdog();
        startConnection();
        return START_STICKY;
    }

    /** 建立数据连接(幂等): 悬浮窗开关与"开始监测"共用 */
    private void startConnection() {
        if (connStarted) return;
        connStarted = true;
        if (pollOnly) {
            sendStatus("仅轮询模式启动: http://" + host + ":" + port);
            startPolling();
        } else {
            connectWs();
        }
    }

    @Override
    public void onDestroy() {
        // 移除主线程全部待执行回调(看门狗+待触发WS重连), 防止停止服务后幽灵重连
        main.removeCallbacksAndMessages(null);
        closeWs();
        stopPolling();
        if (pollDelayTask != null) pollDelayTask.cancel(false);
        if (poller != null) poller.shutdownNow();
        overlay.remove();
        connStarted = false;
        sRunning = false;
        sOverlayOn = false;
        // OkHttp 3.x 关停: 两个client共享连接池/线程池, 关停一次即可
        wsClient.dispatcher().executorService().shutdown();
        wsClient.connectionPool().evictAll();
        sendStatus("服务已停止");
        super.onDestroy();
    }

    // ---------- WebSocket ----------
    private void connectWs() {
        if (ws != null || pollOnly) return;
        String url = String.format("ws://%s:%s/ws", host, port);
        Request req = new Request.Builder().url(url).build();
        sendStatus("连接 " + url + " ...");
        ws = wsClient.newWebSocket(req, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                android.util.Log.i(TAG, "WS已连接: " + response.code());
                // OkHttp回调线程 → 统一回主线程变更状态, 消除与connectWs的竞态
                main.post(() -> {
                    if (ws == null) return; // 服务已停止/连接已被主动取消
                    wsHealthy = true;
                    backoffIdx = 0;
                    stopPolling();
                    sendStatus("WS已连接(实时推送中)");
                });
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                handlePayload(text, true);
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                onWsDown("WS关闭: " + reason);
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                onWsDown("WS断开: " + (t != null ? t.getMessage() : "unknown"));
            }
        });
    }

    private void onWsDown(String why) {
        android.util.Log.w(TAG, why); // 现场诊断用: 输出WS断开/握手失败真实原因
        // OkHttp回调线程 → 统一回主线程处理; ws==null说明是主动cancel, 忽略
        // (顺带对同一连接的onClosed+onFailure双触发天然去重)
        main.post(() -> {
            if (ws == null) return;
            wsHealthy = false;
            closeWs();
            startPolling(); // 先降级轮询保证数据不中断
            long delay = BACKOFFS[Math.min(backoffIdx++, BACKOFFS.length - 1)];
            sendStatus(why + ", 转轮询, " + delay / 1000 + "秒后重连WS");
            main.postDelayed(this::connectWs, delay);
        });
    }

    private void closeWs() {
        WebSocket w = ws;
        ws = null;
        if (w != null) {
            try {
                w.cancel();
            } catch (Exception ignored) {
            }
        }
    }

    // ---------- HTTP轮询兜底 ----------
    private void startPolling() {
        if (pollTask != null && !pollTask.isDone()) return;
        if (pollOnly) {
            pollTask = poller.scheduleWithFixedDelay(this::pollOnce, 0, 2, TimeUnit.SECONDS);
        } else {
            // WS刚断开, 延迟5秒再起轮询, 给重连留窗口
            pollDelayTask = poller.schedule(() -> {
                if (!wsHealthy && pollTask == null) { // 防连续断连重复起轮询
                    pollTask = poller.scheduleWithFixedDelay(this::pollOnce, 0, 2, TimeUnit.SECONDS);
                }
            }, POLL_START_DELAY_MS, TimeUnit.MILLISECONDS);
        }
    }

    private void stopPolling() {
        if (pollTask != null) {
            pollTask.cancel(false);
            pollTask = null;
        }
    }

    private void pollOnce() {
        if (wsHealthy && !pollOnly) return; // WS已恢复, 停轮询
        try {
            String url = String.format("http://%s:%s/api/heartrate", host, port);
            Response resp = pollClient.newCall(new Request.Builder().url(url).build()).execute();
            try {
                if (resp.body() != null) {
                    lastFailNotifyMs = 0; // 轮询恢复成功, 之后再失败立即上报
                    handlePayload(resp.body().string(), false);
                }
            } finally {
                resp.close();
            }
        } catch (Exception e) {
            android.util.Log.w(TAG, "轮询失败: " + e);
            // 节流: 恢复后首次失败立即报, 持续失败每30秒报一次, 防低端机广播风暴
            long now = System.currentTimeMillis();
            if (now - lastFailNotifyMs >= 30000) {
                lastFailNotifyMs = now;
                sendStatus("轮询失败: " + e.getMessage());
            }
        }
    }

    // ---------- 数据处理 ----------
    private void handlePayload(String json, boolean fromWs) {
        try {
            JSONObject o = new JSONObject(json);
            int hr = o.optInt("heart_rate", 0);
            boolean connected = "connected".equals(o.optString("status"));
            lastDataMs = System.currentTimeMillis();
            overlay.update(hr, connected, fromWs, fromWs ? "WS" : "HTTP");
            // 广播给首页(大数字+波形+状态点)
            Intent d = new Intent(ACTION_DATA);
            d.putExtra("hr", hr);
            d.putExtra("connected", connected);
            d.putExtra("src", fromWs ? "WS" : "HTTP");
            sendBroadcast(d);
        } catch (Exception ignored) {
        }
    }

    private void startWatchdog() {
        if (!watchdogRunning) {
            watchdogRunning = true;
            main.postDelayed(watchdog, 5000);
        }
    }

    // ---------- 通知/状态 ----------
    private Notification buildNotification(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW);
        ch.setDescription(getString(R.string.channel_desc));
        nm.createNotificationChannel(ch); // Android 8.0+ 前台服务必须通知渠道
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_heart)
                .setContentTitle("心率悬浮窗运行中")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    private void sendStatus(String text) {
        Intent i = new Intent(ACTION_STATUS);
        i.setPackage(getPackageName());
        i.putExtra("text", text);
        i.putExtra("overlay", sOverlayOn);
        sendBroadcast(i);
    }
}
