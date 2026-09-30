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
    public static final String SRV_ADDRESS = "srv_address";   // "auto"=绑0.0.0.0(LAN/Tailscale双通), 或手填IP
    public static final String SRV_PORT = "srv_port";         // 默认 8765

    // ---- [Camera] 摄像头 ----
    public static final String CAMERAS_JSON = "cameras_json"; // [{"name":"主卧","url":"rtsp://.../stream2","alarm_enabled":true,"is_default":true}]
    public static final String CAMERA_ENABLED = "camera_enabled"; // 摄像头链路总开关, 默认 false（无 ffmpeg/无配置时保持关闭）

    // ---- [Push] 告警规则 ----
    public static final String LOCAL_ALARM_ENABLED = "local_alarm_enabled";        // 心率告警本地响铃开关, 默认true
    public static final String REMOTE_ALARM_ENABLED = "alarm_remote_enabled";      // 远程报警(接收端响铃)开关, 默认true
    public static final String ALARM_SECONDS = "alarm_seconds";                    // 报警窗口秒数(到期自动复位), 默认10
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

    // ---- [ESP32 中继中枢] RelayHub(对齐 EXE config.ini [esp32_relay]) ----
    public static final String RELAY_ENABLED = "relay_enabled";              // 中继中枢开关, 默认 false
    public static final String RELAY_TOKEN = "relay_token";                  // /relay WS令牌, 默认 HRMLink-ESP32-2025
    public static final String RELAY_THRESHOLD_DROP = "relay_threshold_drop";// 切换阈值(dBm), 默认-75
    public static final String RELAY_HYSTERESIS_DB = "relay_hysteresis_db";  // 切换迟滞dB, 默认10
    public static final String RELAY_MIN_RSSI = "relay_min_rssi";            // 最低可连RSSI, 默认-80
    public static final String RELAY_STALE_SECONDS = "relay_stale_seconds";  // 接管判定秒数, 默认30
    public static final String RELAY_FREEZE_CYCLES = "relay_freeze_cycles";  // 切换判定周期数, 默认3
    public static final String RELAY_NODE_BIASES = "relay_node_biases";      // 节点偏差JSON(信号标定, P1仅存储不标定)
    public static final String ROOM_CAMERA_MAP = "room_camera_map";          // 报警房间→摄像头绑定JSON{节点名:相机名}, 空=全用默认相机

    // ---- 大屏显示(设置→大屏显示组, 即改即生效; 对应 ui_preview.html 定稿) ----
    public static final String UI_CARD_BAND = "ui_card_band";      // 底部卡片: 手环
    public static final String UI_CARD_WS = "ui_card_ws";          // 底部卡片: WS客户端
    public static final String UI_CARD_TS = "ui_card_tailscale";   // 底部卡片: Tailscale
    public static final String UI_CARD_MQTT = "ui_card_mqtt";      // 底部卡片: MQTT
    public static final String UI_CARD_INFLUX = "ui_card_influx";  // 底部卡片: InfluxDB
    public static final String UI_CARD_CAM = "ui_card_cam";        // 底部卡片: 摄像头
    public static final String UI_CARD_RELAY = "ui_card_relay";    // 底部卡片: 中继节点
    public static final String UI_HR_SIZE = "ui_hr_size";          // 心率数字字号 sp, 默认150
    public static final String UI_HEART_ICON = "ui_heart_icon";    // 心形图标显隐
    public static final String UI_MINICAM = "ui_minicam";          // 小块视频口显隐
    public static final String UI_SRC_ROW = "ui_src_row";          // 数据源角标行显隐

    // ---- 摄像头细节(设置→摄像头组; 真机联调后逐步接线) ----
    public static final String CLIP_HD_ENABLED = "clip_hd_enabled"; // 高清剪辑开关(主码流剪辑; 多路限1-2路提示)
    public static final String LIVE_STREAM = "live_stream";         // 默认拉流码流: sub=子码流(推荐)/main=主码流, 对齐 stream_manager
    public static final String DETECT_MODE = "detect_mode";         // 侦测方式: frame_diff(当前P2)/onvif_event/npu_human(预留)

    // ---- 存档(设置→存储组; 每项独立启用+位置, 未挂载U盘自动回退板载仅对新文件生效) ----
    public static final String ARCH_CLIP_ENABLED = "arch_clip_enabled";      // 报警剪辑存档
    public static final String ARCH_CSV_ENABLED = "arch_csv_enabled";        // 心率CSV日志存档
    public static final String ARCH_PUSHLOG_ENABLED = "arch_pushlog_enabled";// 推送记录存档
    public static final String CLIP_STORAGE = "clip_storage";                // internal/usb
    public static final String CSV_STORAGE = "csv_storage";
    public static final String PUSHLOG_STORAGE = "pushlog_storage";
    public static final String CSV_RETENTION_DAYS = "csv_retention_days";    // CSV保留天数, 默认30

    private Prefs() {
    }

    // ---- 参数同步(与 EXE 双向, 对齐 HRMLink功能业务逻辑梳理.md §6.5) ----
    public static final String SETTINGS_REV = "settings_rev";   // 配置版本号: 本地改动批次+1, LWW 仲裁用
    public static final String SYNC_TS = "sync_ts";             // 本端生效配置时间戳ms(字符串存), 同 rev 决胜
    public static final String SYNC_TOKEN = "sync_token";       // 同步令牌, 两端 config 各存同值(Bearer 鉴权)
    public static final String SYNC_PUSHED_JSON = "sync_pushed_json"; // 最后生效快照基线(差分检测本地改动)

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
