package com.hrmlink.hrserver;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;

/**
 * 设置页(阶段4-B, 1:1 对标 EXE PushSettingUI/TailscaleSettingUI/InfluxDBSettingUI/MQTTSettingUI/DevCtrl):
 *  - 设备管理: 扫描/点选/保存并连接(服务重启生效)/手动断开/自动连接
 *  - 数据服务: 开关/绑定地址(auto或IP)/端口
 *  - 告警规则: 默认规则 + 疑似心律不齐8参数 + 自定义时段规则(JSON, EXE同构)
 *  - 推送渠道: MeoW/Bark/ntfy(测试) + InfluxDB(测试) + MQTT(仅保存)
 *  - 推送记录入口 + 页面底部统一保存
 * 参数键全部走 Prefs 常量; 修改由监测服务重启时生效(AlarmEngine/Writer/Server 均快照式读配置)。
 */
public class SettingsActivity extends Activity {

    private static final int REQ_BLE_PERMS = 1001;

    // Bark 级别下标 → 存储值(对齐 EXE QComboBox currentData)
    private static final String[] BARK_LEVEL_VALUES = {"", "active", "timeSensitive", "passive", "critical"};

    private EditText editSrvAddress, editSrvPort;
    private EditText editPushMax, editPushMin, editPushDuration, editPushCooldown;
    private CheckBox chkLocalAlarm;
    private CheckBox chkIrrEnabled;
    private EditText editIrrWindow, editIrrSd, editIrrJump, editIrrRatio, editIrrRest, editIrrSustain, editIrrCooldown;
    private EditText editMeowNick, editBarkKey, editBarkServer, editBarkSound, editBarkGroup;
    private Spinner spinBarkLevel;
    private EditText editNtfyTopic, editNtfyServer, editNtfyTags, editNtfyToken;
    private Spinner spinNtfyPriority;
    private EditText editInfluxUrl, editInfluxToken, editInfluxOrg, editInfluxBucket;
    private EditText editMqttBroker, editMqttPort, editMqttUser, editMqttPass, editMqttTopic, editMqttDiscTopic;
    private CheckBox chkSrvEnabled, chkAutoConnect, chkMeowEnabled, chkBarkEnabled, chkNtfyEnabled,
            chkInfluxEnabled, chkMqttDisc;
    private TextView txtDevSaved, txtScanStatus;
    private android.widget.LinearLayout listPeriods;

    private final BleManager ble = new BleManager();
    private final ArrayList<String[]> devices = new ArrayList<>();        // {name, address}
    private final ArrayList<String[]> favorites = new ArrayList<>();      // 收藏设备(对齐EXE favorite_devices)
    private String selName = "", selAddr = "";
    private LinearLayout listScan, listFav;
    private TextView txtFavTitle;
    private Button btnAddFav;
    private boolean pendingScan = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        bindViews();
        loadAll();

        // ---- 设备管理 ----
        findViewById(R.id.btn_scan).setOnClickListener(v -> onScanClicked());
        findViewById(R.id.btn_save_connect).setOnClickListener(v -> saveAndConnect());
        findViewById(R.id.btn_disconnect).setOnClickListener(v -> {
            HeartRateService.stop(this);
            Toast.makeText(this, "监测已停止(重新保存/收藏设备或重启APP可恢复)", Toast.LENGTH_SHORT).show();
        });

        // ---- 推送记录入口 ----
        findViewById(R.id.btn_view_records).setOnClickListener(v ->
                startActivity(new Intent(this, RecordsActivity.class)));

        // ---- 测试按钮(先保存再测, 渠道模块从 Prefs 读参) ----
        findViewById(R.id.btn_test_meow).setOnClickListener(v -> testChannel("meow"));
        findViewById(R.id.btn_test_bark).setOnClickListener(v -> testChannel("bark"));
        findViewById(R.id.btn_test_ntfy).setOnClickListener(v -> testChannel("ntfy"));
        findViewById(R.id.btn_test_influx).setOnClickListener(v -> testInflux());

        findViewById(R.id.btn_add_period).setOnClickListener(v -> addPeriodRow(null));
        findViewById(R.id.btn_save_all).setOnClickListener(v -> saveAll());
        findViewById(R.id.btn_check_update).setOnClickListener(v -> checkUpdate());

        // ---- 悬浮窗开关(自主页迁入): 勾选→检查权限→显示; 取消→隐藏 ----
        final CheckBox chkOverlay = findViewById(R.id.chk_overlay);
        chkOverlay.setChecked(Prefs.getBool(this, OverlayManager.KEY_OVERLAY_ENABLED, false));
        chkOverlay.setOnCheckedChangeListener((button, isChecked) -> {
            if (isChecked) {
                if (!Settings.canDrawOverlays(this)) {
                    Toast.makeText(this, "请先授予\"显示在应用上层\"权限", Toast.LENGTH_LONG).show();
                    button.setChecked(false); // 撤回勾选, 授权回来后用户再勾
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:" + getPackageName())));
                    return;
                }
                Prefs.putBool(this, OverlayManager.KEY_OVERLAY_ENABLED, true);
                OverlayManager.get(this).show();
            } else {
                Prefs.putBool(this, OverlayManager.KEY_OVERLAY_ENABLED, false);
                OverlayManager.get(this).hide();
            }
        });
    }

    private void bindViews() {
        txtDevSaved = findViewById(R.id.txt_dev_saved);
        txtScanStatus = findViewById(R.id.txt_scan_status);
        listScan = findViewById(R.id.list_scan_devices);
        listFav = findViewById(R.id.list_fav_devices);
        txtFavTitle = findViewById(R.id.txt_fav_title);
        btnAddFav = findViewById(R.id.btn_add_favorite);
        btnAddFav.setOnClickListener(v -> addFavorite());
        listPeriods = findViewById(R.id.list_periods);
        chkAutoConnect = findViewById(R.id.chk_auto_connect);
        chkSrvEnabled = findViewById(R.id.chk_srv_enabled);
        editSrvAddress = findViewById(R.id.edit_srv_address);
        editSrvPort = findViewById(R.id.edit_srv_port);
        editPushMax = findViewById(R.id.edit_push_max);
        editPushMin = findViewById(R.id.edit_push_min);
        editPushDuration = findViewById(R.id.edit_push_duration);
        editPushCooldown = findViewById(R.id.edit_push_cooldown);
        chkLocalAlarm = findViewById(R.id.chk_local_alarm);
        chkIrrEnabled = findViewById(R.id.chk_irr_enabled);
        editIrrWindow = findViewById(R.id.edit_irr_window);
        editIrrSd = findViewById(R.id.edit_irr_sd);
        editIrrJump = findViewById(R.id.edit_irr_jump_bpm);
        editIrrRatio = findViewById(R.id.edit_irr_ratio);
        editIrrRest = findViewById(R.id.edit_irr_rest);
        editIrrSustain = findViewById(R.id.edit_irr_sustain);
        editIrrCooldown = findViewById(R.id.edit_irr_cooldown);
        chkMeowEnabled = findViewById(R.id.chk_meow_enabled);
        editMeowNick = findViewById(R.id.edit_meow_nickname);
        chkBarkEnabled = findViewById(R.id.chk_bark_enabled);
        editBarkKey = findViewById(R.id.edit_bark_key);
        editBarkServer = findViewById(R.id.edit_bark_server);
        spinBarkLevel = findViewById(R.id.spin_bark_level);
        editBarkSound = findViewById(R.id.edit_bark_sound);
        editBarkGroup = findViewById(R.id.edit_bark_group);
        chkNtfyEnabled = findViewById(R.id.chk_ntfy_enabled);
        editNtfyTopic = findViewById(R.id.edit_ntfy_topic);
        editNtfyServer = findViewById(R.id.edit_ntfy_server);
        spinNtfyPriority = findViewById(R.id.spin_ntfy_priority);
        editNtfyTags = findViewById(R.id.edit_ntfy_tags);
        editNtfyToken = findViewById(R.id.edit_ntfy_token);
        chkInfluxEnabled = findViewById(R.id.chk_influx_enabled);
        editInfluxUrl = findViewById(R.id.edit_influx_url);
        editInfluxToken = findViewById(R.id.edit_influx_token);
        editInfluxOrg = findViewById(R.id.edit_influx_org);
        editInfluxBucket = findViewById(R.id.edit_influx_bucket);
        editMqttBroker = findViewById(R.id.edit_mqtt_broker);
        editMqttPort = findViewById(R.id.edit_mqtt_port);
        editMqttUser = findViewById(R.id.edit_mqtt_username);
        editMqttPass = findViewById(R.id.edit_mqtt_password);
        editMqttTopic = findViewById(R.id.edit_mqtt_topic);
        chkMqttDisc = findViewById(R.id.chk_mqtt_discovery);
        editMqttDiscTopic = findViewById(R.id.edit_mqtt_discovery_topic);
    }

    // ==================== 加载 ====================

    private void loadAll() {
        // 设备
        String dName = Prefs.getStr(this, Prefs.DEV_NAME, "");
        String dAddr = Prefs.getStr(this, Prefs.DEV_ADDRESS, "");
        txtDevSaved.setText(dName.isEmpty() ? "未保存设备" : "当前设备: " + dName + " (" + dAddr + ")");
        chkAutoConnect.setChecked(Prefs.getBool(this, Prefs.DEV_AUTO_CONNECT, true));
        loadFavorites();
        renderScanList();   // 初始占位提示(尚未扫描)

        // 数据服务(对齐 EXE Tailscale: enabled/address=auto/port=8765)
        chkSrvEnabled.setChecked(Prefs.getBool(this, Prefs.SRV_ENABLED, true));
        editSrvAddress.setText(Prefs.getStr(this, Prefs.SRV_ADDRESS, "auto"));
        editSrvPort.setText(String.valueOf(Prefs.getInt(this, Prefs.SRV_PORT, 8765)));

        // 告警默认规则(EXE: 上/下限0-999, 持续1-600, 冷却0-1440分钟→此处按Prefs契约用秒)
        editPushMax.setText(String.valueOf(Prefs.getInt(this, Prefs.PUSH_MAX_HR, 150)));
        editPushMin.setText(String.valueOf(Prefs.getInt(this, Prefs.PUSH_MIN_HR, 45)));
        editPushDuration.setText(String.valueOf(Prefs.getInt(this, Prefs.PUSH_ABNORMAL_DURATION, 600)));
        editPushCooldown.setText(String.valueOf(Prefs.getInt(this, Prefs.PUSH_COOLDOWN_SECONDS, 300)));

        // 本地报警音(心率告警设备现场响铃, 默认开)
        chkLocalAlarm.setChecked(Prefs.getBool(this, Prefs.LOCAL_ALARM_ENABLED, true));

        // 疑似心律不齐(8参数, 默认值对齐 Prefs 注释)
        chkIrrEnabled.setChecked(Prefs.getBool(this, Prefs.IRR_ENABLED, false));
        editIrrWindow.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_WINDOW_SECONDS, 300)));
        editIrrSd.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_SD_THRESHOLD, 50)));
        editIrrJump.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_JUMP_BPM, 5)));
        editIrrRatio.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_JUMP_RATIO_PCT, 100)));
        editIrrRest.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_REST_MAX_HR, 200)));
        editIrrSustain.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_SUSTAIN_WINDOWS, 2)));
        editIrrCooldown.setText(String.valueOf(Prefs.getInt(this, Prefs.IRR_COOLDOWN_MINUTES, 10)));

        // MeoW
        chkMeowEnabled.setChecked(Prefs.getBool(this, Prefs.MEOW_ENABLED, false));
        editMeowNick.setText(Prefs.getStr(this, Prefs.MEOW_NICKNAME, ""));
        // Bark
        chkBarkEnabled.setChecked(Prefs.getBool(this, Prefs.BARK_ENABLED, false));
        editBarkKey.setText(Prefs.getStr(this, Prefs.BARK_DEVICE_KEY, ""));
        editBarkServer.setText(Prefs.getStr(this, Prefs.BARK_SERVER, ""));
        spinBarkLevel.setSelection(levelIndex(Prefs.getStr(this, Prefs.BARK_LEVEL, "")));
        editBarkSound.setText(Prefs.getStr(this, Prefs.BARK_SOUND, ""));
        editBarkGroup.setText(Prefs.getStr(this, Prefs.BARK_GROUP, ""));
        // ntfy
        chkNtfyEnabled.setChecked(Prefs.getBool(this, Prefs.NTFY_ENABLED, false));
        editNtfyTopic.setText(Prefs.getStr(this, Prefs.NTFY_TOPIC, ""));
        editNtfyServer.setText(Prefs.getStr(this, Prefs.NTFY_SERVER, ""));
        spinNtfyPriority.setSelection(clamp(Prefs.getInt(this, Prefs.NTFY_PRIORITY, 0), 0, 5));
        editNtfyTags.setText(Prefs.getStr(this, Prefs.NTFY_TAGS, ""));
        editNtfyToken.setText(Prefs.getStr(this, Prefs.NTFY_TOKEN, ""));
        // InfluxDB
        chkInfluxEnabled.setChecked(Prefs.getBool(this, Prefs.INFLUX_ENABLED, false));
        editInfluxUrl.setText(Prefs.getStr(this, Prefs.INFLUX_URL, ""));
        editInfluxToken.setText(Prefs.getStr(this, Prefs.INFLUX_TOKEN, ""));
        editInfluxOrg.setText(Prefs.getStr(this, Prefs.INFLUX_ORG, ""));
        editInfluxBucket.setText(Prefs.getStr(this, Prefs.INFLUX_BUCKET, ""));
        // MQTT(Prefs 无独立 enabled 键, 配好 broker 即生效; client_id 固定 heartbeat_monitor 由服务端管理)
        editMqttBroker.setText(Prefs.getStr(this, Prefs.MQTT_BROKER, "localhost"));
        editMqttPort.setText(String.valueOf(Prefs.getInt(this, Prefs.MQTT_PORT, 1883)));
        editMqttUser.setText(Prefs.getStr(this, Prefs.MQTT_USERNAME, ""));
        editMqttPass.setText(Prefs.getStr(this, Prefs.MQTT_PASSWORD, ""));
        editMqttTopic.setText(Prefs.getStr(this, Prefs.MQTT_TOPIC, "homeassistant/sensor/heartrate/state"));
        chkMqttDisc.setChecked(Prefs.getBool(this, Prefs.MQTT_DISCOVERY_ENABLED, true));
        editMqttDiscTopic.setText(Prefs.getStr(this, Prefs.MQTT_DISCOVERY_TOPIC, "homeassistant/sensor/heartrate/config"));

        // 时段规则
        JSONArray arr = parsePeriodsJson(Prefs.getStr(this, Prefs.PUSH_PERIODS, "[]"));
        for (int i = 0; i < arr.length(); i++) {
            JSONObject p = arr.optJSONObject(i);
            if (p != null) addPeriodRow(p);
        }
    }

    /** Bark 级别值→下标 */
    private static int levelIndex(String v) {
        for (int i = 0; i < BARK_LEVEL_VALUES.length; i++) {
            if (BARK_LEVEL_VALUES[i].equals(v)) return i;
        }
        return 0;
    }

    private static int clamp(int v, int lo, int hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    private static JSONArray parsePeriodsJson(String raw) {
        try {
            return new JSONArray(raw == null || raw.trim().isEmpty() ? "[]" : raw);
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    // ==================== 时段规则行 ====================

    /** 添加一行规则; p 为已存规则(缺省用 EXE 默认: 22:00-07:00, 上80/下40, 持续10秒, 冷却10分) */
    private void addPeriodRow(JSONObject p) {
        View row = LayoutInflater.from(this).inflate(R.layout.setting_period_row, listPeriods, false);
        EditText start = row.findViewById(R.id.edit_period_start);
        EditText end = row.findViewById(R.id.edit_period_end);
        EditText max = row.findViewById(R.id.edit_period_max);
        EditText min = row.findViewById(R.id.edit_period_min);
        EditText sustain = row.findViewById(R.id.edit_period_sustain);
        EditText cooldown = row.findViewById(R.id.edit_period_cooldown);
        if (p != null) {
            ((CheckBox) row.findViewById(R.id.chk_period_enabled)).setChecked(p.optBoolean("enabled", true));
            start.setText(p.optString("start", "22:00"));
            end.setText(p.optString("end", "07:00"));
            // 兼容 EXE 原生键 max/min/sustain 与别名 max_hr/min_hr/duration
            max.setText(String.valueOf(p.optInt("max", p.optInt("max_hr", 80))));
            min.setText(String.valueOf(p.optInt("min", p.optInt("min_hr", 40))));
            sustain.setText(String.valueOf(p.optInt("sustain", p.optInt("duration", 10))));
            cooldown.setText(String.valueOf(p.optInt("cooldown", 600) / 60));   // 存储为秒, UI 显示分钟
        } else {
            max.setText("80");
            min.setText("40");
            sustain.setText("10");
            cooldown.setText("10");
        }
        row.findViewById(R.id.btn_period_del).setOnClickListener(v -> listPeriods.removeView(row));
        listPeriods.addView(row);
    }

    /**
     * 收集并校验时段规则(校验范围对齐 EXE _collect_periods: 持续1-600秒, 冷却0-1440分钟),
     * 序列化为 EXE 同构 JSON: {"enabled","start","end","max","min","sustain","cooldown(秒)"}
     *
     * @return 失败返回 null(已 setError/Toast)
     */
    private JSONArray collectPeriods() {
        JSONArray out = new JSONArray();
        for (int i = 0; i < listPeriods.getChildCount(); i++) {
            View row = listPeriods.getChildAt(i);
            boolean enabled = ((CheckBox) row.findViewById(R.id.chk_period_enabled)).isChecked();
            String start = text(row, R.id.edit_period_start);
            String end = text(row, R.id.edit_period_end);
            Integer max = intOf(row, R.id.edit_period_max);
            Integer min = intOf(row, R.id.edit_period_min);
            Integer sustain = intOf(row, R.id.edit_period_sustain);
            Integer cooldownMin = intOf(row, R.id.edit_period_cooldown);
            String where = "时段规则第" + (i + 1) + "行: ";
            if (max == null || min == null || sustain == null || cooldownMin == null) {
                fail(where + "上限/下限/持续/冷却必须为整数", row.findViewById(R.id.edit_period_max));
                return null;
            }
            int[] se = parseHhmm(start);
            int[] ee = parseHhmm(end);
            if (se == null || ee == null) {
                fail(where + "时间格式应为 HH:MM, 如 22:00", row.findViewById(R.id.edit_period_start));
                return null;
            }
            if (start.equals(end)) {
                fail(where + "时段起止不能相同", row.findViewById(R.id.edit_period_end));
                return null;
            }
            if (max < 0 || min < 0 || (max > 0 && min > 0 && min >= max)) {
                fail(where + "上下限无效(需下限<上限, 或填0表示不检测)", row.findViewById(R.id.edit_period_min));
                return null;
            }
            if (sustain < 1 || sustain > 600 || cooldownMin < 0 || cooldownMin > 1440) {
                fail(where + "持续秒数(1-600)或冷却分钟(0-1440)超出范围", row.findViewById(R.id.edit_period_sustain));
                return null;
            }
            try {
                JSONObject o = new JSONObject();
                o.put("enabled", enabled);
                o.put("start", start);
                o.put("end", end);
                o.put("max", max);
                o.put("min", min);
                o.put("sustain", sustain);
                o.put("cooldown", cooldownMin * 60);   // EXE: UI分钟×60存秒
                out.put(o);
            } catch (Exception e) {
                fail(where + "序列化失败", row.findViewById(R.id.edit_period_start));
                return null;
            }
        }
        return out;
    }

    /** 解析 HH:MM 为 {时,分}; 非法返回 null */
    private static int[] parseHhmm(String s) {
        s = s == null ? "" : s.trim();
        int c = s.indexOf(':');
        if (c <= 0 || c == s.length() - 1) return null;
        try {
            int h = Integer.parseInt(s.substring(0, c).trim());
            int m = Integer.parseInt(s.substring(c + 1).trim());
            if (h < 0 || h > 23 || m < 0 || m > 59) return null;
            return new int[]{h, m};
        } catch (NumberFormatException e) {
            return null;
        }
    }

    // ==================== 保存 ====================

    private void saveAll() {
        // ---- 校验(对齐 EXE 保存前校验) ----
        String addr = text(editSrvAddress).trim();
        if (addr.isEmpty()) addr = "auto";
        Integer srvPort = intOf(editSrvPort);
        if (srvPort == null || srvPort < 1 || srvPort > 65535) {
            fail("端口必须为 1-65535 的整数", editSrvPort);
            return;
        }
        if (!"auto".equalsIgnoreCase(addr) && !isIp(addr)) {
            fail("绑定地址必须为 auto 或合法IP地址", editSrvAddress);
            return;
        }
        Integer maxHr = intOf(editPushMax);
        Integer minHr = intOf(editPushMin);
        Integer duration = intOf(editPushDuration);
        Integer cooldown = intOf(editPushCooldown);
        if (maxHr == null || minHr == null || maxHr < 0 || maxHr > 999 || minHr < 0 || minHr > 999) {
            fail("心率上限/下限须为 0-999 的整数(0=不检测)", editPushMax);
            return;
        }
        if (minHr > 0 && maxHr > 0 && minHr >= maxHr) {
            fail("心率下限必须小于上限(填0表示该项不检测)", editPushMin);
            return;
        }
        if (duration == null || duration < 1 || duration > 600) {
            fail("超限持续判定须为 1-600 秒", editPushDuration);
            return;
        }
        if (cooldown == null || cooldown < 0 || cooldown > 86400) {
            fail("告警冷却须为 0-86400 秒(0=不冷却)", editPushCooldown);
            return;
        }
        // 心律不齐(范围对齐 EXE PushSettingUI/irregularity_detector)
        Integer irrWin = intOf(editIrrWindow);
        Integer irrSd = intOf(editIrrSd);
        Integer irrJump = intOf(editIrrJump);
        Integer irrRatio = intOf(editIrrRatio);
        Integer irrRest = intOf(editIrrRest);
        Integer irrSustain = intOf(editIrrSustain);
        Integer irrCd = intOf(editIrrCooldown);
        if (irrWin == null || irrWin < 30 || irrWin > 300) {
            fail("判定窗口须为 30-300 秒", editIrrWindow);
            return;
        }
        if (irrSd == null || irrSd < 1 || irrSd > 50) {
            fail("波动阈值须为 1-50 bpm", editIrrSd);
            return;
        }
        if (irrJump == null || irrJump < 1 || irrJump > 50) {
            fail("大幅跳变须为 1-50 bpm", editIrrJump);
            return;
        }
        if (irrRatio == null || irrRatio < 1 || irrRatio > 100) {
            fail("跳变占比须为 1-100 %", editIrrRatio);
            return;
        }
        if (irrRest == null || irrRest < 30 || irrRest > 200) {
            fail("静息上限须为 30-200 bpm", editIrrRest);
            return;
        }
        if (irrSustain == null || irrSustain < 1) {
            fail("连续确认窗口数须 ≥1", editIrrSustain);
            return;
        }
        if (irrCd == null || irrCd < 0) {
            fail("冷却分钟须 ≥0", editIrrCooldown);
            return;
        }
        // 启用渠道必填参数(对齐 EXE save_settings)
        if (chkMeowEnabled.isChecked() && text(editMeowNick).trim().isEmpty()) {
            fail("MeoW已启用, 请填写昵称", editMeowNick);
            return;
        }
        if (chkBarkEnabled.isChecked() && text(editBarkKey).trim().isEmpty()) {
            fail("Bark已启用, 请填写推送Key", editBarkKey);
            return;
        }
        if (chkNtfyEnabled.isChecked() && text(editNtfyTopic).trim().isEmpty()) {
            fail("ntfy已启用, 请填写订阅主题", editNtfyTopic);
            return;
        }
        if (chkInfluxEnabled.isChecked() && (text(editInfluxUrl).trim().isEmpty()
                || text(editInfluxToken).trim().isEmpty() || text(editInfluxOrg).trim().isEmpty()
                || text(editInfluxBucket).trim().isEmpty())) {
            fail("InfluxDB已启用, 请填写地址/令牌/组织/存储桶", editInfluxUrl);
            return;
        }
        JSONArray periods = collectPeriods();
        if (periods == null) return;

        // ---- 写入(键位类型严格按 Prefs 契约) ----
        Prefs.putBool(this, Prefs.DEV_AUTO_CONNECT, chkAutoConnect.isChecked());
        Prefs.putBool(this, Prefs.SRV_ENABLED, chkSrvEnabled.isChecked());
        Prefs.putStr(this, Prefs.SRV_ADDRESS, addr);
        Prefs.putInt(this, Prefs.SRV_PORT, srvPort);
        Prefs.putInt(this, Prefs.PUSH_MAX_HR, maxHr);
        Prefs.putInt(this, Prefs.PUSH_MIN_HR, minHr);
        Prefs.putInt(this, Prefs.PUSH_ABNORMAL_DURATION, duration);
        Prefs.putInt(this, Prefs.PUSH_COOLDOWN_SECONDS, cooldown);
        Prefs.putBool(this, Prefs.LOCAL_ALARM_ENABLED, chkLocalAlarm.isChecked());
        Prefs.putBool(this, Prefs.IRR_ENABLED, chkIrrEnabled.isChecked());
        Prefs.putInt(this, Prefs.IRR_WINDOW_SECONDS, irrWin);
        Prefs.putInt(this, Prefs.IRR_SD_THRESHOLD, irrSd);
        Prefs.putInt(this, Prefs.IRR_JUMP_BPM, irrJump);
        Prefs.putInt(this, Prefs.IRR_JUMP_RATIO_PCT, irrRatio);
        Prefs.putInt(this, Prefs.IRR_REST_MAX_HR, irrRest);
        Prefs.putInt(this, Prefs.IRR_SUSTAIN_WINDOWS, irrSustain);
        Prefs.putInt(this, Prefs.IRR_COOLDOWN_MINUTES, irrCd);
        Prefs.putBool(this, Prefs.MEOW_ENABLED, chkMeowEnabled.isChecked());
        Prefs.putStr(this, Prefs.MEOW_NICKNAME, text(editMeowNick).trim());
        Prefs.putBool(this, Prefs.BARK_ENABLED, chkBarkEnabled.isChecked());
        Prefs.putStr(this, Prefs.BARK_DEVICE_KEY, text(editBarkKey).trim());
        Prefs.putStr(this, Prefs.BARK_SERVER, text(editBarkServer).trim());
        Prefs.putStr(this, Prefs.BARK_LEVEL, BARK_LEVEL_VALUES[spinBarkLevel.getSelectedItemPosition()]);
        Prefs.putStr(this, Prefs.BARK_SOUND, text(editBarkSound).trim());
        Prefs.putStr(this, Prefs.BARK_GROUP, text(editBarkGroup).trim());
        Prefs.putBool(this, Prefs.NTFY_ENABLED, chkNtfyEnabled.isChecked());
        Prefs.putStr(this, Prefs.NTFY_TOPIC, text(editNtfyTopic).trim());
        Prefs.putStr(this, Prefs.NTFY_SERVER, text(editNtfyServer).trim());
        Prefs.putInt(this, Prefs.NTFY_PRIORITY, spinNtfyPriority.getSelectedItemPosition());
        Prefs.putStr(this, Prefs.NTFY_TAGS, text(editNtfyTags).trim());
        Prefs.putStr(this, Prefs.NTFY_TOKEN, text(editNtfyToken).trim());
        Prefs.putBool(this, Prefs.INFLUX_ENABLED, chkInfluxEnabled.isChecked());
        Prefs.putStr(this, Prefs.INFLUX_URL, text(editInfluxUrl).trim());
        Prefs.putStr(this, Prefs.INFLUX_TOKEN, text(editInfluxToken).trim());
        Prefs.putStr(this, Prefs.INFLUX_ORG, text(editInfluxOrg).trim());
        Prefs.putStr(this, Prefs.INFLUX_BUCKET, text(editInfluxBucket).trim());
        Prefs.putStr(this, Prefs.MQTT_BROKER, text(editMqttBroker).trim());
        Prefs.putInt(this, Prefs.MQTT_PORT, parseIntOr(text(editMqttPort), 1883));
        Prefs.putStr(this, Prefs.MQTT_USERNAME, text(editMqttUser).trim());
        Prefs.putStr(this, Prefs.MQTT_PASSWORD, text(editMqttPass));
        Prefs.putStr(this, Prefs.MQTT_TOPIC, text(editMqttTopic).trim());
        Prefs.putBool(this, Prefs.MQTT_DISCOVERY_ENABLED, chkMqttDisc.isChecked());
        Prefs.putStr(this, Prefs.MQTT_DISCOVERY_TOPIC, text(editMqttDiscTopic).trim());
        Prefs.putStr(this, Prefs.PUSH_PERIODS, periods.toString());

        Toast.makeText(this, "设置已保存"
                + (HeartRateService.isRunning() ? ", 部分参数停止监测后重新开启生效" : ""),
                Toast.LENGTH_LONG).show();
    }

    // ==================== 设备管理 ====================

    private void onScanClicked() {
        if (ble.isScanning()) {
            ble.stopScan();
            return;
        }
        if (!BleManager.hasBlePermissions(this)) {
            pendingScan = true;
            BleManager.requestBlePermissions(this, REQ_BLE_PERMS);
            return;
        }
        startScan();
    }

    private void startScan() {
        devices.clear();
        selName = "";
        selAddr = "";
        txtScanStatus.setText("扫描中…");
        renderScanList();
        boolean ok = ble.startScan(this, new BleManager.ScanListener() {
            @Override
            public void onDeviceFound(String name, String address) {
                for (String[] d : devices) {
                    if (d[1].equals(address)) return;   // 同一设备去重
                }
                devices.add(new String[]{name, address});
                txtScanStatus.setText("扫描中, 已发现 " + devices.size() + " 台设备…");
                renderScanList();   // 实时逐行刷新, 扫描期间即可见(对齐EXE可用设备列表)
            }

            @Override
            public void onScanFinished(int count) {
                if (count == 0) {
                    txtScanStatus.setText("扫描完成, 未发现设备(请确认手环可被发现)");
                    renderScanList();
                    return;
                }
                txtScanStatus.setText("扫描完成, 点击列表中的设备进行选择");
                renderScanList();
            }

            @Override
            public void onScanError(String msg) {
                txtScanStatus.setText(msg);
            }
        });
        if (!ok) txtScanStatus.setText("扫描失败: 蓝牙未开启");
    }

    // ---- 列表渲染(LinearLayout动态行, 避开ScrollView嵌套ListView滑动冲突) ----

    private void renderScanList() {
        listScan.removeAllViews();
        if (devices.isEmpty()) {
            listScan.addView(hintRow("尚未发现设备, 点击\"扫描\"开始"));
            return;
        }
        for (int i = 0; i < devices.size(); i++) {
            final String[] d = devices.get(i);
            boolean fav = isFavorite(d[1]);
            boolean sel = d[1].equals(selAddr);
            TextView row = new TextView(this);
            row.setText((fav ? "★ " : "○ ") + d[0] + "  (" + d[1] + ")" + (sel ? "  ✓已选" : ""));
            row.setTextSize(13);
            row.setPadding(dp(8), dp(9), dp(8), dp(9));
            row.setTextColor(Color.parseColor(sel ? "#4CAF50" : "#FFFFFF"));
            row.setBackgroundColor(Color.parseColor(sel ? "#332E7D32" : "#1C1E26"));
            row.setOnClickListener(v -> selectDevice(d[0], d[1]));
            listScan.addView(row, rowMargins());
        }
    }

    private void renderFavList() {
        listFav.removeAllViews();
        txtFavTitle.setText("★ 收藏的设备 (" + favorites.size() + "):");
        if (favorites.isEmpty()) {
            listFav.addView(hintRow("无收藏; 在扫描列表点选设备后可收藏"));
            return;
        }
        for (int i = 0; i < favorites.size(); i++) {
            final int idx = i;
            final String[] f = favorites.get(i);
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(8), 0, dp(8), 0);
            row.setBackgroundColor(Color.parseColor("#1C1E26"));
            TextView tv = new TextView(this);
            tv.setText("★ " + f[0] + "  (" + f[1] + ")");
            tv.setTextSize(13);
            tv.setTextColor(Color.WHITE);
            tv.setPadding(0, dp(9), 0, dp(9));
            tv.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
            tv.setOnClickListener(v -> selectDevice(f[0], f[1]));
            row.addView(tv);
            Button rm = new Button(this);
            rm.setText("移除");
            rm.setTextSize(11);
            rm.setPadding(dp(10), 0, dp(10), 0);
            rm.setOnClickListener(v -> removeFavorite(idx));
            row.addView(rm);
            listFav.addView(row, rowMargins());
        }
    }

    private TextView hintRow(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(12);
        tv.setTextColor(Color.parseColor("#999999"));
        tv.setPadding(dp(8), dp(6), dp(8), dp(6));
        return tv;
    }

    private LinearLayout.LayoutParams rowMargins() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, dp(2), 0, dp(2));
        return lp;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ---- 选中/收藏 ----

    private void selectDevice(String name, String addr) {
        selName = name;
        selAddr = addr;
        txtDevSaved.setText("已选择: " + name + " (" + addr + ")");
        renderScanList();
    }

    private boolean isFavorite(String addr) {
        for (String[] f : favorites) {
            if (f[1].equalsIgnoreCase(addr)) return true;
        }
        return false;
    }

    /** 收藏列表JSON存取(对齐EXE favorite_devices: [{"name":"..","address":".."},..]) */
    private void loadFavorites() {
        favorites.clear();
        try {
            JSONArray arr = new JSONArray(Prefs.getStr(this, Prefs.FAV_DEVICES, "[]"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                favorites.add(new String[]{o.optString("name", ""), o.optString("address", "")});
            }
        } catch (Exception e) {
            // 坏JSON当空列表(对齐EXE容错)
        }
        renderFavList();
    }

    private void persistFavorites() {
        JSONArray arr = new JSONArray();
        try {
            for (String[] f : favorites) {
                JSONObject o = new JSONObject();
                o.put("name", f[0]);
                o.put("address", f[1]);
                arr.put(o);
            }
        } catch (Exception ignored) {
        }
        Prefs.putStr(this, Prefs.FAV_DEVICES, arr.toString());
    }

    private void addFavorite() {
        if (selAddr.isEmpty()) {
            Toast.makeText(this, "请先在扫描列表中点选一台设备", Toast.LENGTH_SHORT).show();
            return;
        }
        if (isFavorite(selAddr)) {
            Toast.makeText(this, "该设备已在收藏列表中", Toast.LENGTH_SHORT).show();
            return;
        }
        favorites.add(new String[]{selName, selAddr});
        persistFavorites();
        renderFavList();
        renderScanList();   // 刷新★标记
        // 收藏即使用: 同时设为当前设备并自动连接(无需回主页手动启停)
        Prefs.putStr(this, Prefs.DEV_NAME, selName);
        Prefs.putStr(this, Prefs.DEV_ADDRESS, selAddr);
        txtDevSaved.setText("当前设备: " + selName + " (" + selAddr + ")");
        if (HeartRateService.isRunning()) {
            HeartRateService.stop(this);
        }
        HeartRateService.start(this);
        Toast.makeText(this, "已收藏并连接 " + selName, Toast.LENGTH_SHORT).show();
    }

    private void removeFavorite(int idx) {
        if (idx < 0 || idx >= favorites.size()) return;
        String[] f = favorites.remove(idx);
        persistFavorites();
        renderFavList();
        renderScanList();
        Toast.makeText(this, "已移除收藏 " + f[0], Toast.LENGTH_SHORT).show();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_BLE_PERMS) return;
        boolean all = true;
        for (int r : grantResults) {
            if (r != android.content.pm.PackageManager.PERMISSION_GRANTED) all = false;
        }
        if (all && pendingScan) {
            pendingScan = false;
            startScan();
        } else if (!all) {
            Toast.makeText(this, "缺少定位权限, 无法扫描BLE设备", Toast.LENGTH_LONG).show();
        }
    }

    private void saveAndConnect() {
        if (selAddr.isEmpty()) {
            Toast.makeText(this, "请先扫描并点选一台设备", Toast.LENGTH_SHORT).show();
            return;
        }
        Prefs.putStr(this, Prefs.DEV_NAME, selName);
        Prefs.putStr(this, Prefs.DEV_ADDRESS, selAddr);
        Prefs.putBool(this, Prefs.DEV_AUTO_CONNECT, chkAutoConnect.isChecked());
        txtDevSaved.setText("当前设备: " + selName + " (" + selAddr + ")");
        // 配置即运行: 服务运行中重启以应用新设备, 未运行则直接启动(无需回主页手动开启)
        if (HeartRateService.isRunning()) {
            HeartRateService.stop(this);
        }
        HeartRateService.start(this);
        Toast.makeText(this, "已保存并连接 " + selName, Toast.LENGTH_SHORT).show();
    }

    // ==================== 测试 ====================

    /** 渠道测试: 先保存再调 PushChannels.test(回调已在主线程) */
    private void testChannel(String channel) {
        saveAll();   // 渠道模块从 Prefs 读参, 必须先落盘
        Button btn = channel.equals("meow") ? findViewById(R.id.btn_test_meow)
                : channel.equals("bark") ? findViewById(R.id.btn_test_bark)
                : findViewById(R.id.btn_test_ntfy);
        btn.setEnabled(false);
        PushChannels.test(this, channel, (ch, ok, msg) -> {
            btn.setEnabled(true);
            Toast.makeText(this, "[" + ch + (ok ? "] 测试成功: " : "] 测试失败: ") + msg,
                    Toast.LENGTH_LONG).show();
        });
    }

    /** InfluxDB 测试: 先保存再 new InfluxWriter 读最新参(回调已在主线程) */
    private void testInflux() {
        saveAll();
        Button btn = findViewById(R.id.btn_test_influx);
        btn.setEnabled(false);
        new InfluxWriter(this).test((ok, msg) -> {
            btn.setEnabled(true);
            Toast.makeText(this, (ok ? "InfluxDB 测试成功: " : "InfluxDB 测试失败: ") + msg,
                    Toast.LENGTH_LONG).show();
        });
    }

    // ==================== 检查更新 ====================

    /** 检查更新: GitHub releases latest → 弹窗确认 → 下载 → 跳系统安装器 */
    private void checkUpdate() {
        Toast.makeText(this, "正在检查更新...", Toast.LENGTH_SHORT).show();
        UpdateChecker.check((hasUpdate, ver, url, notes, err) -> {
            if (isFinishing()) return;
            if (err != null) {
                Toast.makeText(this, "检查失败: " + err, Toast.LENGTH_LONG).show();
                return;
            }
            if (!hasUpdate) {
                Toast.makeText(this, "已是最新版本 v" + BuildConfig.VERSION_NAME, Toast.LENGTH_SHORT).show();
                return;
            }
            String msg = "当前版本: v" + BuildConfig.VERSION_NAME
                    + "\n最新版本: v" + ver
                    + "\n\n更新说明:\n" + (notes == null || notes.isEmpty() ? "—" : notes);
            new android.app.AlertDialog.Builder(this)
                    .setTitle("发现新版本")
                    .setMessage(msg)
                    .setPositiveButton("下载并安装", (d, w) -> {
                        Toast.makeText(this, "开始下载 v" + ver + "...", Toast.LENGTH_SHORT).show();
                        UpdateChecker.download(getApplicationContext(), url, ver, (apk, derr) -> {
                            if (isFinishing()) return;
                            if (derr != null) {
                                Toast.makeText(this, "下载失败: " + derr, Toast.LENGTH_LONG).show();
                                return;
                            }
                            Toast.makeText(this, "下载完成, 请确认安装", Toast.LENGTH_SHORT).show();
                            UpdateChecker.install(getApplicationContext(), apk);
                        });
                    })
                    .setNegativeButton("稍后再说", null)
                    .show();
        });
    }

    // ==================== 工具 ====================

    private static String text(EditText e) {
        return e == null ? "" : e.getText().toString();
    }

    private static String text(View row, int id) {
        return ((EditText) row.findViewById(id)).getText().toString();
    }

    /** 解析整数; 空串/非法返回 null(交由调用方提示) */
    private static Integer intOf(EditText e) {
        return parseIntOr(text(e), null);
    }

    private static Integer intOf(View row, int id) {
        return parseIntOr(text(row, id), null);
    }

    private static Integer parseIntOr(String s, Integer def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
    }

    /** 宽松IPv4校验(auto 之外的绑定地址, 对齐 EXE validate_bind_address 的日常用法) */
    private static boolean isIp(String s) {
        String[] parts = s.split("\\.");
        if (parts.length != 4) return false;
        for (String p : parts) {
            try {
                int v = Integer.parseInt(p.trim());
                if (v < 0 || v > 255 || (p.trim().length() > 1 && p.trim().startsWith("0"))) return false;
            } catch (NumberFormatException e) {
                return false;
            }
        }
        return true;
    }

    private void fail(String msg, EditText field) {
        if (field != null) field.setError(msg);
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
    }
}
