package com.hrmlink.hrbubble;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;

/**
 * 悬浮窗管理: TYPE_APPLICATION_OVERLAY 心率气泡
 * - 可拖动(位置持久化到SharedPreferences), 点击切换大/小字号
 * - 状态点: 绿=WS实时 黄=HTTP轮询 灰=数据超时 红=设备断连
 * - update/timeout 内部post到主线程, 服务侧任意线程可安全调用
 */
public class OverlayManager {

    private static final String PREFS = "hrbubble";

    private final Context context;
    private final WindowManager wm;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private View bubble;
    private View dot;
    private TextView hrText;
    private TextView srcText;
    private TextView timeText;
    private WindowManager.LayoutParams params;
    private boolean added = false;
    private boolean largeFont = false;

    public OverlayManager(Context context) {
        this.context = context;
        this.wm = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
    }

    /** 显示悬浮窗(需已授予 SYSTEM_ALERT_WINDOW) */
    public synchronized void show() {
        if (added) return;
        if (bubble == null) build();
        try {
            wm.addView(bubble, params);
            added = true;
        } catch (Exception e) {
            // 权限被撤回等场景, 静默失败由服务状态栏提示
        }
    }

    public synchronized void remove() {
        if (added && bubble != null) {
            try {
                wm.removeView(bubble);
            } catch (Exception ignored) {
            }
        }
        added = false;
    }

    public boolean isShowing() {
        return added;
    }

    /** 更新心率显示(时间来自EXE数据包的timestamp, 非设备本地时钟) */
    public void update(int hr, boolean connected, boolean fromWs, String srcLabel, String timeStr) {
        final int color = connected
                ? (fromWs ? context.getColor(R.color.dot_ws) : context.getColor(R.color.dot_poll))
                : context.getColor(R.color.dot_timeout); // 设备断连/无数据 = 灰色
        final String text = hr > 0 ? String.valueOf(hr) : "--";
        mainHandler.post(() -> {
            if (bubble == null) return;
            hrText.setText(text + " BPM");
            srcText.setText(srcLabel);
            timeText.setText(shortTime(timeStr));
            setDotColor(color);
            // 状态点随数据闪烁: 全亮→衰减到30%; 无数据时由 timeout() 恢复常亮灰
            dot.animate().cancel();
            dot.setAlpha(1f);
            dot.animate().alpha(0.3f).setDuration(550).setStartDelay(120).start();
        });
    }

    /** 数据超时(>10秒无更新): 清除旧数字显示"--", 状态点灰色常亮不闪烁 (防断连后残影心率) */
    public void timeout() {
        mainHandler.post(() -> {
            if (bubble == null) return;
            hrText.setText("-- BPM");
            srcText.setText("超时");
            timeText.setText("--:--:--");
            setDotColor(context.getColor(R.color.dot_timeout));
            dot.animate().cancel();
            dot.setAlpha(1f);
        });
    }

    /** EXE时间戳 "yyyy-MM-dd HH:mm:ss" → 仅取 "HH:mm:ss" 部分(无空格则原样, 空则占位) */
    private static String shortTime(String ts) {
        if (ts == null || ts.isEmpty()) return "--:--:--";
        int sp = ts.indexOf(' ');
        return (sp >= 0 && sp < ts.length() - 1) ? ts.substring(sp + 1) : ts;
    }

    private void setDotColor(int color) {
        GradientDrawable d = (GradientDrawable) dot.getBackground().mutate();
        d.setColor(color);
        dot.setBackground(d);
    }

    private void build() {
        bubble = LayoutInflater.from(context).inflate(R.layout.overlay_bubble, null);
        dot = bubble.findViewById(R.id.dot);
        hrText = bubble.findViewById(R.id.hr_text);
        srcText = bubble.findViewById(R.id.src_text);
        timeText = bubble.findViewById(R.id.time_text);

        SharedPreferences sp = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,   // Android 8.0+ 悬浮窗类型
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE           // 不抢焦点, 下层App可正常输入
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL, // 气泡外触摸穿透到下层
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.START;
        params.x = sp.getInt("bubble_x", 40);
        params.y = sp.getInt("bubble_y", 120);

        // 拖动 + 点击(位移小于阈值判定为点击, 切换字号)
        bubble.setOnTouchListener(new View.OnTouchListener() {
            float downRawX, downRawY;
            float startX, startY;
            boolean moved = false;

            @Override
            public boolean onTouch(View v, MotionEvent ev) {
                switch (ev.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downRawX = ev.getRawX();
                        downRawY = ev.getRawY();
                        startX = params.x;
                        startY = params.y;
                        moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        float dx = ev.getRawX() - downRawX;
                        float dy = ev.getRawY() - downRawY;
                        if (Math.abs(dx) > 8 || Math.abs(dy) > 8) moved = true;
                        if (moved) {
                            params.x = (int) (startX + dx);
                            params.y = (int) (startY + dy);
                            try {
                                wm.updateViewLayout(bubble, params);
                            } catch (Exception ignored) {
                            }
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (moved) {
                            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                                    .edit()
                                    .putInt("bubble_x", params.x)
                                    .putInt("bubble_y", params.y)
                                    .apply();
                        } else {
                            toggleFont();
                        }
                        return true;
                }
                return false;
            }
        });
    }

    /** 点击气泡: 大/小字号切换 */
    private void toggleFont() {
        largeFont = !largeFont;
        hrText.setTextSize(largeFont ? 34f : 22f);
    }
}
