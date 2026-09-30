package com.hrmlink.hrserver;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.VideoView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashSet;
import java.util.Locale;

/**
 * 数字大屏首页(ui_preview.html 定稿, 1280×800 横屏主形态):
 * - 左列: 状态角标×2(数据源三态/中继RSSI·服务地址) + 大数字行(字号可调, 超宽自动切纵向) + 小块视频口
 * - 右侧: 60秒波形(EXE同款坐标轴); 报警时原位替换为报警相机快照大画面(不弹窗)
 *   + 红横幅 + 取消本次报警 + 相机tab + ▶回看报警视频(剪辑到手即显, 对齐接收端 replayDismissed)
 * - 底部: 7 张状态卡片(显隐走 Prefs UI_CARD_*)
 * 数据流: 心率/报警全在 HeartRateService+HeartBus, 本页仅订阅(Activity 重建零影响);
 * 快照帧由 CameraManager ffmpeg 抓帧线程产出文件, 本页 500ms 按 mtime 变化取图。
 */
public class MainActivity extends Activity {

    private static final int REQ_BLE = 1;

    /** 进程冷启动后首次进入主页已自动启动监测(防反复拉起, 手动停止后不再自动重启) */
    private static boolean autoStartedOnce = false;
    /** 电池优化白名单每进程仅引导一次(已在白名单则永不弹) */
    private static boolean batteryAskedOnce = false;

    // 顶栏
    private TextView txtVersion;
    // 左列
    private TextView txtSrc, txtRelay, txtHr, txtBpm;
    private ImageView imgHeart;
    private LinearLayout hrRow;
    // 小块视频口
    private View miniCam;
    private ImageView imgMini;
    private TextView miniTag, miniMeta;
    // 报警层
    private View alarmLayer;
    private ImageView imgAlarm;
    private TextView txtAlarmMsg, txtClipGen;
    private LinearLayout alarmTabs;
    private Button btnReplay;
    // 波形 + 卡片
    private HeartWaveView wave;
    private final View[] cards = new View[7];
    private final TextView[] dots = new TextView[7];
    private final TextView[] vals = new TextView[7];

    /** 报警层当前显示相机(tab 切换); 空=alarmCam/默认 */
    private String alarmTabCam = "";
    /** ✕退出回看后收起入口按钮, 防状态广播每秒弹回(对齐接收端 replayDismissed) */
    private boolean replayDismissed = false;
    /** 快照文件 mtime 缓存(变了才重 decode) */
    private long miniMts = 0, alarmMts = 0;
    /** 数字行是否已切纵向(数字上/心形下) */
    private boolean hrRowVertical = false;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    /** 1s 自刷: 角标/卡片(WS客户端数、MQTT/Influx/节点在线数变化不伴随心率回调) */
    private final Runnable statusTick = new Runnable() {
        @Override
        public void run() {
            refreshCorners();
            refreshCards();
            mainHandler.postDelayed(this, 1000);
        }
    };

    /** 500ms 取帧: 报警→报警相机大图; 非报警→小块视频口(房间→绑定相机→默认回退) */
    private final Runnable camTick = new Runnable() {
        @Override
        public void run() {
            refreshCamFrame();
            mainHandler.postDelayed(this, 500);
        }
    };

    private final HeartBus.Listener busListener = new HeartBus.Listener() {
        @Override
        public void onHeartRate(int hr, String ts, String status) {
            applyHeartRate(hr, status);
        }
    };

    /** 任意 _state 字段变化: 报警边沿/剪辑到手/相机tab数据更新 */
    private final HeartBus.StateListener stateListener = new HeartBus.StateListener() {
        @Override
        public void onStateChanged() {
            applyAlarmState();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        txtVersion = findViewById(R.id.txt_version);
        txtSrc = findViewById(R.id.txt_src);
        txtRelay = findViewById(R.id.txt_relay);
        hrRow = findViewById(R.id.hr_row);
        txtHr = findViewById(R.id.txt_hr);
        txtBpm = findViewById(R.id.txt_bpm);
        imgHeart = findViewById(R.id.img_heart);
        miniCam = findViewById(R.id.mini_cam);
        imgMini = findViewById(R.id.img_mini);
        miniTag = findViewById(R.id.mini_tag);
        miniMeta = findViewById(R.id.mini_meta);
        wave = findViewById(R.id.wave);
        alarmLayer = findViewById(R.id.alarm_layer);
        imgAlarm = findViewById(R.id.img_alarm);
        txtAlarmMsg = findViewById(R.id.txt_alarm_msg);
        txtClipGen = findViewById(R.id.txt_clip_gen);
        alarmTabs = findViewById(R.id.alarm_tabs);
        btnReplay = findViewById(R.id.btn_replay);
        int[] cardIds = {R.id.card1, R.id.card2, R.id.card3, R.id.card4, R.id.card5, R.id.card6, R.id.card7};
        int[] dotIds = {R.id.d1, R.id.d2, R.id.d3, R.id.d4, R.id.d5, R.id.d6, R.id.d7};
        int[] valIds = {R.id.v1, R.id.v2, R.id.v3, R.id.v4, R.id.v5, R.id.v6, R.id.v7};
        for (int i = 0; i < 7; i++) {
            cards[i] = findViewById(cardIds[i]);
            dots[i] = findViewById(dotIds[i]);
            vals[i] = findViewById(valIds[i]);
        }

        txtVersion.setText("v" + BuildConfig.VERSION_NAME);

        findViewById(R.id.btn_settings).setOnClickListener(v ->
                startActivity(new Intent(this, SettingsActivity.class)));
        findViewById(R.id.btn_records).setOnClickListener(v ->
                startActivity(new Intent(this, RecordsActivity.class)));
        // 小块视频口 → 摄像头墙(视线动线最短, 不弹窗)
        miniCam.setOnClickListener(v ->
                startActivity(new Intent(this, CameraWallActivity.class)));
        // 底部摄像头卡片 → 摄像头墙(同入口)
        cards[5].setOnClickListener(v ->
                startActivity(new Intent(this, CameraWallActivity.class)));
        // 取消本次报警: 立即复位+停接收端响铃(安卓补齐闭环)
        findViewById(R.id.btn_cancel_alarm).setOnClickListener(v -> {
            HeartBus.get().cancelAlarm();
            Toast.makeText(this, "已取消本次报警", Toast.LENGTH_SHORT).show();
        });
        btnReplay.setOnClickListener(v -> showReplay());
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyDisplayPrefs();
        // 常亮(数字大屏常显场景; Prefs screen_keep_on 默认开)
        if (Prefs.getBool(this, "screen_keep_on", true)) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }

        HeartBus bus = HeartBus.get();
        applyHeartRate(bus.getHeartRate(), bus.getStatus());
        wave.setWave(bus.getWave());
        refreshCorners();
        refreshCards();
        applyAlarmState();
        bus.addListener(busListener);
        bus.addStateListener(stateListener);
        mainHandler.post(statusTick);
        mainHandler.post(camTick);

        // 配置即运行: 进程冷启动后首次进主页自动拉起监测
        if (!autoStartedOnce) {
            autoStartedOnce = true;
            if (!HeartRateService.isRunning()) {
                if (BleManager.hasBlePermissions(this)) {
                    HeartRateService.start(this);
                } else {
                    BleManager.requestBlePermissions(this, REQ_BLE);
                }
            }
        }
        ensureBatteryWhitelist();
    }

    /** 电池优化白名单引导(长期通电常驻防杀; 部分ROM无此动作则静默跳过) */
    private void ensureBatteryWhitelist() {
        if (batteryAskedOnce) return;
        batteryAskedOnce = true;
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception ignore) {
            }
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        HeartBus.get().removeListener(busListener);
        HeartBus.get().removeStateListener(stateListener);
        mainHandler.removeCallbacks(statusTick);
        mainHandler.removeCallbacks(camTick);
        // 页面不可见: 停小块常驻快照(报警快照由服务 AlarmHook 管理, 不受影响)
        CameraManager.get().stopLiveSnapshots();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_BLE) {
            if (BleManager.hasBlePermissions(this)) {
                HeartRateService.start(this);
            } else {
                Toast.makeText(this, "未授予定位权限, 无法连接手环", Toast.LENGTH_LONG).show();
            }
        }
    }

    // ---------------- 心率数字 ----------------

    private void applyHeartRate(int hr, String status) {
        boolean connected = "connected".equals(status) && hr > 0;
        txtHr.setText(hr > 0 ? String.valueOf(hr) : "--");
        int c = hr > 0 ? zoneColor(hr) : 0xFF9E9E9E;
        txtHr.setTextColor(c);
        txtBpm.setTextColor(c);
        imgHeart.setColorFilter(c);
        if (hr > 0) pulseHeart();
        wave.push(hr); // hr<=0 自动画折线断点
    }

    private void pulseHeart() {
        imgHeart.animate().scaleX(1.25f).scaleY(1.25f).setDuration(120)
                .withEndAction(() -> imgHeart.animate().scaleX(1f).scaleY(1f).setDuration(200).start())
                .start();
    }

    private int zoneColor(int hr) {
        if (hr < 60) return 0xFF3498DB;
        if (hr <= 100) return 0xFF2ECC71;
        if (hr <= 120) return 0xFFF39C12;
        return 0xFFE74C3C;
    }

    // ---------------- 报警态(原位替换波形区) ----------------

    private void applyAlarmState() {
        HeartBus bus = HeartBus.get();
        boolean alarm = bus.isAlarm();
        alarmLayer.setVisibility(alarm ? View.VISIBLE : View.GONE);
        miniCam.setVisibility(alarm || !Prefs.getBool(this, Prefs.UI_MINICAM, true)
                ? View.GONE : View.VISIBLE);

        if (!alarm) {
            // 报警结束: 报警层收起; 剪辑在手则保留回看资格由下一轮报警复位
            if (!bus.getClipUrl().isEmpty()) {
                btnReplay.setVisibility(replayDismissed ? View.GONE : View.VISIBLE);
            } else {
                btnReplay.setVisibility(View.GONE);
            }
            return;
        }

        String room = bus.getAlarmRoom();
        txtAlarmMsg.setText("● 报警中" + (room.isEmpty() ? "" : " · " + room)
                + " · " + (bus.alarmElapsedMs() / 1000) + "s");
        rebuildAlarmTabs();
        // 报警默认相机 = alarmCam(空→默认回退); 有 tab 切换时保持用户所选
        if (alarmTabCam.isEmpty()) {
            alarmTabCam = bus.getAlarmCam();
        }
        alarmMts = 0;   // 强制刷新大图
        txtClipGen.setVisibility(bus.getClipUrl().isEmpty() ? View.VISIBLE : View.GONE);
        btnReplay.setVisibility(bus.getClipUrl().isEmpty() || replayDismissed
                ? View.GONE : View.VISIBLE);
    }

    /** 相机 tab: alarmCam + clips 全量相机名去重; >1 路才显示(对齐接收端) */
    private void rebuildAlarmTabs() {
        alarmTabs.removeAllViews();
        LinkedHashSet<String> names = new LinkedHashSet<>();
        String ac = HeartBus.get().getAlarmCam();
        if (!ac.isEmpty()) names.add(ac);
        try {
            JSONArray arr = new JSONArray(HeartBus.get().getClips());
            for (int i = 0; i < arr.length(); i++) {
                String cam = arr.getJSONObject(i).optString("cam", "");
                if (!cam.isEmpty()) names.add(cam);
            }
        } catch (Exception ignore) {
        }
        if (names.size() <= 1) return;
        for (final String name : names) {
            Button b = new Button(this);
            b.setText(name);
            b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
            b.setMinWidth(0);
            b.setMinHeight(0);
            b.setPadding(16, 6, 16, 6);
            b.setTextColor(alarmTabCam.equals(name) ? 0xFFFFFFFF : 0xFFBBBBCC);
            b.setOnClickListener(v -> {
                alarmTabCam = name;
                alarmMts = 0;
                rebuildAlarmTabs();
            });
            alarmTabs.addView(b);
        }
    }

    /** 回看报警视频: 原位弹出播放器(本机剪辑URL); 剪辑未到手提示稍候(对齐 EXE 报警 T+0 语义) */
    private void showReplay() {
        String url = HeartBus.get().getClipUrl();
        if (url.isEmpty()) {
            Toast.makeText(this, "剪辑生成中, 稍候几秒再试", Toast.LENGTH_SHORT).show();
            return;
        }
        replayDismissed = true;   // 进回看: 入口按钮隐藏, 下一轮报警恢复资格
        btnReplay.setVisibility(View.GONE);
        final VideoView vv = new VideoView(this);
        vv.setVideoURI(Uri.parse(url));
        final AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("回看报警视频")
                .setView(vv)
                .setPositiveButton("✕ 退出", null)
                .create();
        dlg.setOnDismissListener(d -> vv.stopPlayback());
        dlg.show();
        vv.start();
    }

    // ---------------- 快照取帧(500ms pump, mtime 变化才 decode) ----------------

    private void refreshCamFrame() {
        HeartBus bus = HeartBus.get();
        CameraManager cam = CameraManager.get();
        if (bus.isAlarm()) {
            File f = cam.snapshotFile(alarmTabCam);
            if (f != null && f.isFile() && f.lastModified() != alarmMts) {
                alarmMts = f.lastModified();
                Bitmap bp = BitmapFactory.decodeFile(f.getAbsolutePath());
                if (bp != null) imgAlarm.setImageBitmap(bp);
            }
            return;
        }
        if (miniCam.getVisibility() != View.VISIBLE) return;

        // 非报警: 小块视频口 = 手环当前房间 → room_camera_map → 默认相机
        String camName = currentRoomCam();
        cam.startLiveSnapshots(camName);   // 幂等(同相机跳过); 无ffmpeg/未配置内部自禁用
        File f = cam.snapshotFile(camName);
        long now = System.currentTimeMillis();
        if (f != null && f.isFile() && f.lastModified() != miniMts) {
            miniMts = f.lastModified();
            Bitmap bp = BitmapFactory.decodeFile(f.getAbsolutePath());
            if (bp != null) imgMini.setImageBitmap(bp);
        }
        String show = camName.isEmpty() ? cam.defaultName() : camName;
        String room = currentRoom();
        miniTag.setText("● " + (room.isEmpty() ? "手环未连接 · " : room + " · ") + show);
        miniMeta.setText("2fps · " + new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date(now)));
    }

    /** 手环当前数据源房间名(hr_source.source; 直连/失联=空串) */
    private String currentRoom() {
        try {
            return new JSONObject(HeartBus.get().getHrSource()).optString("source", "");
        } catch (Exception e) {
            return "";
        }
    }

    /** 房间→绑定相机(ROOM_CAMERA_MAP JSON{节点名:相机名}); 未绑定回退默认(空串=默认) */
    private String currentRoomCam() {
        String room = currentRoom();
        if (room.isEmpty()) return "";
        try {
            String cam = new JSONObject(Prefs.getStr(this, Prefs.ROOM_CAMERA_MAP, "{}"))
                    .optString(room, "");
            return cam;
        } catch (Exception e) {
            return "";
        }
    }

    // ---------------- 角标 + 卡片(1s) ----------------

    private void refreshCorners() {
        HeartBus bus = HeartBus.get();
        // 数据源角标: 直连=本机 · 已连接/未连接; 中继=source + phase 三态
        String src;
        String room = currentRoom();
        String phase = "";
        int rssi = 0;
        try {
            JSONObject hs = new JSONObject(bus.getHrSource());
            phase = hs.optString("phase", "");
            rssi = hs.optInt("rssi", 0);
        } catch (Exception ignore) {
        }
        if (room.isEmpty()) {
            boolean connected = "connected".equals(bus.getStatus());
            src = "数据源: 本机 · " + (connected ? (bus.getHeartRate() > 0 ? "已连接" : "已连接(等待数据)") : "未连接");
        } else {
            String ph = "active".equals(phase) ? "已连接"
                    : ("connecting".equals(phase) ? "切换中" : "失联");
            src = "数据源: " + room + " · " + ph;
        }
        txtSrc.setText(src);
        // 服务地址角标
        String addr = Prefs.getStr(this, Prefs.SRV_ADDRESS, "auto");
        int port = Prefs.getInt(this, Prefs.SRV_PORT, 8765);
        String host;
        if (addr.isEmpty() || "auto".equals(addr)) {
            String ip = HeartServer.detectTailscaleIp();
            host = (ip != null ? ip : "0.0.0.0") + ":" + port;
        } else {
            host = addr + ":" + port;
        }
        String relayTxt = rssi != 0 ? "中继 RSSI " + rssi + " · " : "";
        txtRelay.setText(relayTxt + "服务 " + host
                + (HeartRateService.isRunning() ? "" : " · 监测未启动"));
    }

    private void refreshCards() {
        HeartBus bus = HeartBus.get();
        boolean connected = "connected".equals(bus.getStatus());
        // 1 手环
        setCard(0, Prefs.getBool(this, Prefs.UI_CARD_BAND, true),
                connected ? 0xFF2ECC71 : 0xFF777777,
                connected ? "已连接" : "未连接",
                bus.getDeviceName().isEmpty() ? "未配置设备" : bus.getDeviceName());
        // 2 WS 客户端
        int ws = bus.getWsClients();
        setCard(1, Prefs.getBool(this, Prefs.UI_CARD_WS, true),
                ws > 0 ? 0xFF2ECC71 : 0xFF777777,
                ws + " 在线", "下游接收端");
        // 3 Tailscale
        String tsIp = HeartServer.detectTailscaleIp();
        setCard(2, Prefs.getBool(this, Prefs.UI_CARD_TS, true),
                tsIp != null ? 0xFF2ECC71 : 0xFF777777,
                tsIp != null ? "在线" : "离线",
                tsIp != null ? tsIp : "未运行");
        // 4 MQTT
        MqttPublisher mqtt = HeartRateService.getMqtt();
        boolean mqttOk = mqtt != null && mqtt.isConnected();
        setCard(3, Prefs.getBool(this, Prefs.UI_CARD_MQTT, true),
                mqttOk ? 0xFF2ECC71 : 0xFF777777,
                mqttOk ? "已连接" : "未连接",
                mqtt != null ? mqtt.getStatusText() : "未启用");
        // 5 InfluxDB
        InfluxWriter influx = HeartRateService.getInflux();
        boolean influxOk = influx != null && influx.isWriteOk();
        setCard(4, Prefs.getBool(this, Prefs.UI_CARD_INFLUX, true),
                influxOk ? 0xFF2ECC71 : 0xFF777777,
                influxOk ? "写入正常" : "未写入",
                influx != null && !influx.getWriteError().isEmpty() ? influx.getWriteError() : "—");
        // 6 摄像头
        int[] sc = CameraManager.get().streamCounts();
        setCard(5, Prefs.getBool(this, Prefs.UI_CARD_CAM, true),
                sc[0] > 0 ? 0xFF2ECC71 : 0xFF777777,
                sc[0] + "/" + sc[1] + " 拉流中", "点击打开监控墙");
        // 7 中继节点
        int[] nc = RelayHub.get().nodeCounts();
        setCard(6, Prefs.getBool(this, Prefs.UI_CARD_RELAY, true),
                nc[0] > 0 ? 0xFF2ECC71 : 0xFF777777,
                nc[0] + "/" + nc[1] + " 在线",
                RelayHub.get().isRunning() ? "ESP32 中继" : "中继未启用");
    }

    private void setCard(int i, boolean show, int dotColor, String val, String sub) {
        cards[i].setVisibility(show ? View.VISIBLE : View.GONE);
        if (!show) return;
        dots[i].setTextColor(dotColor);
        vals[i].setText(val + (sub.isEmpty() ? "" : " · " + sub));
    }

    // ---------------- 大屏显示 prefs(即改即生效) ----------------

    private void applyDisplayPrefs() {
        int size = Prefs.getInt(this, Prefs.UI_HR_SIZE, 150);
        txtHr.setTextSize(TypedValue.COMPLEX_UNIT_SP, size);
        imgHeart.setVisibility(Prefs.getBool(this, Prefs.UI_HEART_ICON, true)
                ? View.VISIBLE : View.GONE);
        boolean alarm = HeartBus.get().isAlarm();
        miniCam.setVisibility(alarm || !Prefs.getBool(this, Prefs.UI_MINICAM, true)
                ? View.GONE : View.VISIBLE);
        txtSrc.setVisibility(Prefs.getBool(this, Prefs.UI_SRC_ROW, true)
                ? View.VISIBLE : View.GONE);
        txtRelay.setVisibility(txtSrc.getVisibility());
        refreshCards();
        hrRow.post(this::checkOverflow);
    }

    /** 字号超宽自动切纵向(数字上/心形下): 真实宽度检测, 改回小字号恢复横排 */
    private void checkOverflow() {
        int avail = hrRow.getWidth();
        if (avail <= 0) {
            hrRow.postDelayed(this::checkOverflow, 100);
            return;
        }
        int need = txtHr.getWidth() + imgHeart.getWidth() + txtBpm.getWidth()
                + dp(10) + dp(8);
        boolean overflow = need > avail;
        if (overflow != hrRowVertical) {
            hrRowVertical = overflow;
            buildHrRow();
        }
    }

    /** 数字行横/纵重排: 纵排=数字上→BPM→心形下(对齐预览稿) */
    private void buildHrRow() {
        hrRow.removeAllViews();
        if (!hrRowVertical) {
            hrRow.setOrientation(LinearLayout.HORIZONTAL);
            hrRow.setGravity(Gravity.CENTER_HORIZONTAL | Gravity.CENTER_VERTICAL);
            hrRow.addView(imgHeart);
            ((LinearLayout.LayoutParams) imgHeart.getLayoutParams()).leftMargin = 0;
            hrRow.addView(txtHr);
            ((LinearLayout.LayoutParams) txtHr.getLayoutParams()).leftMargin = dp(10);
            hrRow.addView(txtBpm);
            ((LinearLayout.LayoutParams) txtBpm.getLayoutParams()).leftMargin = dp(8);
        } else {
            hrRow.setOrientation(LinearLayout.VERTICAL);
            hrRow.setGravity(Gravity.CENTER_HORIZONTAL);
            hrRow.addView(txtHr);
            ((LinearLayout.LayoutParams) txtHr.getLayoutParams()).leftMargin = 0;
            hrRow.addView(txtBpm);
            ((LinearLayout.LayoutParams) txtBpm.getLayoutParams()).leftMargin = 0;
            hrRow.addView(imgHeart);
            ((LinearLayout.LayoutParams) imgHeart.getLayoutParams()).leftMargin = 0;
        }
        hrRow.postDelayed(this::checkOverflow, 120);
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP,
                v, getResources().getDisplayMetrics()));
    }
}
