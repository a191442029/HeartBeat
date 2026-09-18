package com.hrmlink.hrserver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

/**
 * 心率监测前台服务（对标 EXE 主窗口的核心角色）:
 * 串联 BleManager(采集) → HeartBus(总线) →
 *   HeartServer(WS/HTTP数据服务) / AlarmEngine(告警→PushChannels) /
 *   InfluxWriter(写库) / MqttPublisher(HA发布)
 *
 * 长期通电常驻: START_STICKY + 前台通知; 被系统杀死后自动拉起重连手环。
 */
public class HeartRateService extends Service {

    private static final String TAG = "HRServer";
    public static final String ACTION_START = "com.hrmlink.hrserver.START";
    public static final String ACTION_STOP = "com.hrmlink.hrserver.STOP";
    private static final String CHANNEL_ID = "hrmonitor";
    private static final int NOTI_ID = 1;

    private static volatile boolean sRunning = false;

    private BleManager ble;
    private HeartServer server;
    private AlarmEngine alarm;
    private InfluxWriter influx;
    private MqttPublisher mqtt;
    private final Handler main = new Handler(Looper.getMainLooper());

    public static boolean isRunning() {
        return sRunning;
    }

    public static void start(Context ctx) {
        Intent it = new Intent(ctx, HeartRateService.class);
        it.setAction(ACTION_START);
        if (Build.VERSION.SDK_INT >= 26) {
            ctx.startForegroundService(it);
        } else {
            ctx.startService(it);
        }
    }

    public static void stop(Context ctx) {
        Intent it = new Intent(ctx, HeartRateService.class);
        it.setAction(ACTION_STOP);
        ctx.startService(it);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopAll();
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTI_ID, buildNotification("正在启动监测..."));
        if (!sRunning) {
            startAll();
        }
        // START_STICKY: 系统回收后 intent=null, 上面 action 判空默认重启
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (sRunning) {
            stopAll();
        }
        super.onDestroy();
    }

    // ---------------- 启动/停止 ----------------

    private void startAll() {
        sRunning = true;
        Context c = this;

        // 1. 设备名进总线(断连提醒文案/WS快照 device 字段)
        String devName = Prefs.getStr(c, Prefs.DEV_NAME, "");
        HeartBus.get().setDeviceName(devName);

        // 2. BLE 自动连接最后设备(长期通电场景)
        ble = new BleManager();
        ble.setConnListener(new BleManager.ConnListener() {
            @Override
            public void onConnecting() {
                updateNotification("正在连接手环...");
            }

            @Override
            public void onConnected(String name) {
                updateNotification("已连接 " + name);
            }

            @Override
            public void onDisconnected(String reason) {
                updateNotification("手环断开(" + reason + "), 自动重连中...");
            }
        });
        if (Prefs.getBool(c, Prefs.DEV_AUTO_CONNECT, true) && !Prefs.getStr(c, Prefs.DEV_ADDRESS, "").isEmpty()) {
            if (!BleManager.hasBlePermissions(c)) {
                android.util.Log.w(TAG, "缺少定位权限, 无法自动连接手环(打开App授权后重试)");
            } else {
                ble.connect(c, devName, Prefs.getStr(c, Prefs.DEV_ADDRESS, ""));
            }
        }

        // 3. 数据服务(WS推送+HTTP轮询, 协议与EXE webpush_server一致)
        if (Prefs.getBool(c, Prefs.SRV_ENABLED, true)) {
            server = new HeartServer();
            String addr = Prefs.getStr(c, Prefs.SRV_ADDRESS, "auto");
            String host;
            if ("auto".equals(addr) || addr.isEmpty()) {
                String detected = HeartServer.detectTailscaleIp();
                host = detected != null ? detected : "0.0.0.0";
                if (detected == null) {
                    android.util.Log.w(TAG, "未探测到Tailscale IP, 兜底绑定0.0.0.0(监听所有网卡)");
                }
            } else {
                host = addr;
            }
            int port = Prefs.getInt(c, Prefs.SRV_PORT, 8765);
            if (server.start(host, port)) {
                android.util.Log.i(TAG, "数据服务绑定 " + host + ":" + port);
            } else {
                android.util.Log.e(TAG, "数据服务启动失败: " + server.getError());
                server = null;
            }
        }

        // 4. 告警引擎 → 推送渠道
        alarm = new AlarmEngine(c);
        alarm.setAlarmListener(new AlarmEngine.AlarmListener() {
            @Override
            public void onAlarm(String title, String body, int kind) {
                PushChannels.push(HeartRateService.this, title, body);
            }
        });
        alarm.start();

        // 5. InfluxDB 写入(内部判断 enabled)
        influx = new InfluxWriter(c);
        influx.start();

        // 6. MQTT(HA): broker 已配置才连(内部异步)
        mqtt = new MqttPublisher(c);
        if (!Prefs.getStr(c, Prefs.MQTT_BROKER, "").isEmpty()) {
            mqtt.connect();
        }

        updateNotification("监测运行中" + (devName.isEmpty() ? "" : " | " + devName));
        android.util.Log.i(TAG, "HeartRateService 已启动全部模块");
    }

    private void stopAll() {
        sRunning = false;
        if (ble != null) {
            ble.shutdown();
            ble = null;
        }
        if (server != null) {
            server.stop();
            server = null;
        }
        if (alarm != null) {
            alarm.stop();
            alarm = null;
        }
        if (influx != null) {
            influx.stop();
            influx = null;
        }
        if (mqtt != null) {
            mqtt.disconnect();
            mqtt = null;
        }
        android.util.Log.i(TAG, "HeartRateService 已停止全部模块");
    }

    // ---------------- 通知 ----------------

    private Notification buildNotification(String text) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "心率监测", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("常驻通知保持后台采集");
            ch.setShowBadge(false);
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return b.setContentTitle("HRHub服务端")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_heart)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }

    private void updateNotification(String text) {
        main.post(new Runnable() {
            @Override
            public void run() {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.notify(NOTI_ID, buildNotification(text));
            }
        });
    }
}
