package com.hrmlink.hrserver;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 配置存取（键名对应 EXE config.ini 各节, SharedPreferences 持久化）
 *
 * 注意: 所有键的类型固定, getInt 的键绝不能用 putStr 写入, 否则 ClassCastException。
 * 默认值与 EXE config.ini / system_utils init_config 保持一致。
 */
public final class Prefs {

    private static final String FILE = "config";

    // ---- [Device] 设备 ----
    public static final String DEV_NAME = "dev_name";                  // 最后连接的手环名
    public static final String DEV_ADDRESS = "dev_address";            // 最后连接的手环 MAC
    public static final String DEV_AUTO_CONNECT = "dev_auto_connect";  // 服务启动自动连最后设备(长期通电场景, 默认true)
    public static final String FAV_DEVICES = "fav_devices";            // 收藏设备JSON数组 [{"name":"..","address":".."},..](对齐EXE favorite_devices)

    // ---- 数据服务（对应 EXE [Tailscale] 节）----
    public static final String SRV_ENABLED = "srv_enabled";   // 数据服务总开关, 默认 true
    public static final String SRV_ADDRESS = "srv_address";   // "auto"=自动探测Tailscale IP, 或手填IP
    public static final String SRV_PORT = "srv_port";         // 默认 8765

    // ---- [Push] 告警规则 ----
    public static final String LOCAL_ALARM_ENABLED = "local_alarm_enabled";        // 心率告警本地响铃开关, 默认true
    public static final String PUSH_MAX_HR = "push_max_hr";                        // 上限, 0=不检测, 默认150
    public static final String PUSH_MIN_HR = "push_min_hr";                        // 下限, 0=不检测, 默认45
    public static final String PUSH_ABNORMAL_DURATION = "push_abnormal_duration";  // 超限持续秒数, 默认600
    public static final String PUSH_COOLDOWN_SECONDS = "push_cooldown_seconds";    // 冷却秒数, 0=不冷却, 默认300
    public static final String PUSH_PERIODS = "push_periods";                      // 时段规则JSON数组, 默认"[]"
    // 疑似心律不齐（对标 EXE irregularity_detector + PushSettingUI 参数范围）
    public static final String IRR_ENABLED = "irr_enabled";
    public static final String IRR_WINDOW_SECONDS = "irr_window_seconds";    // 判定窗口秒 30-300, 默认300
    public static final String IRR_SD_THRESHOLD = "irr_sd_threshold";        // 标准差波动阈值 1-50, 默认50
    public static final String IRR_JUMP_BPM = "irr_jump_bpm";                // 大幅跳变BPM 1-50, 默认5
    public static final String IRR_JUMP_RATIO_PCT = "irr_jump_ratio_pct";    // 跳变占比% 1-100, 默认100
    public static final String IRR_REST_MAX_HR = "irr_rest_max_hr";          // 静息上限 30-200, 默认200
    public static final String IRR_SUSTAIN_WINDOWS = "irr_sustain_windows";  // 连续确认窗口数, 默认2
    public static final String IRR_COOLDOWN_MINUTES = "irr_cooldown_minutes";// 冷却分钟, 默认10
    // Bark 渠道
    public static final String BARK_ENABLED = "bark_enabled";
    public static final String BARK_DEVICE_KEY = "bark_device_key";
    public static final String BARK_SERVER = "bark_server";
    public static final String BARK_LEVEL = "bark_level";        // active/timeSensitive/passive/critical
    public static final String BARK_SOUND = "bark_sound";
    public static final String BARK_GROUP = "bark_group";
    // ntfy 渠道
    public static final String NTFY_ENABLED = "ntfy_enabled";
    public static final String NTFY_TOPIC = "ntfy_topic";
    public static final String NTFY_SERVER = "ntfy_server";
    public static final String NTFY_PRIORITY = "ntfy_priority";  // 1-5, 0=不传
    public static final String NTFY_TAGS = "ntfy_tags";
    public static final String NTFY_TOKEN = "ntfy_token";        // Bearer 鉴权
    // MeoW 渠道
    public static final String MEOW_ENABLED = "meow_enabled";
    public static final String MEOW_NICKNAME = "meow_nickname";

    // ---- [InfluxDB] ----
    public static final String INFLUX_ENABLED = "influx_enabled";
    public static final String INFLUX_URL = "influx_url";      // http://ip:8086
    public static final String INFLUX_TOKEN = "influx_token";
    public static final String INFLUX_ORG = "influx_org";
    public static final String INFLUX_BUCKET = "influx_bucket";

    // ---- [MQTT] ----
    public static final String MQTT_BROKER = "mqtt_broker";
    public static final String MQTT_PORT = "mqtt_port";                    // 默认1883
    public static final String MQTT_USERNAME = "mqtt_username";
    public static final String MQTT_PASSWORD = "mqtt_password";
    public static final String MQTT_CLIENT_ID = "mqtt_client_id";              // 默认 heartbeat_monitor
    public static final String MQTT_TOPIC = "mqtt_topic";                      // 默认 homeassistant/sensor/heartrate/state
    public static final String MQTT_DISCOVERY_TOPIC = "mqtt_discovery_topic";  // 默认 homeassistant/sensor/heartrate/config
    public static final String MQTT_DISCOVERY_ENABLED = "mqtt_discovery_enabled"; // 默认 true

    private Prefs() {
    }

    public static SharedPreferences sp(Context c) {
        return c.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public static String getStr(Context c, String k, String def) {
        return sp(c).getString(k, def);
    }

    public static int getInt(Context c, String k, int def) {
        return sp(c).getInt(k, def);
    }

    public static boolean getBool(Context c, String k, boolean def) {
        return sp(c).getBoolean(k, def);
    }

    public static void putStr(Context c, String k, String v) {
        sp(c).edit().putString(k, v).apply();
    }

    public static void putInt(Context c, String k, int v) {
        sp(c).edit().putInt(k, v).apply();
    }

    public static void putBool(Context c, String k, boolean v) {
        sp(c).edit().putBoolean(k, v).apply();
    }
}
