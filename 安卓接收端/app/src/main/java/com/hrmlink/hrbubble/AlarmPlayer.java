package com.hrmlink.hrbubble;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;

/**
 * 报警音播放(EXE远程报警): alarm=true时循环响铃
 * - 音源: res/raw/alarm.mp3(与EXE端music/报警.mp3同源)
 * - USAGE_ALARM走闹钟音量通道, 不受媒体音量/勿扰静音影响
 * - 10秒自动停(与EXE报警窗口一致); 收到alarm=false立即停; 幂等可重复调用
 * - cancel(): 用户手动取消本次报警 → 停铃并在本次报警周期内不再响,
 *   收到alarm=false(周期结束)自动复位, 下次报警照常响铃
 */
public final class AlarmPlayer {

    private static final String TAG = "HRBubbleAlarm";
    private static final long MAX_DURATION_MS = 10000;

    private static MediaPlayer player;
    private static volatile boolean canceled = false; // 本次报警已被用户取消(周期结束自动复位)
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final Runnable AUTO_STOP = AlarmPlayer::stopNow;

    private AlarmPlayer() {
    }

    /** 开始播放报警音(已在响则仅刷新自动停止计时; 本次报警已被取消则不响) */
    public static synchronized void play(Context context) {
        if (canceled) return; // 用户已取消本次报警, 窗口期内后续alarm=true不再触发
        if (player != null) {
            main.removeCallbacks(AUTO_STOP);
            main.postDelayed(AUTO_STOP, MAX_DURATION_MS);
            return;
        }
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        player = MediaPlayer.create(context.getApplicationContext(), R.raw.alarm, attrs, 0);
        if (player == null) {
            android.util.Log.w(TAG, "报警音资源加载失败");
            return;
        }
        player.setLooping(true);
        player.start();
        main.postDelayed(AUTO_STOP, MAX_DURATION_MS);
    }

    /** 停止报警音(EXE复位alarm=false / 服务销毁时调用, 幂等); 同时复位取消标志(下次报警照常响) */
    public static synchronized void stop() {
        main.removeCallbacks(AUTO_STOP);
        canceled = false;
        stopNow();
    }

    /** 用户手动取消本次报警: 立即停铃, 报警周期内(alarm=false到来前)不再响应play */
    public static synchronized void cancel() {
        canceled = true;
        main.removeCallbacks(AUTO_STOP);
        stopNow();
    }

    /** 本次报警是否已被用户取消(首页/悬浮窗据此隐藏取消按钮) */
    public static synchronized boolean isCanceled() {
        return canceled;
    }

    private static synchronized void stopNow() {
        MediaPlayer p = player;
        player = null;
        if (p != null) {
            try {
                p.stop();
            } catch (Exception ignored) {
            }
            try {
                p.release();
            } catch (Exception ignored) {
            }
        }
    }
}
