package com.hrmlink.hrserver;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 首页=监测页(对标 EXE 心率监测主页):
 * - 顶部: 应用名+版本号, 右侧"设置"进 SettingsActivity
 * - 中部: 大号心率数字(断连"--")+心形+状态行(设备/连接/WS客户端/服务地址), 下方60秒波形
 * - 底部: 开始监测(先查BLE权限)/停止监测/悬浮窗开关(状态存 "overlay_enabled")
 * 数据源 HeartBus: onResume订阅+全量刷新, onPause退订防泄漏
 */
public class MainActivity extends Activity {

    private static final int REQ_BLE = 1; // BLE定位权限申请码

    private TextView txtVersion;
    private TextView txtHr;
    private TextView txtBpm;
    private TextView txtStatus;
    private ImageView imgHeart;
    private HeartWaveView wave;
    /** 进程冷启动后首次进入主页已自动启动监测(防从设置页回来反复拉起, 手动停止后不再自动重启) */
    private static boolean autoStartedOnce = false;

    /** 状态行每秒自刷: WS客户端数/服务地址变化不伴随心率回调, 只能轮询补齐 */
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable statusTick = new Runnable() {
        @Override
        public void run() {
            refreshStatusLine();
            mainHandler.postDelayed(this, 1000);
        }
    };

    /** HeartBus监听: 回调已在主线程, 直接刷UI */
    private final HeartBus.Listener busListener = new HeartBus.Listener() {
        @Override
        public void onHeartRate(int hr, String ts, String status) {
            applyHeartRate(hr, status);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        txtVersion = findViewById(R.id.txt_version);
        txtHr = findViewById(R.id.txt_hr);
        txtBpm = findViewById(R.id.txt_bpm);
        txtStatus = findViewById(R.id.txt_status);
        imgHeart = findViewById(R.id.img_heart);
        wave = findViewById(R.id.wave);

        txtVersion.setText("v" + BuildConfig.VERSION_NAME);

        findViewById(R.id.btn_settings).setOnClickListener(v ->
                startActivity(new Intent(this, SettingsActivity.class)));
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 全量刷新: 快照心率/波形/状态/按钮
        HeartBus bus = HeartBus.get();
        applyHeartRate(bus.getHeartRate(), bus.getStatus());
        wave.setWave(bus.getWave()); // 整帧同步60点快照(旧→新, 0=断点)
        refreshStatusLine();
        bus.addListener(busListener);
        mainHandler.post(statusTick);
        // 配置即运行: 进程冷启动后首次进入主页自动拉起监测服务(设置页保存/收藏也会触发连接)
        if (!autoStartedOnce) {
            autoStartedOnce = true;
            if (!HeartRateService.isRunning()) {
                if (BleManager.hasBlePermissions(this)) {
                    HeartRateService.start(this);
                } else {
                    // 首次使用: 发起权限申请, 授权结果回调里续接启动
                    BleManager.requestBlePermissions(this, REQ_BLE);
                }
            }
        }
        // 开关状态持久化, 开机后重进页面自动恢复浮窗
        if (Prefs.getBool(this, OverlayManager.KEY_OVERLAY_ENABLED, false)) {
            if (Settings.canDrawOverlays(this)) {
                OverlayManager.get(this).show();
            } else {
                Prefs.putBool(this, OverlayManager.KEY_OVERLAY_ENABLED, false); // 权限已撤回, 纠正状态
            }
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        HeartBus.get().removeListener(busListener);
        mainHandler.removeCallbacks(statusTick);
    }

    /** BLE权限申请结果: 授权后直接继续启动监测 */
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

    /** 心率回调统一入口: 数字/颜色/心形脉冲/波形断点 */
    private void applyHeartRate(int hr, String status) {
        boolean connected = "connected".equals(status) && hr > 0;
        txtHr.setText(hr > 0 ? String.valueOf(hr) : "--");
        // 心率数值/心形按区间变色(EXE波形区间同款): <60蓝 60-100绿 100-120橙 >120红
        int c = hr > 0 ? zoneColor(hr) : 0xFF9E9E9E;
        txtHr.setTextColor(c);
        txtBpm.setTextColor(c);
        imgHeart.setColorFilter(c);
        if (hr > 0) pulseHeart();
        wave.push(hr); // hr<=0 时自动画折线断点
        refreshStatusLine();
    }

    /** 状态行: 设备名/连接状态/WS客户端数/服务地址(auto:端口 或 IP:端口) */
    private void refreshStatusLine() {
        HeartBus bus = HeartBus.get();
        boolean running = HeartRateService.isRunning();
        String dev = bus.getDeviceName().isEmpty() ? "未配置设备" : bus.getDeviceName();
        String conn = "connected".equals(bus.getStatus())
                ? (bus.getHeartRate() > 0 ? "已连接" : "已连接(等待数据)")
                : "未连接";
        String addr = Prefs.getStr(this, Prefs.SRV_ADDRESS, "auto");
        int port = Prefs.getInt(this, Prefs.SRV_PORT, 8765);
        String hostText;
        if (addr.isEmpty() || "auto".equals(addr)) {
            String ip = HeartServer.detectTailscaleIp(); // 服务运行时与HeartRateService探测逻辑一致
            hostText = (ip != null ? ip : "auto") + ":" + port;
        } else {
            hostText = addr + ":" + port;
        }
        txtStatus.setText("设备: " + dev
                + " | 连接: " + conn
                + " | WS客户端: " + bus.getWsClients()
                + " | 服务地址: " + hostText
                + (running ? "" : " | 监测未启动"));
    }

    /** 心形随每次数据脉冲一下 */
    private void pulseHeart() {
        imgHeart.animate().scaleX(1.25f).scaleY(1.25f).setDuration(120)
                .withEndAction(() -> imgHeart.animate().scaleX(1f).scaleY(1f).setDuration(200).start())
                .start();
    }

    /** 心率区间颜色(EXE波形区间同款) */
    private int zoneColor(int hr) {
        if (hr < 60) return 0xFF3498DB;   // 静息 蓝
        if (hr <= 100) return 0xFF2ECC71; // 正常 绿
        if (hr <= 120) return 0xFFF39C12; // 偏高 橙
        return 0xFFE74C3C;                // 很高 红
    }
}
