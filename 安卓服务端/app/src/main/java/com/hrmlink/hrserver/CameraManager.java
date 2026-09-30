package com.hrmlink.hrserver;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * 摄像头子系统（对标 EXE camera/stream_manager.py + hls_stream.py, copy-not-transcode）
 *
 * 与 EXE 的差异（RK3566 无 PC 级 CPU, 方案见 安卓服务端开发计划.md §4.4）:
 * - 环形缓冲: EXE 用内存 TS 管道; 本端用磁盘 HLS 分片（ffmpeg -c copy 直写, 零转码零读管道）
 * - 实时流: EXE 报警时另起一路转码 ffmpeg; 本端常驻拉流输出物即 HLS（分片已在磁盘,
 *   /camera/live/* 路由直接映射, 报警时零额外开销）
 * - 剪辑: EXE 管道喂 stdin 转码 H.264（老盒子兼容）; 本端 concat 最近分片 -c copy
 *   出 mp4（秒级完成, 前提=子码流 H.264, H.265-only 摄像头不支持——已拍板接受）
 *
 * ffmpeg 依赖: 外置 arm64 静态二进制, 首选 filesDir/ffmpeg（adb push 后 chmod 755）,
 * 备选 assets/ffmpeg 自动解包; 都没有则整条链路禁用并记日志（不崩溃）。
 * 要求 ffmpeg ≥ 6.1（RTSP 输入超时用 -timeout, 旧版参数名不同, 见 EXE rtsp_input_opts）。
 *
 * 线程模型:
 * - 每路相机 1 个拉流线程（断线自动重连, 退避 2s→60s, 对齐 EXE _buffer_loop）
 * - 报警快照: 报警期间 1 个刷新线程对绑定相机低频抓帧（一次性 ffmpeg 起停, ~1s/帧,
 *   仅为 HLS 未就绪空窗期的兜底, 对齐 EXE 报警期快照流"按需启停"语义）
 * - 剪辑: 同步方法, 调用方（HeartRateService 报警钩子）自行放工作线程
 */
public final class CameraManager implements HeartServer.CamProvider {

    private static final String TAG = "HRServer";

    private static final CameraManager sInstance = new CameraManager();

    public static CameraManager get() {
        return sInstance;
    }

    private CameraManager() {
    }

    /** 单路相机配置（解析自 Prefs.CAMERAS_JSON） */
    public static final class CamCfg {
        String name = "";
        /** 拉流地址: 直填RTSP, 或 ONVIF 模式(ip非空)下拉流线程解析后回填(pull/clip/snapshot 共用, 故 volatile) */
        volatile String url = "";
        boolean alarmEnabled = true;
        boolean isDefault = false;
        // ONVIF 可选字段: url 留空且 ip 非空时经 ONVIF 解析子码流地址(对齐 EXE resolve_rtsp_uri)
        String ip = "";
        int onvifPort = 80;
        String username = "";
        String password = "";
        volatile long uriResolvedAt = 0;   // 上次 ONVIF 解析时刻(1小时重解析, 对齐 EXE 缓存语义)
        File liveDir;      // HLS 分片目录
        File snapFile;     // 最新快照 JPEG
        File snapTmpFile;  // 抓帧临时文件（原子改名用）
    }

    private Context app;
    private volatile String ffmpegPath = null;
    private final List<CamCfg> cams = new ArrayList<>();
    private volatile boolean running = false;
    private final List<Thread> pullThreads = new ArrayList<>();
    private volatile Thread snapThread = null;     // 报警快照刷新线程
    private volatile boolean snapStop = false;

    // ---------- 生命周期 ----------

    /** 读配置并启动全部拉流（可 stop 后重新 start; 重复调用幂等） */
    public synchronized void start(Context c) {
        if (running) return;
        app = c.getApplicationContext();
        ffmpegPath = findFfmpeg(app);
        if (ffmpegPath == null) {
            Log.w(TAG, "未找到 ffmpeg(filesDir/ffmpeg 或 assets/ffmpeg), 摄像头链路禁用");
            return;
        }
        cams.clear();
        cams.addAll(parseCams(app));
        if (cams.isEmpty()) {
            Log.i(TAG, "未配置摄像头, 摄像头链路禁用");
            return;
        }
        running = true;
        for (final CamCfg cam : cams) {
            Thread t = new Thread(new Runnable() {
                @Override
                public void run() {
                    pullLoop(cam);
                }
            }, "campull-" + cam.name);
            t.start();
            pullThreads.add(t);
        }
        Log.i(TAG, "摄像头链路已启动: " + cams.size() + "路");
    }

    public synchronized void stop() {
        running = false;
        stopAlarmSnapshots();
        for (Thread t : pullThreads) {
            t.interrupt();
        }
        pullThreads.clear();
        cams.clear();
        Log.i(TAG, "摄像头链路已停止");
    }

    /** 解析相机配置 JSON（单条非法跳过, 对齐 EXE rebuild 行为） */
    private List<CamCfg> parseCams(Context c) {
        List<CamCfg> out = new ArrayList<>();
        if (!Prefs.getBool(c, Prefs.CAMERA_ENABLED, false)) {
            return out;
        }
        try {
            JSONArray arr = new JSONArray(Prefs.getStr(c, Prefs.CAMERAS_JSON, "[]"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                String name = o.optString("name", "").trim();
                String url = o.optString("url", "").trim();
                String ip = o.optString("ip", "").trim();
                // 直填 url 或 ONVIF(ip) 二选一; 两者都没有才跳过
                if (name.isEmpty() || (url.isEmpty() && ip.isEmpty())) continue;
                CamCfg cfg = new CamCfg();
                cfg.name = name;
                cfg.url = url;
                cfg.ip = ip;
                cfg.onvifPort = o.optInt("onvif_port", 80);
                cfg.username = o.optString("username", "").trim();
                cfg.password = o.optString("password", "").trim();
                cfg.alarmEnabled = o.optBoolean("alarm_enabled", true);
                cfg.isDefault = o.optBoolean("is_default", false);
                File dir = new File(app.getFilesDir(), "live");
                cfg.liveDir = new File(dir, safeName(name));
                cfg.liveDir.mkdirs();
                cfg.snapFile = new File(cfg.liveDir, "snap.jpg");
                cfg.snapTmpFile = new File(cfg.liveDir, "snap_tmp.jpg");
                out.add(cfg);
            }
        } catch (Exception e) {
            Log.e(TAG, "解析摄像头配置失败: " + e);
        }
        return out;
    }

    /** 文件名清洗（对齐 EXE _SAFE_FILE: 防路径逃逸/非法字符） */
    private static String safeName(String name) {
        String s = name.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
        if (s.length() > 80) s = s.substring(0, 80);
        return s.isEmpty() ? "cam" : s;
    }

    /** 定位 ffmpeg: filesDir/ffmpeg → assets/ffmpeg 解包; 无则 null */
    private static String findFfmpeg(Context c) {
        File f = new File(c.getFilesDir(), "ffmpeg");
        if (f.isFile() && f.canExecute()) {
            return f.getAbsolutePath();
        }
        try {
            InputStream in = c.getAssets().open("ffmpeg");
            FileOutputStream out = new FileOutputStream(f);
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            out.close();
            in.close();
            f.setExecutable(true, false);
            if (f.isFile() && f.canExecute()) {
                Log.i(TAG, "已从 assets 解包 ffmpeg");
                return f.getAbsolutePath();
            }
        } catch (IOException ignore) {
            // assets 无 ffmpeg, 属正常未安装场景
        }
        return null;
    }

    // ---------- 常驻拉流（HLS 分片环形缓冲） ----------

    /**
     * 确保拉流地址就绪(仅拉流线程调用, 同一 cam 单线程访问):
     * 直填 url 直接可用; ONVIF 模式(ip非空)取子码流地址回填 cam.url, 1小时重解析(对齐 EXE 缓存语义,
     * 部分设备 URI 内嵌会过期的 token)。解析失败记日志返回 false, 由外层按统一退避重试。
     */
    private boolean ensureUri(CamCfg cam) {
        if (cam.ip.isEmpty()) return !cam.url.isEmpty();
        if (!cam.url.isEmpty() && System.currentTimeMillis() - cam.uriResolvedAt < 3600_000L) {
            return true;
        }
        try {
            OnvifClient c = new OnvifClient(cam.ip, cam.onvifPort, cam.username, cam.password);
            OnvifClient.Profile p = c.pickProfile(true);   // 子码流(分辨率最低)
            String uri = c.getStreamUri(p.token);
            cam.url = uri;
            cam.uriResolvedAt = System.currentTimeMillis();
            Log.i(TAG, "[相机:" + cam.name + "] ONVIF子码流 " + p.res() + " → " + uri);
            return true;
        } catch (OnvifClient.OnvifException e) {
            Log.w(TAG, "[相机:" + cam.name + "] ONVIF解析失败: " + e.getMessage());
            return false;
        } catch (Exception e) {
            Log.w(TAG, "[相机:" + cam.name + "] ONVIF解析异常: " + e);
            return false;
        }
    }

    /**
     * 单路拉流主循环: ffmpeg -c copy → HLS(2s分片, 播放列表窗口30片≈60s,
     * delete_segments 自动删窗外文件=环形缓冲)。断线自动重连, 退避 2s→60s。
     * 进程退出码/异常仅记日志; 无重试风暴（统一退避）。
     */
    private void pullLoop(CamCfg cam) {
        long backoff = 2000;
        while (running) {
            // ONVIF 模式(ip非空): 解析子码流地址回填 cam.url(1小时重解析, 对齐 EXE resolve_rtsp_uri)
            if (!ensureUri(cam)) {
                try {
                    Thread.sleep(backoff);
                } catch (InterruptedException ie) {
                    return;
                }
                backoff = Math.min(backoff * 2, 60000);
                continue;
            }
            Process proc = null;
            try {
                // -nostdin 防后台进程抢 stdin; -timeout 5000000(微秒)=5秒无数据自行退出触发重连
                ProcessBuilder pb = new ProcessBuilder(
                        ffmpegPath, "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", cam.url,
                        "-c", "copy", "-f", "hls",
                        "-hls_time", "2", "-hls_list_size", "30",
                        "-hls_flags", "delete_segments",
                        "-hls_segment_filename", new File(cam.liveDir, "seg_%05d.ts").getAbsolutePath(),
                        new File(cam.liveDir, "index.m3u8").getAbsolutePath());
                pb.redirectErrorStream(false);
                proc = pb.start();
                backoff = 2000;   // 成功拉流后重置退避
                drain(proc.getErrorStream(), "campull-" + cam.name);
                proc.waitFor();
                Log.w(TAG, "[相机:" + cam.name + "] 拉流进程退出(code=" + proc.exitValue() + "), 重连中");
            } catch (Exception e) {
                Log.w(TAG, "[相机:" + cam.name + "] 拉流异常: " + e);
            } finally {
                if (proc != null) proc.destroy();
            }
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException ie) {
                return;
            }
            backoff = Math.min(backoff * 2, 60000);
        }
    }

    /** 排干子进程 stderr 防管道写满阻塞（error 级日志量极小, 全量转 logcat） */
    private static void drain(final InputStream in, final String tag) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                byte[] buf = new byte[4096];
                try {
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        Log.w(TAG, "[" + tag + "] ffmpeg: " + new String(buf, 0, n).trim());
                    }
                } catch (IOException ignore) {
                } finally {
                    try { in.close(); } catch (IOException ignore) {}
                }
            }
        }, tag + "-err").start();
    }

    // ---------- 报警快照（HLS 未就绪空窗期的兜底帧源, 按需启停） ----------

    /** 报警开始: 对绑定相机启动低频快照刷新（幂等; cam空/未知=默认相机, 对齐 EXE 回退语义） */
    public synchronized void startAlarmSnapshots(String camName) {
        if (ffmpegPath == null || !running) return;
        final CamCfg cam = resolveCam(camName);
        if (cam == null) return;
        stopAlarmSnapshots();   // 相机切换/重复触发: 停旧起新
        liveSnapCam = null;     // 快照线程槽位让给报警快照
        snapStop = false;
        snapThread = new Thread(new Runnable() {
            @Override
            public void run() {
                snapshotLoop(cam);
            }
        }, "camsnap-" + cam.name);
        snapThread.start();
    }

    /** 报警结束/取消: 停快照刷新（幂等） */
    public synchronized void stopAlarmSnapshots() {
        snapStop = true;
        Thread t = snapThread;
        snapThread = null;
        if (t != null) t.interrupt();
    }

    /**
     * 大屏小块视频口: 常驻低频快照(1s/帧, 与报警快照共用 snapThread 槽位)。
     * 同名相机已在跑则跳过; 报警来时 startAlarmSnapshots 会停旧起新(报警相机), 语义正确。
     * cam 空/未知 = 默认相机(resolveCam 回退)。
     */
    public synchronized void startLiveSnapshots(String camName) {
        if (ffmpegPath == null || !running) return;
        final CamCfg cam = resolveCam(camName);
        if (cam == null) return;
        if (snapThread != null && !snapStop && liveSnapCam != null && liveSnapCam.equals(cam.name)) {
            return;   // 同相机已在刷
        }
        stopAlarmSnapshots();
        snapStop = false;
        liveSnapCam = cam.name;
        snapThread = new Thread(new Runnable() {
            @Override
            public void run() {
                snapshotLoop(cam);
            }
        }, "camlive-" + cam.name);
        snapThread.start();
    }

    /** 最近一次常驻快照的相机名(供 UI 标注"● 房间 · 相机") */
    public volatile String liveSnapCam = null;

    /**
     * 停常驻小块快照(幂等): 仅在 live 快照在跑时清理——报警快照期间 liveSnapCam=null,
     * 不误停报警快照线程。
     */
    public synchronized void stopLiveSnapshots() {
        if (liveSnapCam == null) return;
        stopAlarmSnapshots();
        liveSnapCam = null;
    }

    /** 指定相机的最新快照 JPEG 文件(未配置/未知返回 null); UI 轮询按 mtime 变化取图 */
    public File snapshotFile(String camName) {
        CamCfg cam = resolveCam(camName);
        return cam == null ? null : cam.snapFile;
    }

    /** 拉流中相机数 / 已配置相机总数（大屏"摄像头"卡片用） */
    public int[] streamCounts() {
        int alive = 0;
        for (Thread t : pullThreads) {
            if (t != null && t.isAlive()) alive++;
        }
        return new int[]{alive, cams.size()};
    }

    /** 报警期快照循环: 一次性 ffmpeg 抓帧→临时文件→原子改名, ~1s/帧 */
    private void snapshotLoop(CamCfg cam) {
        while (!snapStop && running) {
            try {
                ProcessBuilder pb = new ProcessBuilder(
                        ffmpegPath, "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-y", "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", cam.url,
                        "-frames:v", "1", "-q:v", "5", cam.snapTmpFile.getAbsolutePath());
                pb.redirectErrorStream(false);
                Process proc = pb.start();
                drain(proc.getErrorStream(), "camsnap-" + cam.name);
                proc.waitFor();
                proc.destroy();
                if (!snapStop && proc.exitValue() == 0 && cam.snapTmpFile.isFile()
                        && cam.snapTmpFile.length() > 0) {
                    cam.snapTmpFile.renameTo(cam.snapFile);   // 原子替换, 读端不会拿到半张图
                } else {
                    cam.snapTmpFile.delete();
                }
            } catch (Exception e) {
                Log.w(TAG, "[相机:" + cam.name + "] 快照抓取失败: " + e);
            }
            try {
                Thread.sleep(1000);
            } catch (InterruptedException ie) {
                return;
            }
        }
    }

    // ---------- 报警剪辑（concat 最近分片 -c copy, 报警触发时调用） ----------

    /**
     * 全部报警联动相机并行式剪辑（顺序执行, 单路秒级; 对齐 EXE cut_clips_for_alarm 语义）。
     * 剪辑窗口: 分片中 lastModified 在 [now-14s, now] 的全部（10秒 + GOP/边界余量）。
     * 返回 [{"cam": 名, "name": 文件名}]（仅成功项）; 外部拼 URL 后推 WS。
     */
    public List<JSONObject> cutClipsForAlarm() {
        List<JSONObject> out = new ArrayList<>();
        if (ffmpegPath == null) return out;
        long minMtime = System.currentTimeMillis() - 14000;
        for (CamCfg cam : cams) {
            if (!cam.alarmEnabled) continue;
            try {
                String name = cutOne(cam, minMtime);
                if (name != null) {
                    JSONObject o = new JSONObject();
                    o.put("cam", cam.name);
                    o.put("name", name);
                    out.add(o);
                }
            } catch (Exception e) {
                Log.e(TAG, "[相机:" + cam.name + "] 报警剪辑失败: " + e);
            }
        }
        return out;
    }

    /** 单路剪辑: 收集窗口分片 → concat list → -c copy 出 mp4 → 末3秒抽封面 jpg */
    private String cutOne(CamCfg cam, long minMtime) throws Exception {
        File[] segs = cam.liveDir.listFiles();
        List<File> picked = new ArrayList<>();
        if (segs != null) {
            Arrays.sort(segs, new Comparator<File>() {
                @Override
                public int compare(File a, File b) {
                    return a.getName().compareTo(b.getName());
                }
            });
            for (File f : segs) {
                if (f.getName().matches("seg_\\d{5}\\.ts") && f.lastModified() >= minMtime) {
                    picked.add(f);
                }
            }
        }
        if (picked.isEmpty()) {
            Log.w(TAG, "[相机:" + cam.name + "] 窗口内无分片(刚启动/断流), 放弃剪辑");
            return null;
        }
        File captures = StorageUtil.archiveRoot(app, Prefs.CLIP_STORAGE, "captures");   // 存档位置按设置(U盘/板载, 未挂载回退)
        String base = safeName(cam.name) + "_"
                + new java.text.SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
                        .format(new java.util.Date())
                + String.format(Locale.US, "_%03d", System.currentTimeMillis() % 1000);
        File mp4 = new File(captures, base + ".mp4");
        File jpg = new File(captures, base + ".jpg");
        // concat 清单: -safe 0 允许绝对路径
        File listFile = new File(cam.liveDir, "concat_list.txt");
        StringBuilder sb = new StringBuilder();
        for (File f : picked) {
            sb.append("file '").append(f.getAbsolutePath().replace("'", "'\\''")).append("'\n");
        }
        writeText(listFile, sb.toString());
        try {
            // 拷贝封装不转码（剪辑秒级完成）; +faststart 供手机边下边播
            Process p = new ProcessBuilder(
                    ffmpegPath, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "concat", "-safe", "0", "-i", listFile.getAbsolutePath(),
                    "-c", "copy", "-movflags", "+faststart", mp4.getAbsolutePath())
                    .redirectErrorStream(false).start();
            drain(p.getErrorStream(), "camcut-" + cam.name);
            p.waitFor();
            p.destroy();
            if (p.exitValue() != 0 || !mp4.isFile() || mp4.length() == 0) {
                throw new IOException("mp4 生成失败");
            }
            // 封面帧: 成品 mp4 末尾3秒取第一帧(=报警时刻最新画面); 失败降级仅留 mp4
            Process p2 = new ProcessBuilder(
                    ffmpegPath, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-sseof", "-3", "-i", mp4.getAbsolutePath(),
                    "-frames:v", "1", "-q:v", "4", jpg.getAbsolutePath())
                    .redirectErrorStream(false).start();
            drain(p2.getErrorStream(), "camthumb-" + cam.name);
            p2.waitFor();
            p2.destroy();
            if (p2.exitValue() != 0) jpg.delete();
            Log.i(TAG, "[相机:" + cam.name + "] 报警剪辑已存: " + mp4.getName()
                    + " (" + picked.size() + "分片)");
            return mp4.getName();
        } finally {
            listFile.delete();
        }
    }

    private static void writeText(File f, String s) throws IOException {
        FileOutputStream out = new FileOutputStream(f);
        out.write(s.getBytes("UTF-8"));
        out.close();
    }

    // ---------- CamProvider 实现（HeartServer 路由调用） ----------

    @Override
    public byte[] snapshotJpeg(String cam) {
        CamCfg c = resolveCam(cam);
        if (c == null || !c.snapFile.isFile()) return null;
        return readFile(c.snapFile);
    }

    @Override
    public File clipFile(String name) {
        if (name == null || name.isEmpty()) return null;
        // 防路径穿越: 只允许纯文件名 + .mp4 后缀（对齐 EXE handle_clip 校验）
        if (name.contains("/") || name.contains("\\") || !name.equals(new File(name).getName())
                || !name.toLowerCase(Locale.US).endsWith(".mp4")) {
            return null;
        }
        File f = new File(StorageUtil.archiveRoot(app, Prefs.CLIP_STORAGE, "captures"), name);
        if (f.isFile()) return f;
        File legacy = new File(new File(app.getFilesDir(), "captures"), name);   // 旧文件不迁移
        return legacy.isFile() ? legacy : null;
    }

    /** 全部报警剪辑(存档根+旧板载目录合并去重, 按时间倒序; 记录页剪辑卡片用) */
    public File[] listClips() {
        java.util.TreeMap<String, File> byName = new java.util.TreeMap<>();
        File[] roots = {
                StorageUtil.archiveRoot(app, Prefs.CLIP_STORAGE, "captures"),
                new File(app.getFilesDir(), "captures")};
        for (File root : roots) {
            File[] fs = root.listFiles();
            if (fs == null) continue;
            for (File f : fs) {
                if (f.isFile() && f.getName().toLowerCase(Locale.US).endsWith(".mp4")) {
                    byName.put(f.getName(), f);
                }
            }
        }
        File[] out = byName.values().toArray(new File[0]);
        Arrays.sort(out, new Comparator<File>() {
            @Override
            public int compare(File a, File b) {
                return Long.compare(b.lastModified(), a.lastModified());
            }
        });
        return out;
    }

    @Override
    public File livePlaylist(String cam) {
        CamCfg c = resolveCam(cam);
        if (c == null) return null;
        File f = new File(c.liveDir, "index.m3u8");
        return f.isFile() ? f : null;
    }

    @Override
    public File liveSegment(String cam, String seg) {
        CamCfg c = resolveCam(cam);
        if (c == null) return null;
        File f = new File(c.liveDir, seg);
        return f.isFile() ? f : null;
    }

    /** 相机解析: 名称精确匹配; 空/未知回退默认相机（对齐 EXE snapshot_jpeg_by_name 语义） */
    private CamCfg resolveCam(String name) {
        if (name != null && !name.isEmpty()) {
            for (CamCfg c : cams) {
                if (c.name.equals(name)) return c;
            }
        }
        CamCfg def = null;
        for (CamCfg c : cams) {
            if (c.isDefault) return c;
            if (def == null) def = c;   // 无显式默认时取第一路
        }
        return def;
    }

    /** 默认相机名（剪辑 URL 拼装/快照联动用; 无配置返回空串） */
    public String defaultName() {
        CamCfg c = resolveCam("");
        return c == null ? "" : c.name;
    }

    /** 相机列表 JSON（看板 tab 渲染用; 快照遍历防 stop 并发清理） */
    @Override
    public String camListJson() {
        if (ffmpegPath == null || !running) return "[]";
        JSONArray arr = new JSONArray();
        for (CamCfg c : cams.toArray(new CamCfg[0])) {
            JSONObject o = new JSONObject();
            try {
                o.put("name", c.name);
                o.put("is_default", c.isDefault);
            } catch (Exception ignore) {
            }
            arr.put(o);
        }
        return arr.toString();
    }

    /**
     * 看板 MSE 按需推流（P1-6）: 为指定相机起 fMP4 推流进程（一客户端一路）。
     * 与常驻 HLS 拉流互不相干（各自独立 RTSP 连接, 摄像头支持多客户端取流）;
     * 断开即由调用方 stop, 严格按需。
     */
    @Override
    public ViewStream startMseStream(String camName, ViewStream.Sink sink, Runnable onEnd) {
        if (ffmpegPath == null || !running) return null;
        CamCfg cam = resolveCam(camName);
        if (cam == null || cam.url.isEmpty()) return null;
        return new ViewStream(ffmpegPath, cam.url, cam.name, sink, onEnd);
    }

    private static byte[] readFile(File f) {
        try {
            FileInputStream in = new FileInputStream(f);
            byte[] out = new byte[(int) f.length()];
            int off = 0;
            while (off < out.length) {
                int n = in.read(out, off, out.length - off);
                if (n < 0) break;
                off += n;
            }
            in.close();
            return off == out.length ? out : Arrays.copyOf(out, off);
        } catch (IOException e) {
            return null;
        }
    }
}
