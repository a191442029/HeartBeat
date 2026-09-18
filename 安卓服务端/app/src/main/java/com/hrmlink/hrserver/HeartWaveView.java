package com.hrmlink.hrserver;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.DashPathEffect;
import android.graphics.Paint;
import android.graphics.Path;
import android.util.AttributeSet;
import android.util.TypedValue;
import android.view.View;

import java.util.ArrayList;

/**
 * 实时心率波形(严格对齐EXE版 HeartRateWaveform):
 * - 窗口60点@1Hz=60秒(max_points=60同款), 最新点贴右缘, hr<=0为断点
 * - 左侧Y轴心率刻度: 动态量程(余量max(span*25%,15), 夹在40~200), 4等分网格
 * - 心率区间背景带(EXE同款颜色/alpha0.1): <60静息蓝 60-100正常绿 100-120偏高橙 >120很高红
 * - 底部X轴(EXE同款相对时间): 每10秒一刻度, 标签 "-60秒,-50秒,...,-10秒,现在"
 * 零第三方库, 每秒1帧, 低端机友好
 */
public class HeartWaveView extends View {

    private static final int MAX_POINTS = 60;  // 60秒窗口(EXE max_points=60同款)
    private static final int Y_TICKS = 4;      // Y轴4等分

    // 心率区间颜色(EXE同款, alpha 0.1≈0x1A)
    private static final int ZONE_REST = 0x1A3498DB;    // 静息 <60 蓝
    private static final int ZONE_NORMAL = 0x1A2ECC71;  // 正常 60-100 绿
    private static final int ZONE_HIGH = 0x1AF39C12;    // 偏高 100-120 橙
    private static final int ZONE_VHIGH = 0x1AE74C3C;   // 很高 >120 红

    private final ArrayList<Integer> data = new ArrayList<>();

    private final Paint linePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint gridPaint = new Paint();
    private final Paint bandPaint = new Paint();
    private final Paint dotPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint labelPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint dashPaint = new Paint();
    private final Path path = new Path();
    private final float gutterL, gutterB; // 左/下刻度区(px)

    public HeartWaveView(Context context) {
        this(context, null);
    }

    public HeartWaveView(Context context, AttributeSet attrs) {
        super(context, attrs);
        linePaint.setColor(0xFFFF6B6B); // EXE折线同款颜色
        linePaint.setStyle(Paint.Style.STROKE);
        linePaint.setStrokeWidth(4f);
        linePaint.setStrokeJoin(Paint.Join.ROUND);
        linePaint.setStrokeCap(Paint.Cap.ROUND);
        gridPaint.setColor(0x30FFFFFF);
        gridPaint.setStrokeWidth(1f);
        // 时间轴竖向虚线(EXE网格方向同款)
        dashPaint.setColor(0x30FFFFFF);
        dashPaint.setStrokeWidth(1f);
        dashPaint.setPathEffect(new DashPathEffect(new float[]{6, 6}, 0));
        bandPaint.setStyle(Paint.Style.FILL);
        dotPaint.setColor(0xFFFF6B6B);
        labelPaint.setColor(0xB3FFFFFF);
        labelPaint.setTextSize(TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_SP, 11, getResources().getDisplayMetrics()));
        gutterL = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 42,
                getResources().getDisplayMetrics());
        gutterB = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 20,
                getResources().getDisplayMetrics());
    }

    /** 追加一个心率样本(0=无效/断连, 画折线断点) */
    public void push(int hr) {
        synchronized (data) {
            data.add(hr);
            if (data.size() > MAX_POINTS) data.remove(0);
        }
        invalidate();
    }

    /** 批量重灌波形(旧→新, 0=断点): onResume 用 HeartBus.getWave() 快照整体同步, 避免逐点push多次重绘 */
    public void setWave(int[] pts) {
        synchronized (data) {
            data.clear();
            if (pts != null) {
                for (int v : pts) data.add(v);
                while (data.size() > MAX_POINTS) data.remove(0);
            }
        }
        invalidate();
    }

    /** 清空(停止监测时) */
    public void clear() {
        synchronized (data) {
            data.clear();
        }
        invalidate();
    }

    @Override
    protected void onDraw(Canvas cv) {
        super.onDraw(cv);
        int w = getWidth(), h = getHeight();
        if (w < gutterL + 30 || h < gutterB + 30) return;

        float x0 = gutterL;          // 绘图区左缘
        float plotW = w - x0;        // 绘图区宽
        float plotB = h - gutterB;   // 绘图区底缘(留出时间轴)
        float plotH = plotB;

        Integer[] arr;
        synchronized (data) {
            arr = data.toArray(new Integer[0]);
        }
        int n = arr.length;

        // === Y量程(EXE同款逻辑) ===
        int mn = Integer.MAX_VALUE, mx = Integer.MIN_VALUE;
        for (Integer v : arr) {
            if (v != null && v > 0) {
                if (v < mn) mn = v;
                if (v > mx) mx = v;
            }
        }
        float yMin, yMax;
        if (mn > mx) { // 无数据默认(EXE: set_ylim(40,200))
            yMin = 40f;
            yMax = 200f;
        } else {
            int span = mx - mn;
            float margin = Math.max(span * 0.25f, 15f);
            yMin = Math.max(40f, mn - margin);
            yMax = Math.min(200f, mx + margin);
            if (yMax - yMin < 10f) { // 防呆: 保证非零跨度
                yMin = Math.max(40f, yMin - 5f);
                yMax = Math.min(200f, yMax + 5f);
            }
        }
        float range = yMax - yMin;

        // === 心率区间背景带(EXE axhspan同款: 与可见量程取交集) ===
        drawZoneBand(cv, x0, w, yMin, yMax, plotH, yMin, Math.min(60f, yMax), ZONE_REST);
        drawZoneBand(cv, x0, w, yMin, yMax, plotH, Math.max(60f, yMin), Math.min(100f, yMax), ZONE_NORMAL);
        drawZoneBand(cv, x0, w, yMin, yMax, plotH, Math.max(100f, yMin), Math.min(120f, yMax), ZONE_HIGH);
        drawZoneBand(cv, x0, w, yMin, yMax, plotH, Math.max(120f, yMin), yMax, ZONE_VHIGH);

        // === 横向网格 + 左侧心率刻度(顶部标签下移防裁切) ===
        float tsAscent = labelPaint.getTextSize();
        for (int i = 0; i <= Y_TICKS; i++) {
            float y = plotH * i / Y_TICKS;
            cv.drawLine(x0, y, w, y, gridPaint);
            int val = Math.round(yMax - range * i / Y_TICKS);
            float by = Math.max(tsAscent, y + tsAscent / 3f);
            cv.drawText(String.valueOf(val), 4f, by, labelPaint);
        }

        // === 底部时间轴(EXE同款相对时间): 0~60每10秒一刻度 → "-60秒...-10秒,现在" ===
        cv.drawLine(x0, plotB, w, plotB, gridPaint);
        for (int t = 0; t <= MAX_POINTS; t += 10) {
            float px = x0 + plotW * t / MAX_POINTS;
            if (t < MAX_POINTS) cv.drawLine(px, 0f, px, plotB, dashPaint); // 竖向虚线(右缘除外)
            String s = t >= MAX_POINTS ? "现在" : "-" + (MAX_POINTS - t) + "秒";
            float tw = labelPaint.measureText(s);
            float tx = Math.max(x0 + 2f, Math.min(w - tw - 2f, px - tw / 2f));
            cv.drawText(s, tx, h - 4f, labelPaint);
        }

        if (n < 2) return;
        float step = plotW / (MAX_POINTS - 1);

        // === 心率折线(最新点贴右缘) ===
        path.reset();
        boolean pen = false;
        float lastX = 0, lastY = 0;
        for (int i = 0; i < n; i++) {
            int hr = arr[i] == null ? 0 : arr[i];
            float px = x0 + plotW - (n - 1 - i) * step;
            if (hr <= 0) { // 断点: 抬笔
                pen = false;
                continue;
            }
            float py = plotH - (hr - yMin) / range * plotH;
            py = Math.max(4f, Math.min(plotH - 4f, py));
            if (!pen) {
                path.moveTo(px, py);
                pen = true;
            } else {
                path.lineTo(px, py);
            }
            lastX = px;
            lastY = py;
        }
        cv.drawPath(path, linePaint);
        if (pen) cv.drawCircle(lastX, lastY, 6f, dotPaint);
    }

    /** 画一条心率区间背景带: [loHr,hiHr]∩[yMin,yMax] 可见部分 */
    private void drawZoneBand(Canvas cv, float x0, float x1,
                              float yMin, float yMax, float plotH,
                              float loHr, float hiHr, int color) {
        float lo = Math.max(loHr, yMin);
        float hi = Math.min(hiHr, yMax);
        if (hi <= lo) return;
        float range = yMax - yMin;
        float yTop = plotH - (hi - yMin) / range * plotH;
        float yBot = plotH - (lo - yMin) / range * plotH;
        bandPaint.setColor(color);
        cv.drawRect(x0, yTop, x1, yBot, bandPaint);
    }
}
