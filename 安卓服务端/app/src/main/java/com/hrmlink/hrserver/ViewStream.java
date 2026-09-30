package com.hrmlink.hrserver;

import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.util.Arrays;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * 远程看板 MSE fMP4 推流进程（P1-6, 严格按需: 一客户端一相机一路）
 *
 * ffmpeg -c copy 从子码流 RTSP 输出 fragmented MP4 到 stdout, 读线程按块回调
 * Sink（经 per-socket 有界队列转 WS 二进制帧）, 零转码（CPU≈解封装, RK3566 可行,
 * 见 安卓服务端开发计划.md §4.4）。
 *
 * fMP4 关键参数（MSE 流式播放三件套）:
 * - frag_keyframe: 每关键帧切一个 moof 片段（≈GOP 一片, 1-2s 延迟）
 * - empty_moov: 初始化段(ftyp+moov)在流头一次性输出（管道不可 seek 的必要条件）
 * - default_base_moof: 浏览器 MSE 兼容要求
 * 前提: 子码流 H.264（浏览器不解 H.265; 与报警剪辑同一前提, 已拍板接受）。
 *
 * 生命周期: /view WS 客户端发 live 订阅时由 CameraManager 创建; 断开/切换相机/
 * 服务停止 → stop() 销毁; ffmpeg 自行退出（相机断流）或发送队列过载 → onEnd 回调。
 *
 * 背压设计: 发送队列有界(128块≈1MB), 慢客户端堆满后丢弃任务并触发 onEnd
 * （fMP4 丢中间块=解码断流, 与其让画面花屏不如主动断流由前端重试）。
 */
final class ViewStream {

    /** fMP4 数据块回调（frame 为独立副本, 可延迟处理; 在 view-reader 线程执行） */
    interface Sink {
        void onFrame(byte[] frame);
    }

    private static final String TAG = "HRServer";

    private Process proc;
    private volatile boolean stop = false;
    private final ThreadPoolExecutor sendPool;   // per-socket 发送队列（有界, 防慢客户端堆积 OOM）

    /**
     * @param ffmpeg ffmpeg 可执行路径
     * @param rtspUrl 子码流 RTSP 地址
     * @param tag 日志标签（相机名）
     * @param sink 数据块回调（reader 线程）
     * @param onEnd 流结束回调（ffmpeg 退出或发送过载; 主动 stop 不回调。可能在 reader 线程）
     */
    ViewStream(String ffmpeg, String rtspUrl, String tag, Sink sink, final Runnable onEnd) {
        sendPool = new ThreadPoolExecutor(1, 1, 30, TimeUnit.SECONDS,
                new ArrayBlockingQueue<Runnable>(128), new ThreadFactory() {
            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "view-send");
                t.setDaemon(true);
                return t;
            }
        }, new ThreadPoolExecutor.DiscardPolicy());   // 满时丢弃, 由 offer 检测计数
        try {
            // 参数对齐 pullLoop 的输入侧; -nostdin 防抢 stdin, -timeout 5s 无数据自退触发 onEnd
            ProcessBuilder pb = new ProcessBuilder(
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", rtspUrl,
                    "-c", "copy", "-f", "mp4",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    "pipe:1");
            pb.redirectErrorStream(false);
            proc = pb.start();
        } catch (IOException e) {
            Log.w(TAG, "[看板:" + tag + "] ffmpeg 启动失败: " + e);
            proc = null;
        }
        if (proc != null) {
            drain(proc.getErrorStream(), tag);
            final InputStream in = proc.getInputStream();
            final byte[] buf = new byte[8192];
            final Thread reader = new Thread(new Runnable() {
                @Override
                public void run() {
                    boolean ended = false;
                    try {
                        int n;
                        while (!stop && (n = in.read(buf)) > 0) {
                            final byte[] chunk = Arrays.copyOf(buf, n);   // reader 复用 buf, 必须拷贝
                            if (!offer(new FrameTask(sink, chunk))) {
                                ended = true;   // 发送过载: 断流(前端收 onEnd 提示重试)
                                break;
                            }
                        }
                    } catch (IOException ignore) {
                        // 进程退出时 read 抛流关闭, 正常路径
                    } finally {
                        try { in.close(); } catch (IOException ignore) {}
                        if (ended || !stop) {
                            if (onEnd != null) {
                                try { onEnd.run(); } catch (Exception ignore) {}
                            }
                        }
                    }
                }
            }, "view-reader");
            reader.setDaemon(true);
            reader.start();
        } else if (onEnd != null) {
            // 启动失败也走 onEnd, 让外层统一提示客户端
            onEnd.run();
        }
    }

    /** 提交发送任务; 队列满返回 false（并置过载标记） */
    private boolean offer(FrameTask t) {
        try {
            sendPool.execute(t);
            return true;
        } catch (RejectedExecutionException e) {
            Log.w(TAG, "[看板] 发送队列过载, 断流保护");
            return false;
        }
    }

    private static final class FrameTask implements Runnable {
        final Sink sink;
        final byte[] frame;

        FrameTask(Sink sink, byte[] frame) {
            this.sink = sink;
            this.frame = frame;
        }

        @Override
        public void run() {
            sink.onFrame(frame);
        }
    }

    /** 停止推流: 停发送队列 + 销毁 ffmpeg（幂等） */
    void stop() {
        stop = true;
        sendPool.shutdownNow();
        Process p = proc;
        if (p != null) {
            p.destroy();
            try {
                if (!p.waitFor(2, TimeUnit.SECONDS)) {
                    p.destroyForcibly();
                }
            } catch (InterruptedException e) {
                p.destroyForcibly();
                Thread.currentThread().interrupt();
            }
        }
    }

    /** 排干 ffmpeg stderr 防管道写满阻塞（error 级日志量极小, 全量转 logcat） */
    private static void drain(final InputStream in, final String tag) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                byte[] buf = new byte[4096];
                try {
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        Log.w(TAG, "[看板:" + tag + "] ffmpeg: " + new String(buf, 0, n).trim());
                    }
                } catch (IOException ignore) {
                } finally {
                    try { in.close(); } catch (IOException ignore) {}
                }
            }
        }, "view-" + tag + "-err").start();
    }
}
