package com.hrmlink.hrserver;

import android.os.Handler;
import android.os.Looper;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 心率数据总线（单例，对标 EXE 主窗口 on_heart_rate_updated 数据流 + webpush_server._state 状态快照）
 *
 * 生产者: BleManager（publishHeartRate / publishDeviceStatus）、RelayHub(P1, setHrSource)、
 *         报警生命周期(triggerAlarm / cancelAlarm / pushClip / setAlarmLive)
 * 消费者: HeartServer(WS广播) / AlarmEngine(告警) / InfluxWriter(写库) /
 *         MqttPublisher(发布) / 监测UI(数字+波形+状态)
 *
 * 监听器回调统一在主线程派发；消费方做网络/磁盘操作必须自行切到工作线程。
 *
 * _state 全字段对齐 EXE webpush_server.py（12 字段, WS 快照/HTTP 轮询共用）:
 *   heart_rate / timestamp / status / device / info / alarm /
 *   clip_url / clips / hr_source / alarm_cam / alarm_room / alarm_live
 */
public final class HeartBus {

    /** 数据监听器: hr<=0 表示断连状态推送 */
    public interface Listener {
        void onHeartRate(int hr, String ts, String status);
    }

    /**
     * 状态快照变更监听器: 任意 _state 字段变化后回调（心率更新/断连/info/hr_source/
     * 报警/剪辑/HLS 均触发）, HeartServer 据此做 WS 全量广播（对齐 EXE _schedule_broadcast）。
     */
    public interface StateListener {
        void onStateChanged();
    }

    /**
     * 报警起止钩子（对标 EXE webpush_server.on_alarm_start / on_alarm_end）:
     * start → 摄像头侧启动报警快照流; end(自然到期或手动取消) → 停快照流。
     * 回调在主线程派发, 耗时操作自行切工作线程。
     */
    public interface AlarmHook {
        void onAlarmStart(String cam, String room);
        void onAlarmEnd();
    }

    private static final HeartBus sInstance = new HeartBus();

    public static HeartBus get() {
        return sInstance;
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<StateListener> stateListeners = new CopyOnWriteArrayList<>();

    private volatile int heartRate = 0;          // 0=无效/未连接
    private volatile String timestamp = "";
    private volatile String status = "disconnected";
    private volatile String deviceName = "";
    private volatile int wsClients = 0;          // 由 HeartServer 维护
    private volatile long lastDataMs = 0;        // 最近一次真实心率数据时间(看门狗用)

    // ---- _state 附加字段（对齐 EXE webpush_server._state）----
    private volatile String info = "";           // 附加状态文本(如重连进度), 空串=无
    private volatile String hrSource = "{}";     // 心率数据源状态JSON对象(中继中枢推送)
    private volatile boolean alarm = false;      // 远程报警标志(true=接收端循环响铃)
    private volatile String clipUrl = "";        // 报警剪辑流式播放地址(默认相机)
    private volatile String clips = "[]";        // 全量相机剪辑列表JSON数组 [{"cam","url"}]
    private volatile String alarmCam = "";       // 报警联动摄像头名(空=接收端用默认)
    private volatile String alarmRoom = "";      // 报警房间名
    private volatile String alarmLive = "";      // 报警相机HLS实时流m3u8地址(空=未就绪)

    // ---- 远程报警生命周期 ----
    private volatile AlarmHook alarmHook;
    private static final Runnable ALARM_AUTO_RESET = HeartBus::autoResetAlarm;
    private volatile long alarmStartedElapsed = 0;

    /** 波形环形缓冲: 60点@1Hz（对齐 EXE 60秒窗口, hr=0 为断点） */
    public static final int WAVE_POINTS = 60;
    private final int[] wave = new int[WAVE_POINTS];
    private int wavePos = 0;

    /** 滚动统计缓冲（对齐 EXE 10000 点上限） */
    private static final int STAT_MAX = 10000;
    private final int[] statBuf = new int[STAT_MAX];
    private int statPos = 0;
    private int statCount = 0;

    private HeartBus() {
    }

    private static String now() {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(new Date());
    }

    /** 心率数据入口（BLE 收到通知时调用）; hr<=0 忽略（对齐 EXE 过滤逻辑） */
    public void publishHeartRate(int hr) {
        if (hr <= 0) return;
        String ts;
        synchronized (this) {
            ts = now();
            heartRate = hr;
            timestamp = ts;
            status = "connected";
            lastDataMs = System.currentTimeMillis();
            wave[wavePos] = hr;
            wavePos = (wavePos + 1) % WAVE_POINTS;
            statBuf[statPos] = hr;
            statPos = (statPos + 1) % STAT_MAX;
            if (statCount < STAT_MAX) statCount++;
        }
        notifyListeners(hr, ts, "connected");
        fireStateChanged();
    }

    /**
     * 设备连接状态入口（对齐 EXE watch_device_connection 的断连/恢复推送）:
     * 断连 → 心率清0+波形打断点; 恢复 → 保留最后心率值。
     * 断流保护（对齐 EXE update()）: 中继源供数期间(hr_source.phase=active)抑制本端
     * 断连信号——节点持连手环时本端连接态丢失属正常, 误覆盖会让接收端状态在
     * 连接/断开间每2秒翻转（心率数字闪烁/波形夹断点）。
     */
    public void publishDeviceStatus(boolean connected) {
        if (!connected && isRelayActive()) {
            return;
        }
        int hr;
        String ts;
        synchronized (this) {
            ts = now();
            if (connected) {
                // 恢复时保留最后心率值（对齐 EXE update(last_heart_rate, now, "connected")）
            } else {
                heartRate = 0;
                wave[wavePos] = 0;
                wavePos = (wavePos + 1) % WAVE_POINTS;
            }
            status = connected ? "connected" : "disconnected";
            hr = heartRate;
        }
        notifyListeners(hr, ts, status);
        fireStateChanged();
    }

    private void notifyListeners(int hr, String ts, String st) {
        for (Listener l : listeners) {
            main.post(() -> l.onHeartRate(hr, ts, st));
        }
    }

    public void addListener(Listener l) {
        listeners.add(l);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public void addStateListener(StateListener l) {
        stateListeners.add(l);
    }

    public void removeStateListener(StateListener l) {
        stateListeners.remove(l);
    }

    /** 任意 _state 字段变化后调用: 通知 HeartServer 做 WS 全量广播 */
    private void fireStateChanged() {
        for (StateListener l : stateListeners) {
            main.post(l::onStateChanged);
        }
    }

    // ---- _state 附加字段写入（均对齐 EXE webpush_server 对应方法）----

    /** 仅更新附加状态文本（对齐 update_info: 未变化不重复广播） */
    public void setInfo(String text) {
        String v = text == null ? "" : text;
        if (v.equals(info)) return;
        info = v;
        fireStateChanged();
    }

    /** 中继数据源状态变化（P1 RelayHub 调用, 对齐 push_source）; json 须为合法 JSON 对象 */
    public void setHrSource(String json) {
        String v = (json == null || json.trim().isEmpty()) ? "{}" : json;
        if (v.equals(hrSource)) return;
        hrSource = v;
        fireStateChanged();
    }

    /** hr_source.phase 是否为 active（断流保护判定; P1 中继供数期间为 true） */
    private boolean isRelayActive() {
        return hrSource.contains("\"phase\":\"active\"");
    }

    /** 报警剪辑成型（对齐 push_clip）: 接收端显示回放按钮+相机tab */
    public void pushClip(String url, String clipsJson) {
        clipUrl = url == null ? "" : url;
        clips = (clipsJson == null || clipsJson.trim().isEmpty()) ? "[]" : clipsJson;
        fireStateChanged();
    }

    /** 报警HLS实时流地址就绪（或报警结束清空, 对齐 set_live_url: 未变化不重复广播） */
    public void setAlarmLive(String url) {
        String v = url == null ? "" : url;
        if (v.equals(alarmLive)) return;
        alarmLive = v;
        fireStateChanged();
    }

    public void setAlarmHook(AlarmHook hook) {
        alarmHook = hook;
    }

    // ---- 远程报警生命周期（对齐 trigger_alarm / _alarm_auto_clear; cancel 为安卓补齐闭环）----

    /**
     * 远程报警: alarm=true 并广播（接收端开始循环响铃）, seconds 后自动复位
     * （接收端收到 false 停铃）。报警开始清空上一次的 clip_url/clips/alarm_live
     * （防接收端误播旧流/旧片段）; 重复触发仅刷新自动复位计时（旧任务被移除,
     * 不会提前复位新报警）。
     */
    public void triggerAlarm(int seconds, String cam, String room) {
        alarm = true;
        clipUrl = "";
        clips = "[]";
        alarmCam = cam == null ? "" : cam;
        alarmRoom = room == null ? "" : room;
        alarmLive = "";
        alarmStartedElapsed = android.os.SystemClock.elapsedRealtime();
        // 重复报警: 先移除上一次的自动复位任务再起新的
        main.removeCallbacks(ALARM_AUTO_RESET);
        main.postDelayed(ALARM_AUTO_RESET, Math.max(1, seconds) * 1000L);
        fireStateChanged();
        AlarmHook h = alarmHook;
        if (h != null) {
            h.onAlarmStart(alarmCam, alarmRoom);
        }
    }

    /** 手动取消报警（对齐安卓补齐的 POST /api/cancel_alarm）: 立即复位并停铃 */
    public void cancelAlarm() {
        main.removeCallbacks(ALARM_AUTO_RESET);
        if (!alarm) return;
        alarm = false;
        fireStateChanged();
        AlarmHook h = alarmHook;
        if (h != null) {
            h.onAlarmEnd();
        }
    }

    /** 报警窗口到期自动复位: alarm=false 并广播, 接收端停铃 */
    private static void autoResetAlarm() {
        HeartBus bus = get();
        if (bus.alarm) {
            bus.alarm = false;
            bus.fireStateChanged();
        }
        AlarmHook h = bus.alarmHook;
        if (h != null) {
            h.onAlarmEnd();
        }
    }

    // ---- 快照读取 ----

    public int getHeartRate() {
        return heartRate;
    }

    public String getTimestamp() {
        return timestamp;
    }

    public String getStatus() {
        return status;
    }

    public String getDeviceName() {
        return deviceName;
    }

    public void setDeviceName(String name) {
        deviceName = name == null ? "" : name;
    }

    public void setWsClients(int n) {
        wsClients = n;
    }

    public int getWsClients() {
        return wsClients;
    }

    public long getLastDataMs() {
        return lastDataMs;
    }

    public String getInfo() {
        return info;
    }

    public String getHrSource() {
        return hrSource;
    }

    public boolean isAlarm() {
        return alarm;
    }

    public String getClipUrl() {
        return clipUrl;
    }

    public String getClips() {
        return clips;
    }

    public String getAlarmCam() {
        return alarmCam;
    }

    public String getAlarmRoom() {
        return alarmRoom;
    }

    public String getAlarmLive() {
        return alarmLive;
    }

    /** 报警已持续毫秒数（0=未在报警） */
    public long alarmElapsedMs() {
        return alarm ? android.os.SystemClock.elapsedRealtime() - alarmStartedElapsed : 0;
    }

    /**
     * _state 全字段状态快照（WS 连接即推/实时广播的 payload, 与 EXE webpush_server 完全一致）:
     * heart_rate/timestamp/status/device/info/alarm/clip_url/clips/hr_source/
     * alarm_cam/alarm_room/alarm_live
     */
    public String snapshotStateJson() {
        return "{\"heart_rate\":" + heartRate
                + ",\"timestamp\":\"" + jsonEscape(timestamp)
                + "\",\"status\":\"" + status
                + "\",\"device\":\"" + jsonEscape(deviceName)
                + "\",\"info\":\"" + jsonEscape(info)
                + "\",\"alarm\":" + alarm
                + ",\"clip_url\":\"" + jsonEscape(clipUrl)
                + "\",\"clips\":" + safeJsonArray(clips)
                + ",\"hr_source\":" + safeJsonObject(hrSource)
                + ",\"alarm_cam\":\"" + jsonEscape(alarmCam)
                + "\",\"alarm_room\":\"" + jsonEscape(alarmRoom)
                + "\",\"alarm_live\":\"" + jsonEscape(alarmLive)
                + "\"}";
    }

    /** 全字段快照 + clients（仅 HTTP /api/heartrate 用, 与 EXE 一致） */
    public String snapshotApiJson() {
        int idx = snapshotStateJson().lastIndexOf('}');
        return snapshotStateJson().substring(0, idx)
                + ",\"clients\":" + wsClients + "}";
    }

    /** 波形副本（60点, 旧→新顺序, hr=0 为断点/无数据） */
    public synchronized int[] getWave() {
        int[] out = new int[WAVE_POINTS];
        for (int i = 0; i < WAVE_POINTS; i++) {
            out[i] = wave[(wavePos + i) % WAVE_POINTS];
        }
        return out;
    }

    /**
     * 统计: {min, max, avg(四舍五入), count}（对标 EXE get_heart_rate_stats）; 无数据返回 null
     */
    public synchronized int[] getStats() {
        if (statCount == 0) return null;
        int min = Integer.MAX_VALUE;
        int max = 0;
        long sum = 0;
        for (int i = 0; i < statCount; i++) {
            int v = statBuf[i];
            if (v < min) min = v;
            if (v > max) max = v;
            sum += v;
        }
        return new int[]{min, max, (int) Math.round((double) sum / statCount), statCount};
    }

    private static String jsonEscape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /** clips 字段防非法JSON注入快照: 非法时回退 "[]"（内部来源可控, 双保险） */
    private static String safeJsonArray(String raw) {
        if (raw != null && raw.startsWith("[")) return raw;
        return "[]";
    }

    /** hr_source 字段防非法JSON注入快照: 非法时回退 "{}"（双保险） */
    private static String safeJsonObject(String raw) {
        if (raw != null && raw.startsWith("{")) return raw;
        return "{}";
    }
}
