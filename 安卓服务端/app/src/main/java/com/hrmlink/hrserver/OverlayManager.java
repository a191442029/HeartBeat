package com.hrmlink.hrserver;

import android.content.Context;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.TextView;

/**
 * 悬浮窗管理(服务端版, 对标接收端同款交互):
 * - TYPE_APPLICATION_OVERLAY 心率气泡: 心形+大号数字, 断连显示 "--"
 * - 可拖动(位置持久化到config), 点击切换大/小字号, 第二行24小时制时钟
 * - 数据源: HeartBus.addListener(回调已在主线程, 直接更新TextView), show时订阅/hide时退订
 * - 开关状态持久化: Prefs键 "overlay_enabled"(boolean, 默认false), 由MainActivity写入
 */
public class OverlayManager {

    /** 悬浮窗开关键(与MainActivity/设置页共用, 不新增Prefs常量) */
    public static final String KEY_OVERLAY_ENABLED = "overlay_enabled";

    private static OverlayManager sInstance;

    /** 单例入口(应用级Context, 页面销毁后浮窗仍存活) */
    public static synchronized OverlayManager get(Context ctx) {
        if (sInstance == null) {
            sInstance = new OverlayManager(ctx.getApplicationContext());
        }
        return sInstance;
    }

    private final Context context;
    private final WindowManager wm;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private View bubble;
    private View dot;
    private ImageView imgHeart;
    private TextView hrText;
    private TextView srcText;
    private TextView timeText;
    private WindowManager.LayoutParams params;
    private boolean added = false;
    private boolean largeFont = false;

    /** HeartBus订阅: show()时挂上, hide()时摘除(与页面生命周期解耦) */
    private final HeartBus.Listener busListener = new HeartBus.Listener() {
        @Override
        public void onHeartRate(int hr, String ts, String status) {
            update(hr, status);
        }
    };

    // 悬浮窗时钟: 24小时制 HH:mm:ss, 对齐整秒刷新(仅显示期间跑)
    private final java.text.SimpleDateFormat clockFmt =
            new java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault());
    private Runnable clockTask;

    private OverlayManager(Context context) {
        this.context = context;
        this.wm = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
    }

    /** 显示悬浮窗(需已授予 SYSTEM_ALERT_WINDOW, 权限判断由调用方完成) */
    public synchronized void show() {
        if (added) return;
        if (bubble == null) build();
        try {
            wm.addView(bubble, params);
            added = true;
            HeartBus.get().addListener(busListener);
            // 先按当前快照刷一次, 避免等待下一次数据推送
            HeartBus bus = HeartBus.get();
            update(bus.getHeartRate(), bus.getStatus());
            startClock();
        } catch (Exception e) {
            // 权限被撤回等场景, 静默失败
        }
    }

    /** 隐藏悬浮窗(不改开关状态, 状态由MainActivity写Prefs) */
    public synchronized void hide() {
        HeartBus.get().removeListener(busListener);
        stopClock();
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

    /** 更新心率显示: hr<=0 → "--" + 灰点; 正常 → 数字 + 绿点(回调已在主线程) */
    private void update(final int hr, final String status) {
        boolean connected = "connected".equals(status);
        final int color = context.getColor(connected ? R.color.dot_green : R.color.dot_timeout);
        final String text = hr > 0 ? String.valueOf(hr) : "--";
        final String src = connected
                ? (HeartBus.get().getDeviceName().isEmpty() ? "已连接" : HeartBus.get().getDeviceName())
                : "未连接";
        mainHandler.post(() -> {
            if (bubble == null) return;
            hrText.setText(text + " BPM");
            srcText.setText(src);
            imgHeart.setColorFilter(color);
            setDotColor(color);
            // 状态点随数据闪烁: 全亮→衰减到30%; 断连时常亮灰
            if (connected) {
                dot.animate().cancel();
                dot.setAlpha(1f);
                dot.animate().alpha(0.3f).setDuration(550).setStartDelay(120).start();
            } else {
                dot.animate().cancel();
                dot.setAlpha(1f);
            }
        });
    }

    private void setDotColor(int color) {
        GradientDrawable d = (GradientDrawable) dot.getBackground().mutate();
        d.setColor(color);
        dot.setBackground(d);
    }

    private void build() {
        bubble = LayoutInflater.from(context).inflate(R.layout.overlay_bubble, null);
        dot = bubble.findViewById(R.id.dot);
        imgHeart = bubble.findViewById(R.id.img_heart_bubble);
        hrText = bubble.findViewById(R.id.hr_text);
        srcText = bubble.findViewById(R.id.src_text);
        timeText = bubble.findViewById(R.id.time_text);

        android.content.SharedPreferences sp = Prefs.sp(context);
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
                            Prefs.sp(context).edit()
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

    /** 时钟: 显示期间每秒刷新, 对齐到下一个整秒(跳动更自然) */
    private void startClock() {
        if (clockTask != null) return;
        clockTask = new Runnable() {
            @Override
            public void run() {
                if (!added) return;
                timeText.setText(clockFmt.format(new java.util.Date()));
                mainHandler.postDelayed(this, 1000 - (System.currentTimeMillis() % 1000));
            }
        };
        mainHandler.post(clockTask);
    }

    private void stopClock() {
        if (clockTask != null) {
            mainHandler.removeCallbacks(clockTask);
            clockTask = null;
        }
    }
}
