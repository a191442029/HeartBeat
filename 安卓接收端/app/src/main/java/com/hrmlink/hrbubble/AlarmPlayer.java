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
 */
public final class AlarmPlayer {

    private static final String TAG = "HRBubbleAlarm";
    private static final long MAX_DURATION_MS = 10000;

    private static MediaPlayer player;
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final Runnable AUTO_STOP = AlarmPlayer::stopNow;

    private AlarmPlayer() {
    }

    /** 开始播放报警音(已在响则仅刷新自动停止计时) */
    public static synchronized void play(Context context) {
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

    /** 停止报警音(EXE复位alarm=false / 服务销毁时调用, 幂等) */
    public static synchronized void stop() {
        main.removeCallbacks(AUTO_STOP);
        stopNow();
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
