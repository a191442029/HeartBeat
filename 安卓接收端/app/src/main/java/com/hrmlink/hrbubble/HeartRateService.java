package com.hrmlink.hrbubble;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.IntentFilter;
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
    private static volatile OkHttpClient sPollClient;   // 首页快照轮询复用(共享线程池, 少养一套)

    private OkHttpClient wsClient;   // WS长连接: 无读超时 + 20秒ping保活
    private OkHttpClient pollClient; // HTTP轮询: 4秒超时防挂死(由wsClient派生, 共享线程池)
    private volatile WebSocket ws;
    private volatile boolean wsHealthy = false;
    private volatile boolean pollOnly = false;
    private String host = "";
    private String port = "8765";

    private OverlayManager overlay;
    private BroadcastReceiver screenOnReceiver; // 亮屏广播: Doze后半开WS立即重建(不等心跳超时+退避)
    private final Handler main = new Handler(Looper.getMainLooper());
    private ScheduledExecutorService poller;
    private volatile ScheduledFuture<?> pollTask;
    private ScheduledFuture<?> pollDelayTask;
    private volatile long lastDataMs = 0;      // volatile: 32位ART上防long撕裂
    private volatile long lastFailNotifyMs = 0; // 失败广播节流: 状态变化立即报, 持续失败30秒一次
    private volatile String lastSourceText = ""; // 通知当前显示的数据源文案(去重, 变化才刷新通知)
    private boolean watchdogRunning = false;
    private int backoffIdx = 0;
    private boolean connStarted = false; // 连接是否已建立(悬浮窗单独开启时据此决定是否顺带建连)
    private static final long[] BACKOFFS = {2000, 5000, 10000, 30000};

    // 超时看门狗: 每5秒检查一次, 数据>10秒未更新则气泡+首页清除旧数字(仅广播一次, 恢复数据后复位)
    private boolean timeoutNotified = false;
    private final Runnable watchdog = new Runnable() {
        @Override
        public void run() {
            if (lastDataMs > 0 && System.currentTimeMillis() - lastDataMs > 10000) {
                overlay.timeout();
                if (!timeoutNotified) { // 只广播一次, 防断连后每5秒刷屏
                    timeoutNotified = true;
                    Intent d = new Intent(ACTION_DATA);
                    d.putExtra("hr", 0);
                    d.putExtra("connected", false);
                    d.putExtra("src", "超时");
                    d.putExtra("timestamp", ""); // 超时清空时间显示
                    sendBroadcast(d);
                }
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
        sPollClient = pollClient;

        // 亮屏恢复加速: 息屏期间WS可能已半开(服务器已关闭/网络被Doze挂起),
        // OkHttp心跳超时检测要等亮屏后10~30秒才触发onFailure。监听SCREEN_ON,
        // 数据陈旧(>10秒)时立即取消旧连接并重连, 恢复空窗从~30秒缩到1~2秒。
        screenOnReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (pollOnly || !connStarted) return;
                if (lastDataMs > 0 && System.currentTimeMillis() - lastDataMs > 10000) {
                    main.post(() -> {
                        if (ws == null) return; // 轮询/重连流程已在处理
                        wsHealthy = false;
                        closeWs();
                        connectWs();
                    });
                }
            }
        };
        registerReceiver(screenOnReceiver, new IntentFilter(Intent.ACTION_SCREEN_ON));
    }

    /** 首页报警快照轮询复用的HTTP客户端(服务未运行时返回null, 调用方自行判空跳过) */
    public static OkHttpClient pollClientStatic() {
        return sPollClient;
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
        if (screenOnReceiver != null) {
            try {
                unregisterReceiver(screenOnReceiver);
            } catch (Exception ignored) {
            }
            screenOnReceiver = null;
        }
        closeWs();
        stopPolling();
        if (pollDelayTask != null) pollDelayTask.cancel(false);
        if (poller != null) poller.shutdownNow();
        AlarmPlayer.stop(); // 服务停止时确保报警音不残留
        overlay.remove();
        connStarted = false;
        sRunning = false;
        sOverlayOn = false;
        // OkHttp 3.x 关停: 两个client共享连接池/线程池, 关停一次即可
        wsClient.dispatcher().executorService().shutdown();
        wsClient.connectionPool().evictAll();
        sPollClient = null;
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
            String info = o.optString("info", ""); // 附加状态(EXE智能重连进度), 空串=无
            String ts = o.optString("timestamp", ""); // EXE侧时间戳(PC生成), 随数据透传显示
            String clipUrl = o.optString("clip_url", ""); // 报警剪辑流式播放地址(期3, 剪辑成型后才有值)
            // 全量相机剪辑列表(EXE方案1): [{"cam":"卧室","url":"http://..."}], 供视频面板tab切换
            org.json.JSONArray clips = o.optJSONArray("clips");
            // 报警视频联动(2026-09-25): 绑定摄像头名/房间名/HLS直播地址(首分片就绪后异步推送, 报警开始时为空)
            String alarmCam = o.optString("alarm_cam", "");
            String alarmRoom = o.optString("alarm_room", "");
            String liveUrl = o.optString("alarm_live", "");
            boolean alarm = o.optBoolean("alarm", false); // EXE远程报警: true=循环响铃, false=停铃
            // 数据源状态(EXE快照顶层hr_source对象): 字段缺失/为null则跳过, 文案变化时静默刷新常驻通知
            JSONObject src = o.optJSONObject("hr_source");
            if (src != null) {
                String sourceText = buildSourceText(src);
                if (sourceText != null && !sourceText.equals(lastSourceText)) {
                    lastSourceText = sourceText;
                    // WS回调在非主线程, NotificationManager.notify()线程安全可直接调用
                    NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                    nm.notify(1, buildNotification(host + ":" + port + " · " + sourceText));
                }
            }
            if (alarm) {
                AlarmPlayer.play(this);
            } else {
                AlarmPlayer.stop();
            }
            overlay.setAlarm(alarm); // 悬浮窗报警态: true时气泡显示"取消报警"按钮
            lastDataMs = System.currentTimeMillis();
            timeoutNotified = false; // 数据恢复, 超时广播复位
            overlay.update(hr, connected, fromWs, fromWs ? "WS" : "HTTP", ts);
            // 广播给首页(大数字+波形+状态点+附加状态行)
            Intent d = new Intent(ACTION_DATA);
            d.putExtra("hr", hr);
            d.putExtra("connected", connected);
            d.putExtra("info", info);
            d.putExtra("src", fromWs ? "WS" : "HTTP");
            d.putExtra("timestamp", ts);
            d.putExtra("alarm", alarm); // 报警态: 首页据此显示/隐藏"取消本次报警"按钮
            d.putExtra("clip_url", clipUrl); // 期3: 报警剪辑回放地址(空=尚未成型)
            if (clips != null && clips.length() > 0) {
                d.putExtra("clips", clips.toString()); // 方案1: 全量相机剪辑JSON(tab切换)
            }
            d.putExtra("alarm_cam", alarmCam);   // 报警联动: 绑定摄像头名(快照URL带cam参数)
            d.putExtra("alarm_room", alarmRoom); // 报警联动: 房间名(面板红色标签)
            d.putExtra("alarm_live", liveUrl);   // 报警联动: HLS直播地址(空=首分片未就绪)
            sendBroadcast(d);
        } catch (Exception ignored) {
        }
    }

    /** hr_source对象 → 中文数据源文案; phase未知返回null(不动通知) */
    private static String buildSourceText(JSONObject src) {
        String phase = src.optString("phase", "");
        String source = src.optString("source", "");
        String target = src.optString("target", "");
        int rssi = src.optInt("rssi", 0); // 负数=信号强度, 0/缺失=未知
        if ("active".equals(phase)) {
            // rssi为负数有效; 0表示未知强度则省略dBm
            return rssi < 0 ? "数据源: " + source + " · " + rssi + "dBm"
                            : "数据源: " + source;
        }
        if ("switching".equals(phase)) {
            // 原节点与目标节点都已知才显示完整切换路径
            return (!source.isEmpty() && !target.isEmpty())
                    ? "切换中: " + source + " → " + target
                    : "正在切换数据源…";
        }
        if ("direct".equals(phase)) return "数据源: PC直连";
        if ("none".equals(phase)) return "数据源: 未连接";
        return null;
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
                .setStyle(new Notification.BigTextStyle().bigText(text)) // 首行=地址 副行=数据源, 展开完整显示
                .setOngoing(true)
                .setOnlyAlertOnce(true) // 数据源变化属静默刷新, 不重复响铃/震动
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
