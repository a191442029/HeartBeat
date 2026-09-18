package com.hrmlink.hrbubble;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 首页=监测页: 大号心率+实时波形+状态条, 横竖屏双布局
 * - 配置(IP/端口/仅轮询)与权限引导在设置页(标题栏"设置"进入)
 * - 悬浮窗为独立开关: 不开浮窗也可只看首页; 开浮窗未监测时自动建连
 * - 系统标题栏即工具栏: 标题=HRBubble+本地日期时间, 右侧菜单项=设置/悬浮窗/监测
 */
public class MainActivity extends Activity {

    private static final String PREFS = "hrbubble";

    private TextView txtStatus, txtHr, txtBpm;
    private TextView txtReconnect; // 波形上方附加状态行(EXE智能重连进度), 无内容时隐藏
    private View dotHome;
    private HeartWaveView wave;
    private ImageView imgHeart;
    private BroadcastReceiver receiver;
    private TextView txtTime;
    // 标题栏右侧动作菜单(设置/悬浮窗/监测), 悬浮窗与监测文案随服务状态切换
    private Menu actionBarMenu;
    // 标题栏本地时钟: 对齐整秒刷新, 仅页面可见期间运行
    private final android.os.Handler clockHandler = new android.os.Handler();
    private Runnable clockTask;
    private final java.text.SimpleDateFormat clockFmt =
            new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.getDefault());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        txtStatus = findViewById(R.id.txt_status);
        txtHr = findViewById(R.id.txt_hr);
        dotHome = findViewById(R.id.dot_home);
        wave = findViewById(R.id.wave);
        imgHeart = findViewById(R.id.img_heart);
        txtTime = findViewById(R.id.txt_time);
        txtBpm = findViewById(R.id.txt_bpm);
        txtReconnect = findViewById(R.id.txt_reconnect);
    }

    private boolean serverConfigured() {
        return !getSharedPreferences(PREFS, MODE_PRIVATE).getString("server", "").isEmpty();
    }

    /** 开始/停止监测(前台服务生命周期) */
    private void toggleMonitor() {
        if (HeartRateService.sRunning) {
            stopService(new Intent(this, HeartRateService.class));
            txtHr.setText("--");
            txtTime.setText("--:--:--");
            wave.clear();
            setStatusDot(false, false, "");
        } else {
            if (!serverConfigured()) {
                Toast.makeText(this, "请先点⚙设置填写电脑Tailscale IP", Toast.LENGTH_LONG).show();
                startActivity(new Intent(this, SettingsActivity.class));
                return;
            }
            startService(new Intent(this, HeartRateService.class));
        }
        refreshButtons();
    }

    /** 悬浮窗独立开关: 服务未运行时顺带建连 */
    private void toggleFloat() {
        if (!HeartRateService.sRunning) {
            if (!serverConfigured()) {
                Toast.makeText(this, "请先点⚙设置填写电脑Tailscale IP", Toast.LENGTH_LONG).show();
                startActivity(new Intent(this, SettingsActivity.class));
                return;
            }
            Intent i = new Intent(this, HeartRateService.class);
            i.setAction(HeartRateService.ACTION_OVERLAY);
            i.putExtra("on", true);
            startService(i);
        } else {
            Intent i = new Intent(this, HeartRateService.class);
            i.setAction(HeartRateService.ACTION_OVERLAY);
            i.putExtra("on", !HeartRateService.sOverlayOn);
            startService(i);
        }
        refreshButtons();
    }

    @Override
    protected void onStart() {
        super.onStart();
        receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent i) {
                String action = i.getAction();
                if (HeartRateService.ACTION_DATA.equals(action)) {
                    int hr = i.getIntExtra("hr", 0);
                    boolean connected = i.getBooleanExtra("connected", false);
                    String src = i.getStringExtra("src");
                    String info = i.getStringExtra("info"); // 附加状态(EXE智能重连进度)
                    txtHr.setText(hr > 0 ? String.valueOf(hr) : "--");
                    // 时间显示EXE数据包里的timestamp(PC侧生成), 空则占位
                    txtTime.setText(shortTime(i.getStringExtra("timestamp")));
                    wave.push(hr);
                    // 心率数值/心形按区间变色(EXE波形区间同款): <60蓝 60-100绿 100-120橙 >120红
                    int c = hr > 0 ? zoneColor(hr) : getColor(R.color.dot_timeout);
                    txtHr.setTextColor(c);
                    txtBpm.setTextColor(c);
                    imgHeart.setColorFilter(c);
                    if (hr > 0) pulseHeart();
                    setStatusDot(true, connected, src);
                    // 波形上方附加状态行: 有内容显示, 无内容隐藏
                    if (info != null && !info.isEmpty()) {
                        txtReconnect.setText(info);
                        txtReconnect.setVisibility(View.VISIBLE);
                    } else {
                        txtReconnect.setVisibility(View.GONE);
                    }
                } else if (HeartRateService.ACTION_STATUS.equals(action)) {
                    String text = i.getStringExtra("text");
                    if (text != null) txtStatus.setText(text);
                }
                refreshButtons();
            }
        };
        IntentFilter f = new IntentFilter();
        f.addAction(HeartRateService.ACTION_STATUS);
        f.addAction(HeartRateService.ACTION_DATA);
        registerReceiver(receiver, f);
        startClock();
    }

    @Override
    protected void onStop() {
        super.onStop();
        stopClock();
        if (receiver != null) {
            unregisterReceiver(receiver);
            receiver = null;
        }
    }

    /** 标题栏本地日期时间: 对齐整秒刷新, 仅页面可见期间运行; 显示在"HRBubble"之后 */
    private void startClock() {
        if (clockTask != null) return;
        clockTask = new Runnable() {
            @Override
            public void run() {
                android.app.ActionBar bar = getActionBar();
                if (bar != null) {
                    bar.setTitle(getString(R.string.app_name) + "  "
                            + clockFmt.format(new java.util.Date()));
                }
                clockHandler.postDelayed(this, 1000 - (System.currentTimeMillis() % 1000));
            }
        };
        clockHandler.post(clockTask);
    }

    private void stopClock() {
        if (clockTask != null) {
            clockHandler.removeCallbacks(clockTask);
            clockTask = null;
        }
    }

    /** EXE时间戳 "yyyy-MM-dd HH:mm:ss" → 仅取 "HH:mm:ss" 部分(无空格则原样, 空则占位) */
    private static String shortTime(String ts) {
        if (ts == null || ts.isEmpty()) return "--:--:--";
        int sp = ts.indexOf(' ');
        return (sp >= 0 && sp < ts.length() - 1) ? ts.substring(sp + 1) : ts;
    }

    /** 系统标题栏动作菜单: 设置/悬浮窗/监测(替代原自定义工具栏按钮) */
    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_main, menu);
        actionBarMenu = menu;
        refreshButtons();
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_settings) {
            startActivity(new Intent(this, SettingsActivity.class));
            return true;
        } else if (id == R.id.action_float) {
            toggleFloat();
            return true;
        } else if (id == R.id.action_monitor) {
            toggleMonitor();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyFontPrefs();
        refreshButtons();
    }

    /** 应用设置页的字体大小(心率数字/时间), 每次回首页刷新 */
    private void applyFontPrefs() {
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        txtHr.setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, sp.getInt("hr_font_sp", 88));
        txtTime.setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, sp.getInt("time_font_sp", 20));
    }

    /** 标题栏菜单项文案跟随服务真实状态(static标志, 旋转重建后也能正确恢复) */
    private void refreshButtons() {
        if (actionBarMenu == null) return;
        actionBarMenu.findItem(R.id.action_monitor).setTitle(HeartRateService.sRunning
                ? R.string.btn_monitor_stop : R.string.btn_monitor_start);
        actionBarMenu.findItem(R.id.action_float).setTitle(HeartRateService.sOverlayOn
                ? R.string.btn_float_on : R.string.btn_float_off);
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

    /** 首页状态点: 绿=WS 黄=轮询 灰=无数据/断连 */
    private void setStatusDot(boolean hasData, boolean connected, String src) {
        int color;
        if (!hasData || !connected) {
            color = getColor(R.color.dot_timeout);
        } else {
            color = "WS".equals(src) ? getColor(R.color.dot_ws) : getColor(R.color.dot_poll);
        }
        GradientDrawable d = (GradientDrawable) dotHome.getBackground().mutate();
        d.setColor(color);
        dotHome.setBackground(d);
    }
}
