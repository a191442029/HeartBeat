package com.hrmlink.hrbubble;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 设置页: 服务器地址/端口/仅轮询 + 悬浮窗授权 + 电池优化白名单
 * 首页右上角⚙进入; 修改在下次连接时生效
 */
public class SettingsActivity extends Activity {

    private static final String PREFS = "hrbubble";
    // 字体大小可调范围: 心率数字60~140sp, 时间12~40sp(SeekBar无min, 用偏移映射)
    private static final int HR_FONT_MIN = 60, HR_FONT_MAX = 140;
    private static final int TIME_FONT_MIN = 12, TIME_FONT_MAX = 40;

    private EditText editServer, editPort;
    private CheckBox chkPollOnly;
    private android.widget.SeekBar seekHr, seekTime;
    private TextView valHr, valTime;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        editServer = findViewById(R.id.edit_server);
        editPort = findViewById(R.id.edit_port);
        chkPollOnly = findViewById(R.id.chk_poll_only);
        seekHr = findViewById(R.id.seek_hr);
        seekTime = findViewById(R.id.seek_time);
        valHr = findViewById(R.id.val_hr);
        valTime = findViewById(R.id.val_time);

        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        editServer.setText(sp.getString("server", ""));
        editPort.setText(sp.getString("port", "8765"));
        chkPollOnly.setChecked(sp.getBoolean("poll_only", false));

        // 字体大小滑条: 加载当前值并实时刷新数值显示
        int hrSp = clamp(sp.getInt("hr_font_sp", 88), HR_FONT_MIN, HR_FONT_MAX);
        int timeSp = clamp(sp.getInt("time_font_sp", 20), TIME_FONT_MIN, TIME_FONT_MAX);
        seekHr.setMax(HR_FONT_MAX - HR_FONT_MIN);
        seekHr.setProgress(hrSp - HR_FONT_MIN);
        seekTime.setMax(TIME_FONT_MAX - TIME_FONT_MIN);
        seekTime.setProgress(timeSp - TIME_FONT_MIN);
        valHr.setText(hrSp + " sp");
        valTime.setText(timeSp + " sp");
        seekHr.setOnSeekBarChangeListener(new android.widget.SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(android.widget.SeekBar s, int p, boolean b) {
                valHr.setText((p + HR_FONT_MIN) + " sp");
            }
            @Override public void onStartTrackingTouch(android.widget.SeekBar s) { }
            @Override public void onStopTrackingTouch(android.widget.SeekBar s) { }
        });
        seekTime.setOnSeekBarChangeListener(new android.widget.SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(android.widget.SeekBar s, int p, boolean b) {
                valTime.setText((p + TIME_FONT_MIN) + " sp");
            }
            @Override public void onStartTrackingTouch(android.widget.SeekBar s) { }
            @Override public void onStopTrackingTouch(android.widget.SeekBar s) { }
        });

        findViewById(R.id.btn_overlay).setOnClickListener(v -> ensureOverlayPermission());
        findViewById(R.id.btn_battery).setOnClickListener(v -> requestIgnoreBattery());
        findViewById(R.id.btn_save).setOnClickListener(v -> save());
        findViewById(R.id.btn_check_update).setOnClickListener(v -> checkUpdate());

        // 版本号
        ((TextView) findViewById(R.id.txt_version)).setText("HRBubble " + BuildConfig.VERSION_NAME);
    }

    private static int clamp(int v, int lo, int hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

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

    private void save() {
        String host = editServer.getText().toString().trim();
        if (host.isEmpty()) {
            editServer.setError("请填写电脑Tailscale IP");
            return;
        }
        String port = editPort.getText().toString().trim();
        if (port.isEmpty()) port = "8765";
        try {
            int p = Integer.parseInt(port);
            if (p < 1 || p > 65535) throw new NumberFormatException();
        } catch (NumberFormatException e) {
            editPort.setError("端口须为1-65535的数字");
            return;
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString("server", host)
                .putString("port", port)
                .putBoolean("poll_only", chkPollOnly.isChecked())
                .putInt("hr_font_sp", seekHr.getProgress() + HR_FONT_MIN)
                .putInt("time_font_sp", seekTime.getProgress() + TIME_FONT_MIN)
                .apply();
        Toast.makeText(this, HeartRateService.sRunning
                ? "已保存, 停止监测后重新开启生效" : "已保存", Toast.LENGTH_SHORT).show();
        finish();
    }

    /** 悬浮窗特殊权限: 跳转系统授权页 */
    private boolean ensureOverlayPermission() {
        if (!Settings.canDrawOverlays(this)) {
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
                Toast.makeText(this, "请找到本应用并允许\"显示在其他应用上层\"", Toast.LENGTH_LONG).show();
            } catch (Exception e) {
                Toast.makeText(this, "无法打开悬浮窗授权页: " + e.getMessage(), Toast.LENGTH_LONG).show();
            }
            return false;
        }
        return true;
    }

    /** 电池优化白名单: 防止 Doze 冻结后台服务/Tailscale */
    private void requestIgnoreBattery() {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm.isIgnoringBatteryOptimizations(getPackageName())) {
            Toast.makeText(this, "已在电池优化白名单中", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            // 部分ROM不支持直接申请, 退回列表页手动设置
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
                Toast.makeText(this, "无法打开电池优化设置", Toast.LENGTH_SHORT).show();
            }
        }
    }
}
