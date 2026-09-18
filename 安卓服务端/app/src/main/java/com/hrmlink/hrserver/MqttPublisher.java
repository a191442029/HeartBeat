package com.hrmlink.hrserver;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.eclipse.paho.client.mqttv3.IMqttDeliveryToken;
import org.eclipse.paho.client.mqttv3.MqttAsyncClient;
import org.eclipse.paho.client.mqttv3.MqttCallbackExtended;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.Random;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * MQTT 输出模块（对标 EXE mqtt_client.py, 面向 Home Assistant）
 *
 * - 连接: tcp://host:port + MemoryPersistence, 用户名/密码均非空才设置;
 *   连接成功发布 {topic}/availability="online"(qos1 retain)。
 * - 发现: discovery_enabled 时发送 HA 自动发现配置(字段与 py send_discovery_config
 *   一一对应: expire_after=60 / force_update / json_attributes / device 等)。
 * - 心率: Handler 定时器每1秒(对齐 EXE mqtt_timer)读 HeartBus 发布
 *   {"heart_rate","timestamp","status","timestamp_ms","random"}, qos1 非保留;
 *   hr 无变化也照发(random+timestamp_ms 保证每条消息唯一, 对齐 force_update 场景),
 *   status=disconnected 时 heart_rate 取 HeartBus 当前值(0)照发。
 * - 断开: connectionLost 与手动 disconnect 均尝试发布 availability offline(retain);
 *   非手动断开自动重连最多5次、间隔5秒(对齐 EXE reconnect())。
 *
 * 实现说明: 用 MqttAsyncClient 而非同步 MqttClient —— 同步版 publish 会阻塞等待完成,
 * 在主线程的1秒定时器里调用会触发 NetworkOnMainThreadException; 异步版语义与
 * py loop_start + 非阻塞 publish 一致。connect()/disconnect() 内部耗时操作均派发到
 * 单线程后台执行器, connect() 为同步阻塞语义(调用方请勿在主线程调用)。
 */
public class MqttPublisher {

    private static final String TAG = "HRServer";

    private final Context app;
    private final Handler timer = new Handler(Looper.getMainLooper());
    private final Random rnd = new Random();
    private final ExecutorService net = Executors.newSingleThreadExecutor();

    private volatile MqttAsyncClient client;
    private volatile boolean connected = false;         // 对齐 EXE self.connected
    private volatile boolean manualDisconnect = false;  // 对齐 EXE stop_reconnect
    private volatile String statusText = "未连接";

    // 连接配置(connect() 时读取, 对齐 EXE self.config)
    private volatile String stateTopic;
    private volatile String discoveryTopic;
    private volatile boolean discoveryEnabled = true;

    /** 1秒定时器(对齐 EXE mqtt_timer): 周期读 HeartBus 快照发布 */
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            publishHeartRateTick();
            timer.postDelayed(this, 1000L);
        }
    };

    public MqttPublisher(Context context) {
        app = context.getApplicationContext();
    }

    /**
     * 同步阻塞连接(带超时判断), 成功返回 true。耗时操作, 请勿在主线程调用。
     * 重复调用会先清理旧客户端(对齐 py connect 里的 self.disconnect())。
     */
    public boolean connect() {
        manualDisconnect = false;   // 复位重连标志(对齐 py connect 顶部注释)
        stopTimer();
        connected = false;
        statusText = "连接中...";

        // 旧客户端先清理(offline 遗言发到旧主题, 避免配置已变后发错主题)
        final MqttAsyncClient old = client;
        final String oldAvailTopic = (stateTopic == null ? "" : stateTopic) + "/availability";
        client = null;
        if (old != null) {
            net.execute(new Runnable() {
                @Override
                public void run() {
                    teardown(old, oldAvailTopic);
                }
            });
        }

        // ---- 读 Prefs [MQTT], 默认值对齐 EXE mqtt_client.py config ----
        String broker = Prefs.getStr(app, Prefs.MQTT_BROKER, "localhost").trim();
        if (broker.isEmpty()) broker = "localhost";
        int port = Prefs.getInt(app, Prefs.MQTT_PORT, 1883);
        if (port <= 0 || port > 65535) {
            Log.w(TAG, "MQTT端口非法(" + port + "), 使用默认1883");
            port = 1883;
        }
        String clientId = Prefs.getStr(app, Prefs.MQTT_CLIENT_ID, "heartbeat_monitor").trim();
        if (clientId.isEmpty()) clientId = "heartbeat_monitor";
        stateTopic = Prefs.getStr(app, Prefs.MQTT_TOPIC, "homeassistant/sensor/heartrate/state").trim();
        if (stateTopic.isEmpty()) stateTopic = "homeassistant/sensor/heartrate/state";
        discoveryTopic = Prefs.getStr(app, Prefs.MQTT_DISCOVERY_TOPIC, "homeassistant/sensor/heartrate/config").trim();
        if (discoveryTopic.isEmpty()) discoveryTopic = "homeassistant/sensor/heartrate/config";
        discoveryEnabled = Prefs.getBool(app, Prefs.MQTT_DISCOVERY_ENABLED, true);
        final String username = Prefs.getStr(app, Prefs.MQTT_USERNAME, "").trim();
        final String password = Prefs.getStr(app, Prefs.MQTT_PASSWORD, "").trim();
        final String uri = "tcp://" + broker + ":" + port;

        try {
            final MqttAsyncClient c = new MqttAsyncClient(uri, clientId, new MemoryPersistence());
            final MqttConnectOptions opt = new MqttConnectOptions();
            if (!username.isEmpty() && !password.isEmpty()) {   // 对齐 EXE: 用户名密码均非空才设置
                opt.setUserName(username);
                opt.setPassword(password.toCharArray());
            }
            opt.setConnectionTimeout(10);       // 阻塞连接的判定上限
            opt.setKeepAliveInterval(60);       // 对齐 EXE client.connect(broker, port, 60)
            opt.setAutomaticReconnect(false);   // 重连由自管线程负责(对齐 EXE reconnect())
            opt.setCleanSession(true);

            c.setCallback(new MqttCallbackExtended() {
                @Override
                public void connectComplete(boolean reconnect, String serverURI) {
                    onConnected(c, reconnect, serverURI);
                }

                @Override
                public void connectionLost(Throwable cause) {
                    onLost(c, cause);
                }

                @Override
                public void messageArrived(String topic, MqttMessage message) {
                }

                @Override
                public void deliveryComplete(IMqttDeliveryToken token) {
                }
            });
            client = c;

            final CountDownLatch latch = new CountDownLatch(1);
            final boolean[] ok = {false};
            net.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        c.connect(opt).waitForCompletion();   // 同步等待(受 connectionTimeout 约束)
                        ok[0] = true;
                    } catch (Exception e) {
                        Log.e(TAG, "MQTT连接失败: " + e);
                        statusText = "连接失败: " + e.getMessage();
                    } finally {
                        latch.countDown();
                    }
                }
            });
            if (!latch.await(15, TimeUnit.SECONDS)) {   // 连接超时判定(10s连接超时+5s余量)
                Log.e(TAG, "MQTT连接超时: " + uri);
                statusText = "连接超时";
                client = null;   // 作废本次尝试, 迟到的回调经 c != client 全部失效
                net.execute(new Runnable() {
                    @Override
                    public void run() {
                        forceClose(c);
                    }
                });
                return false;
            }
            if (ok[0] && c.isConnected()) {
                startTimer();
                return true;
            }
            return false;
        } catch (Exception e) {
            Log.e(TAG, "MQTT连接失败: " + e);
            statusText = "连接失败: " + e.getMessage();
            return false;
        }
    }

    /** 手动断开: 先停1秒定时器 → 置手动标志 → 后台尝试发布 offline 遗言并关闭连接 */
    public void disconnect() {
        stopTimer();
        manualDisconnect = true;
        connected = false;
        final MqttAsyncClient c = client;
        client = null;   // 使过期回调/重连线程全部失效
        statusText = "已手动断开";
        if (c == null) return;
        final String availTopic = (stateTopic == null ? "" : stateTopic) + "/availability";
        net.execute(new Runnable() {
            @Override
            public void run() {
                teardown(c, availTopic);
            }
        });
    }

    public boolean isConnected() {
        return connected;
    }

    /** 供UI显示的状态文本 */
    public String getStatusText() {
        return statusText;
    }

    // ---- 回调处理 ----

    /** 连接成功(含重连成功, 对齐 py on_connect): 发在线状态 + 自动发现配置 */
    private void onConnected(MqttAsyncClient c, boolean reconnect, String serverURI) {
        if (manualDisconnect || c != client) return;   // 过期回调(客户端已更换/已手动断开)
        connected = true;
        Log.i(TAG, "已连接到MQTT服务器" + (reconnect ? "(重连)" : "") + ": " + serverURI);
        statusText = "已连接 (" + serverURI + ")";
        // 发布在线状态(qos1 retain)
        if (publish(c, stateTopic + "/availability", "online", 1, true)) {
            Log.i(TAG, "已发布设备在线状态");
        } else {
            Log.e(TAG, "发布设备在线状态失败");
        }
        if (discoveryEnabled) {
            sendDiscoveryConfig(c);
        }
    }

    /** 意外断开(对齐 py on_disconnect): 尝试发 offline, 非手动断开则自动重连5次 */
    private void onLost(MqttAsyncClient c, Throwable cause) {
        if (c != client) return;   // 过期回调
        connected = false;
        Log.w(TAG, "MQTT连接断开: " + cause);
        statusText = "已断开";
        // 此时连接已断, 发布大概率失败, 但仍尝试(对齐 EXE 行为并记日志)
        if (publish(c, stateTopic + "/availability", "offline", 1, true)) {
            Log.i(TAG, "已发布设备离线状态");
        } else {
            Log.e(TAG, "发布设备离线状态失败");
        }
        if (!manualDisconnect) {
            startReconnect(c);
        }
    }

    /** 自动重连: 最多5次、每次间隔5秒(对齐 EXE reconnect()) */
    private void startReconnect(final MqttAsyncClient c) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                for (int i = 1; i <= 5; i++) {
                    if (manualDisconnect || c != client || c.isConnected()) break;
                    Log.i(TAG, "尝试重新连接MQTT服务器 (" + i + "/5)...");
                    statusText = "重连中 (" + i + "/5)";
                    try {
                        Thread.sleep(5000L);   // 等待5秒再重试
                    } catch (InterruptedException e) {
                        break;
                    }
                    try {
                        c.reconnect();   // 成功后 connectComplete 回调 → 在线状态+discovery
                    } catch (Exception e) {
                        Log.e(TAG, "MQTT重连失败: " + e);
                    }
                }
                if (!manualDisconnect && c == client && !c.isConnected()) {
                    statusText = "重连失败(已达5次)";
                    Log.e(TAG, "MQTT重连已达最大次数(5次), 放弃重连");
                }
            }
        }, "mqtt-reconnect").start();
    }

    /** 发送 Home Assistant 自动发现配置(字段与 py send_discovery_config 一一对应, qos1 retain) */
    private void sendDiscoveryConfig(MqttAsyncClient c) {
        if (!connected || c == null) return;
        try {
            JSONObject device = new JSONObject();
            device.put("identifiers", new JSONArray().put("heartbeat_monitor"));
            device.put("name", "HeartBeat Monitor");
            device.put("model", "BLE Heart Rate Monitor");
            device.put("manufacturer", "HeartBeat App");

            JSONObject cfg = new JSONObject();
            cfg.put("name", "Heart Rate");
            cfg.put("unique_id", "heartbeat_monitor_hr");
            cfg.put("state_topic", stateTopic);
            cfg.put("value_template", "{{ value_json.heart_rate }}");
            cfg.put("unit_of_measurement", "BPM");
            cfg.put("icon", "mdi:heart-pulse");
            cfg.put("force_update", true);    // 强制更新, 即使值没有变化
            cfg.put("expire_after", 60);      // 60秒无更新后显示为不可用
            cfg.put("availability_topic", stateTopic + "/availability");
            cfg.put("payload_available", "online");
            cfg.put("payload_not_available", "offline");
            cfg.put("json_attributes_topic", stateTopic);
            cfg.put("json_attributes_template",
                    "{\"last_updated\": \"{{ value_json.timestamp }}\", \"status\": \"{{ value_json.status }}\"}");
            cfg.put("device", device);

            if (publish(c, discoveryTopic, cfg.toString(), 1, true)) {
                Log.i(TAG, "已发送MQTT自动发现配置");
            } else {
                Log.e(TAG, "发送MQTT自动发现配置失败");
            }
        } catch (Exception e) {
            Log.e(TAG, "发送MQTT自动发现配置时出错: " + e);
        }
    }

    /** 1秒定时回调: 读 HeartBus 快照发布(未连接静默返回, 对齐 py publish_heart_rate) */
    private void publishHeartRateTick() {
        MqttAsyncClient c = client;
        if (c == null || !connected) return;
        // status=disconnected 时 heart_rate 为 HeartBus 当前值(0), 照发(对齐 EXE 断连推送场景)
        int hr = HeartBus.get().getHeartRate();
        String ts = HeartBus.get().getTimestamp();
        if (ts == null || ts.isEmpty()) ts = now();
        String st = HeartBus.get().getStatus();
        try {
            JSONObject payload = new JSONObject();
            payload.put("heart_rate", hr);
            payload.put("timestamp", ts);
            payload.put("status", st);
            payload.put("timestamp_ms", System.currentTimeMillis());   // 毫秒时间戳
            payload.put("random", rnd.nextInt(1000000) + 1);           // 随机数确保每条消息唯一
            if (publish(c, stateTopic, payload.toString(), 1, false)) {   // qos1 非保留
                Log.d(TAG, "成功发布心率数据: " + hr + ", 状态: " + st);
            }
        } catch (Exception e) {
            Log.e(TAG, "发布心率数据时出错: " + e);
        }
    }

    // ---- 内部工具 ----

    private boolean publish(MqttAsyncClient c, String topic, String payload, int qos, boolean retain) {
        if (c == null) return false;
        try {
            MqttMessage m = new MqttMessage(payload.getBytes(StandardCharsets.UTF_8));
            m.setQos(qos);
            m.setRetained(retain);
            c.publish(topic, m);
            return true;
        } catch (Exception e) {
            Log.e(TAG, "MQTT发布失败(" + topic + "): " + e);
            return false;
        }
    }

    /** 断开并释放客户端; 断开前先向指定可用性主题发 offline 遗言(qos1 retain) */
    private void teardown(MqttAsyncClient c, String availTopic) {
        try {
            if (c.isConnected()) {
                publish(c, availTopic, "offline", 1, true);
            }
        } catch (Exception e) {
            Log.w(TAG, "发布设备离线状态失败: " + e);
        }
        try {
            c.disconnect();
        } catch (Exception e) {
            Log.w(TAG, "断开MQTT连接时出现异常: " + e);
        }
        try {
            c.close();
        } catch (Exception e) {
            // close 失败可忽略(客户端即将被丢弃)
        }
    }

    /** 连接超时后的强制清理(迟到连接作废) */
    private void forceClose(MqttAsyncClient c) {
        try {
            c.disconnectForcibly(1000L);
        } catch (Exception e) {
            // 忽略
        }
        try {
            c.close();
        } catch (Exception e) {
            // 忽略
        }
    }

    private void startTimer() {
        timer.removeCallbacks(tick);
        timer.post(tick);   // 立即发第一帧, 之后每1秒一帧
    }

    private void stopTimer() {
        timer.removeCallbacks(tick);
    }

    private static String now() {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(new Date());
    }
}
