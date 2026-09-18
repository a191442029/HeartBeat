package com.hrmlink.hrserver;

import android.os.Handler;
import android.os.Looper;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 心率数据总线（单例，对标 EXE 主窗口 on_heart_rate_updated 数据流）
 *
 * 生产者: BleManager（publishHeartRate / publishDeviceStatus）
 * 消费者: HeartServer(WS广播) / AlarmEngine(告警) / InfluxWriter(写库) /
 *         MqttPublisher(发布) / 监测UI(数字+波形+状态)
 *
 * 监听器回调统一在主线程派发；消费方做网络/磁盘操作必须自行切到工作线程。
 */
public final class HeartBus {

    /** 数据监听器: hr<=0 表示断连状态推送 */
    public interface Listener {
        void onHeartRate(int hr, String ts, String status);
    }

    private static final HeartBus sInstance = new HeartBus();

    public static HeartBus get() {
        return sInstance;
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();

    private volatile int heartRate = 0;          // 0=无效/未连接
    private volatile String timestamp = "";
    private volatile String status = "disconnected";
    private volatile String deviceName = "";
    private volatile int wsClients = 0;          // 由 HeartServer 维护
    private volatile long lastDataMs = 0;        // 最近一次真实心率数据时间(看门狗用)

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
    }

    /**
     * 设备连接状态入口（对齐 EXE watch_device_connection 的断连/恢复推送）:
     * 断连 → 心率清0+波形打断点; 恢复 → 保留最后心率值。
     */
    public void publishDeviceStatus(boolean connected) {
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

    /**
     * 4字段状态快照（WS 连接即推/实时广播的 payload, 与 EXE webpush_server 完全一致）:
     * {"heart_rate":N,"timestamp":"...","status":"...","device":"..."}
     */
    public String snapshotStateJson() {
        return "{\"heart_rate\":" + heartRate
                + ",\"timestamp\":\"" + jsonEscape(timestamp)
                + "\",\"status\":\"" + status
                + "\",\"device\":\"" + jsonEscape(deviceName) + "\"}";
    }

    /** 5字段快照（含 clients, 仅 HTTP /api/heartrate 用, 与 EXE 一致） */
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
}
