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

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.net.URLEncoder;
import java.util.List;

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
    private CameraManager camera;
    private final Handler main = new Handler(Looper.getMainLooper());

    // ---- 模块静态引用(大屏状态卡片读 MQTT/InfluxDB 状态; 服务销毁置 null) ----
    private static volatile MqttPublisher sMqtt = null;
    private static volatile InfluxWriter sInflux = null;

    public static MqttPublisher getMqtt() {
        return sMqtt;
    }

    public static InfluxWriter getInflux() {
        return sInflux;
    }


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
        SyncManager.get().start(c);   // 与 EXE 双向参数同步: mDNS 互发现 + 对账(§6.5)

        // 1. 设备名进总线(断连提醒文案/WS快照 device 字段)
        String devName = Prefs.getStr(c, Prefs.DEV_NAME, "");
        HeartBus.get().setDeviceName(devName);

        // 2. BLE(直连或兜底): relay 启用时手环连接权归中继仲裁器, 本机不自动连接(仅 LocalBridge 兜底)
        boolean relayOn = Prefs.getBool(c, Prefs.RELAY_ENABLED, false);
        ble = new BleManager();
        ble.setConnListener(new BleManager.ConnListener() {
            @Override
            public void onConnecting() {
                updateNotification("正在连接手环...");
            }

            @Override
            public void onConnected(String name) {
                updateNotification("已连接 " + name);
                // 本机连接建立(手动直连或仲裁兜底): 记录让位判定窗起点(RelayHub 未运行时忽略)
                if (RelayHub.get().isRunning()) {
                    RelayHub.get().fallbackEvt(true, "");
                }
            }

            @Override
            public void onDisconnected(String reason) {
                updateNotification("手环断开(" + reason + "), 自动重连中...");
                if (RelayHub.get().isRunning()) {
                    RelayHub.get().fallbackEvt(false, reason);
                }
            }
        });
        if (relayOn) {
            android.util.Log.i(TAG, "中继模式: 手环连接权归 RelayHub 仲裁, 本机 BLE 仅兜底");
        } else if (Prefs.getBool(c, Prefs.DEV_AUTO_CONNECT, true) && !Prefs.getStr(c, Prefs.DEV_ADDRESS, "").isEmpty()) {
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
                // 绑定策略(2026-09-30 拍板): 一律 0.0.0.0 —— LAN/Tailscale 双通,
                // 内网接收端走局域网低延迟, 外网接收端走 100.x(0.0.0.0 覆盖所有网卡)
                host = "0.0.0.0";
                String ts = HeartServer.detectTailscaleIp();
                if (ts != null) {
                    android.util.Log.i(TAG, "Tailscale IP: " + ts + "(数据服务绑 0.0.0.0, LAN/Tailscale 双通)");
                } else {
                    android.util.Log.i(TAG, "未探测到Tailscale IP, 数据服务绑 0.0.0.0(仅局域网可达)");
                }
            } else {
                host = addr;   // 手填 IP 尊重用户指定
            }
            int port = Prefs.getInt(c, Prefs.SRV_PORT, 8765);
            server.setAssetsContext(this);   // 看板页 view.html 读取用
            if (server.start(host, port)) {
                android.util.Log.i(TAG, "数据服务绑定 " + host + ":" + port);
            } else {
                android.util.Log.e(TAG, "数据服务启动失败: " + server.getError());
                server = null;
            }
        }

        // 3.5 ESP32 中继中枢(relay_enabled, 独立监听 0.0.0.0:8899 + mDNS 通告 _hrmlink._tcp):
        //     板载BLE即"直连", 兜底经 LocalBridge 下发; 心率/数据源/手环三态回调统一并入 HeartBus(与直连同一下游)
        if (relayOn) {
            RelayHub hub = RelayHub.get();
            hub.setHrCallback((ts, bpm) -> HeartBus.get().publishHeartRate(bpm));
            hub.setAlarmCheck(() -> HeartBus.get().isAlarm());
            hub.setDirectCheck(() -> ble != null && ble.isConnected());
            hub.setSourceCallback(json -> HeartBus.get().setHrSource(json.toString()));
            hub.setBandStatusCallback(st -> {
                String s = st == null ? "" : st.optString("state", "");
                if ("ok".equals(s)) {
                    updateNotification("已连接 " + st.optString("node", "") + "(中继)");
                } else if ("nodata".equals(s)) {
                    String why = st.optString("reason", "");
                    updateNotification("手环无数据(" + (why.isEmpty() ? st.optString("node", "") : why) + ")");
                } else if ("lost".equals(s)) {
                    updateNotification("手环未连接(所有节点未搜索到)");
                    // 此刻 hr_source.phase=none, 断流保护不抑制: 接收端正确进入断连态
                    HeartBus.get().publishDeviceStatus(false);
                }
            });
            hub.setLocalBridge(cmd -> localBleCommand(cmd));
            hub.start(this);
        }

        // 4. 告警引擎 → 推送渠道 + 本地报警音 + 远程报警生命周期
        alarm = new AlarmEngine(c);
        alarm.setAlarmListener(new AlarmEngine.AlarmListener() {
            @Override
            public void onAlarm(String title, String body, int kind) {
                PushChannels.push(HeartRateService.this, title, body);
                // 本地响铃: 仅心率类告警(过高/过低/疑似心律不齐), 设备断连/恢复不响(对齐EXE);
                // 可在设置页"本地报警声音"开关控制, 默认开
                if ((kind == AlarmEngine.KIND_HIGH || kind == AlarmEngine.KIND_LOW
                        || kind == AlarmEngine.KIND_IRREGULAR)
                        && Prefs.getBool(HeartRateService.this, Prefs.LOCAL_ALARM_ENABLED, true)) {
                    AlarmPlayer.play(HeartRateService.this);
                }
                // 远程报警: 心率类告警触发接收端响铃窗口, alarm_seconds 到期自动复位
                // (对齐 EXE alarm_remote_enabled + alarm_seconds; 接收端可经
                //  POST /api/cancel_alarm 提前取消——EXE 无此闭环, 安卓补齐)
                if ((kind == AlarmEngine.KIND_HIGH || kind == AlarmEngine.KIND_LOW
                        || kind == AlarmEngine.KIND_IRREGULAR)
                        && Prefs.getBool(HeartRateService.this, Prefs.REMOTE_ALARM_ENABLED, true)) {
                    // 报警视频联动: 按"当前持有手环的节点=所在房间"查绑定摄像头(对齐 EXE _on_remote_alarm)
                    String[] cr = alarmRoomCamera();
                    HeartBus.get().triggerAlarm(
                            Prefs.getInt(HeartRateService.this, Prefs.ALARM_SECONDS, 10), cr[0], cr[1]);
                }
            }
        });
        alarm.start();

        // 5. 报警视频联动钩子(对齐 EXE webpush_server.on_alarm_start/end + _on_camera_alarm):
        //    start → HLS实时流地址入快照 + 报警快照兜底流 + 触发即剪(报警前10秒分片在环形缓冲)
        //    end(到期/手动取消) → 停快照流 + 清 alarm_live
        HeartBus.get().setAlarmHook(new HeartBus.AlarmHook() {
            @Override
            public void onAlarmStart(String cam, String room) {
                if (camera != null) {
                    String camName = camera.defaultName();
                    String bound = (cam == null || cam.isEmpty()) ? camName : cam;
                    // HLS实时流: 常驻拉流分片已在磁盘, 报警即就绪; 播放列表未生成则留空
                    // (接收端回退快照轮询, 对齐 EXE set_live_url 语义)
                    if (server != null && !bound.isEmpty()) {
                        File pl = camera.livePlaylist(bound);
                        if (pl != null) {
                            String base = "http://" + server.getHost() + ":" + server.getPort();
                            HeartBus.get().setAlarmLive(base + "/camera/live/index.m3u8?cam="
                                    + enc(bound));
                        }
                    }
                    camera.startAlarmSnapshots(cam);
                    // 触发即剪(EXE 剪辑在报警推送时刻, 非报警结束): 后台执行, 完成后补推WS
                    new Thread(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                cutAndPushClips();
                            } catch (Exception e) {
                                android.util.Log.e(TAG, "报警剪辑推送失败: " + e);
                            }
                        }
                    }, "alarm-cut").start();
                }
            }

            @Override
            public void onAlarmEnd() {
                if (camera != null) {
                    camera.stopAlarmSnapshots();
                }
                HeartBus.get().setAlarmLive("");
            }
        });

        // 6. 摄像头链路(常驻拉流+报警联动; 无ffmpeg/无配置时内部自禁用)
        if (Prefs.getBool(c, Prefs.CAMERA_ENABLED, false)) {
            camera = CameraManager.get();
            if (server != null) {
                server.setCamProvider(camera);
            }
            camera.start(c);
        }

        // 7. InfluxDB 写入(内部判断 enabled)
        influx = new InfluxWriter(c);
        influx.start();
        sInflux = influx;

        // 8. MQTT(HA): broker 已配置才连(内部异步)
        mqtt = new MqttPublisher(c);
        if (!Prefs.getStr(c, Prefs.MQTT_BROKER, "").isEmpty()) {
            mqtt.connect();
        }
        sMqtt = mqtt;

        updateNotification("监测运行中" + (devName.isEmpty() ? "" : " | " + devName));
        android.util.Log.i(TAG, "HeartRateService 已启动全部模块");
    }

    // ---------------- 报警视频联动辅助 ----------------

    /**
     * 报警剪辑完成补推(后台线程调用, 对齐 EXE _emit_default_clip_url):
     * 全量剪辑列表拼绝对URL入 clips(新接收端视频面板tab切换),
     * 默认相机(无则第一条)入 clip_url(兼容旧接收端)。
     */
    private void cutAndPushClips() {
        List<JSONObject> clips = CameraManager.get().cutClipsForAlarm();
        if (clips.isEmpty()) return;
        if (server == null || !server.isRunning()) {
            android.util.Log.w(TAG, "数据服务未运行, 剪辑已存档但不推送WS");
            return;
        }
        String base = "http://" + server.getHost() + ":" + server.getPort();
        String def = CameraManager.get().defaultName();
        String defaultUrl = null;
        JSONArray arr = new JSONArray();
        for (JSONObject c : clips) {
            String url = base + "/camera/clip?name=" + enc(c.optString("name"));
            try {
                JSONObject item = new JSONObject();
                item.put("cam", c.optString("cam", "监控"));
                item.put("url", url);
                arr.put(item);
            } catch (Exception ignore) {
            }
            // 默认相机优先; 报警绑定相机与默认相机一致(EXE pick 语义), 无则第一条
            if (defaultUrl == null || c.optString("cam").equals(def)) {
                defaultUrl = url;
            }
        }
        HeartBus.get().pushClip(defaultUrl == null ? "" : defaultUrl, arr.toString());
        android.util.Log.i(TAG, "报警剪辑已推WS: " + arr.length() + "路");
    }

    /** URL 编码(UTF-8, 失败回退原文) */
    private static String enc(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    // ---------------- 中继本机兜底桥 ----------------

    /**
     * 报警房间→摄像头绑定(对齐 EXE _alarm_room_camera):
     * 中继数据源(RelayHub.sourceStatus.source)得到房间名(=节点名),
     * 查 [RELAY] room_camera_map(房间→摄像头名)绑定;
     * 中继未启用/无数据源/未绑定返回空串(AlarmHook 回退默认摄像头)。
     * source 原样查映射不特判(对齐 EXE; 直连时 source="本机", 用户绑了该键也可命中)。
     */
    private String[] alarmRoomCamera() {
        if (!RelayHub.get().isRunning()) {
            return new String[]{"", ""};
        }
        String room;
        try {
            room = RelayHub.get().sourceStatus().optString("source", "");
        } catch (Exception e) {
            room = "";
        }
        if (room.isEmpty()) {
            return new String[]{"", ""};
        }
        String cam = "";
        try {
            JSONObject m = new JSONObject(Prefs.getStr(this, Prefs.ROOM_CAMERA_MAP, "{}"));
            cam = m.optString(room, "");
        } catch (Exception e) {
            android.util.Log.e(TAG, "报警房间摄像头绑定解析失败: " + e);
        }
        return new String[]{cam, room};
    }

    /**
     * RelayHub 本机BLE命令桥(对齐 EXE _pc_bridge.on_command):
     * connect{mac} → 本机连接手环(全体节点失聪兜底); disconnect → 本机让位断开。
     * 连接结果经 ConnListener → fallbackEvt 回报仲裁器(让位判定窗起点)。
     */
    private void localBleCommand(final JSONObject cmd) {
        // BLE 操作切主线程执行(仲裁器在 relay-net 线程下发命令)
        main.post(new Runnable() {
            @Override
            public void run() {
                String type = cmd == null ? "" : cmd.optString("type", "");
                if ("connect".equals(type)) {
                    String mac = cmd.optString("mac", "");
                    if (mac.isEmpty() || ble == null) return;
                    android.util.Log.i(TAG, "RelayHub 兜底: 本机连接手环 " + mac);
                    ble.connect(HeartRateService.this, Prefs.getStr(HeartRateService.this,
                            Prefs.DEV_NAME, ""), mac);
                } else if ("disconnect".equals(type)) {
                    if (ble == null) return;
                    android.util.Log.i(TAG, "RelayHub: 本机让位, 断开手环");
                    ble.disconnect();
                }
            }
        });
    }

    private void stopAll() {
        sRunning = false;
        SyncManager.get().stop();      // 参数同步随服务停
        HeartBus.get().cancelAlarm();  // 清报警态(接收端停铃) + 停快照流钩子
        HeartBus.get().setAlarmHook(null);
        if (ble != null) {
            ble.shutdown();
            ble = null;
        }
        RelayHub.get().stop();   // 中继中枢: 断开全部节点+停仲裁线程(幂等, 未启用时无操作)
        if (camera != null) {
            camera.stop();
            camera = null;
        }
        if (server != null) {
            server.stop();
            server = null;
        }
        if (alarm != null) {
            alarm.stop();
            alarm = null;
        }
        AlarmPlayer.stop();   // 服务停止时兜底停铃
        if (influx != null) {
            influx.stop();
            influx = null;
        }
        sInflux = null;
        if (mqtt != null) {
            mqtt.disconnect();
            mqtt = null;
        }
        sMqtt = null;
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
