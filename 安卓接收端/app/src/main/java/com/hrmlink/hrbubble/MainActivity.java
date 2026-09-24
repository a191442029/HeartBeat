package com.hrmlink.hrbubble;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.BitmapFactory;
import android.graphics.SurfaceTexture;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * 首页=监测页: 大号心率+实时波形+状态条, 横竖屏双布局
 * - 配置(IP/端口/仅轮询)与权限引导在设置页(标题栏"设置"进入)
 * - 悬浮窗为独立开关: 不开浮窗也可只看首页; 开浮窗未监测时自动建连
 * - 系统标题栏即工具栏: 标题=HRBubble+本地日期时间, 右侧菜单项=设置/悬浮窗/监测
 * - 期3报警视频面板: 报警时波形区替换为默认摄像头画面(2fps快照轮询)+[▶ 回放]按钮,
 *   点击应用内 MediaPlayer 流式播放报警前20秒剪辑(不跳系统播放器, 单实例规则),
 *   播完销毁播放器回到快照轮询; 取消报警/报警结束恢复波形
 */
public class MainActivity extends Activity {

    private static final String PREFS = "hrbubble";
    private static final long SNAP_INTERVAL_MS = 500; // 快照轮询周期(2fps)

    private TextView txtStatus, txtHr, txtBpm;
    private TextView txtReconnect; // 波形上方附加状态行(EXE智能重连进度), 无内容时隐藏
    private View dotHome;
    private TextView btnAlarmCancel; // 远程报警取消按钮(仅报警期间显示)
    private HeartWaveView wave;
    private ImageView imgHeart;
    private BroadcastReceiver receiver;
    private TextView txtTime;
    // 期3 报警视频面板
    private View videoPanel;
    private ImageView imgSnap;
    private TextureView videoView;
    private TextView btnPlay;
    private MediaPlayer player;          // 单实例: 播放前先销毁上一个
    private boolean pendingPlay = false; // TextureView surface未就绪时挂起播放请求
    private String clipUrl = "";         // EXE推送的报警剪辑地址(空=未成型; 报警结束后保留供回看)
    private boolean alarmActive = false; // 当前是否处于EXE报警周期(区分报警中回放与报警后回看)
    private java.util.List<String[]> clipList = new java.util.ArrayList<>(); // 全部相机剪辑[{cam名,url}]
    private int currentClipIdx = 0;      // 当前回放的剪辑索引(视频面板tab切换)
    private TextView btnReplay;          // 报警结束后的回看入口(不随视频面板消失)
    private android.widget.LinearLayout clipTabs;          // 多相机剪辑切换tab容器
    private android.view.View clipTabsScroll;              // tab外层滚动条(单剪辑时隐藏)
    // 报警视频联动(2026-09-25): 绑定摄像头/房间名/HLS直播
    private TextView txtRoom;            // 面板左上角红色房间标签("房间: XXX")
    private TextView txtLiveBadge;       // 面板右上角"● 实时"角标(仅直播播放中显示)
    private String alarmCam = "";        // 报警联动摄像头名(快照URL带cam参数, 空=默认摄像头)
    private String alarmRoom = "";       // 报警联动房间名(空=不显示标签)
    private String liveUrl = "";         // EXE推送的HLS直播地址(首分片就绪后异步推送, 报警开始时为空)
    private boolean livePlaying = false;  // 当前处于HLS直播态(区分直播与剪辑回放)
    private boolean pendingLive = false;  // TextureView surface未就绪时挂起直播请求
    private int liveRetries = 0;          // 直播失败重试计数(≤5次, 超限本轮报警内回退纯快照)
    private final android.os.Handler liveHandler = new android.os.Handler();
    private Runnable liveRetryTask;
    private long lastDataWallMs = 0;     // 上次收到心率数据的本地时钟(断流断点检测用)
    // EXE失联兜底: 监测运行中且>3秒没收到任何数据包(EXE进程退出/Tailscale断),
    // 每秒注入一个波形断点(示波器式滚动), 直至数据恢复; 正常断流由EXE的
    // hr=0断流心跳驱动滚动, 此Handler仅在完全失联时接管
    private final android.os.Handler scrollHandler = new android.os.Handler();
    private Runnable scrollTask;
    private ScheduledExecutorService snapExec;
    private ScheduledFuture<?> snapTask;
    private final android.os.Handler main = new android.os.Handler();
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
        btnAlarmCancel = findViewById(R.id.btn_alarm_cancel);
        videoPanel = findViewById(R.id.video_panel);
        imgSnap = findViewById(R.id.img_snap);
        videoView = findViewById(R.id.video_view);
        btnPlay = findViewById(R.id.btn_play);
        btnReplay = findViewById(R.id.btn_replay);
        clipTabs = findViewById(R.id.clip_tabs);
        clipTabsScroll = findViewById(R.id.clip_tabs_scroll);
        txtRoom = findViewById(R.id.txt_room);
        txtLiveBadge = findViewById(R.id.txt_live_badge);
        // 取消本次报警: 立即停铃并隐藏按钮(本次报警周期内不会再响), 同时退出视频面板恢复波形;
        // 剪辑已到手则显示"回看"入口(取消≠删档, 事后仍可回放)
        btnAlarmCancel.setOnClickListener(v -> {
            AlarmPlayer.cancel();
            btnAlarmCancel.setVisibility(View.GONE);
            exitAlarmVideo();
            refreshReplayEntry();
        });
        btnReplay.setOnClickListener(v -> {
            enterAlarmVideo();
            playCurrent();
        });
        btnPlay.setOnClickListener(v -> playCurrent());
        // TextureView surface就绪后执行挂起的播放(单实例: 启动前确保旧播放器已销毁; 直播优先)
        videoView.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override
            public void onSurfaceTextureAvailable(SurfaceTexture st, int w, int h) {
                if (pendingLive) {
                    pendingLive = false;
                    startLivePlayer(st);
                } else if (pendingPlay) {
                    pendingPlay = false;
                    String url = clipList.isEmpty() ? "" : clipList.get(currentClipIdx)[1];
                    if (!url.isEmpty()) startPlayer(st, url);
                }
            }

            @Override
            public void onSurfaceTextureSizeChanged(SurfaceTexture st, int w, int h) {
            }

            @Override
            public boolean onSurfaceTextureDestroyed(SurfaceTexture st) {
                stopPlayer();
                return true;
            }

            @Override
            public void onSurfaceTextureUpdated(SurfaceTexture st) {
            }
        });
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
            lastDataWallMs = 0; // 停止监测后重置断流检测基线
            setStatusDot(false, false, "");
            exitAlarmVideo(); // 停止监测: 退出视频面板恢复波形
            clipUrl = "";
            clipList.clear();
            alarmActive = false;
            btnReplay.setVisibility(View.GONE);
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
        // 息屏/切后台时本页onStop: 广播已注销+滚动tick停止, 波形窗口冻结在离开前的状态;
        // 若此时距上一包数据超过15秒, 恢复可见时先注入一个断点抬笔并拉平基线,
        // 防止恢复后的新波形与离开前的旧波形被无缝相连(必须在registerReceiver与
        // startScrollTick之前做: tick的拉平基线会在1秒内抹掉间隔证据, 让收包处的
        // >15秒断点检查永远看不到真实间隔)
        long resumeWall = System.currentTimeMillis();
        if (lastDataWallMs > 0 && resumeWall - lastDataWallMs > 15000) {
            wave.push(0);
            lastDataWallMs = resumeWall;
        }
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
                    // 断流断点: 与上一包数据间隔>15秒(息屏断网/手表断开重连等)时先抬笔,
                    // 防止息屏前的旧波形与恢复后的新波形被无缝相连, 造成心率"跳变"假象
                    long nowWall = System.currentTimeMillis();
                    if (lastDataWallMs > 0 && nowWall - lastDataWallMs > 15000) {
                        wave.push(0);
                    }
                    lastDataWallMs = nowWall;
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
                    // 报警取消按钮: 报警中且未被取消时显示, 报警结束/已取消隐藏;
                    // 视频面板仅报警期间显示, 报警结束后剪辑保留(蓝色"回看"按钮入口)
                    boolean alarm = i.getBooleanExtra("alarm", false);
                    // 剪辑地址: 空值不覆盖(EXE报警开始会清空clip_url, 但剪辑成型后的每条广播都带全量值)
                    String cu = i.getStringExtra("clip_url");
                    if (cu != null && !cu.isEmpty()) clipUrl = cu;
                    String clipsJson = i.getStringExtra("clips");
                    if (clipsJson != null && !clipsJson.isEmpty()) parseClips(clipsJson);
                    // 报警视频联动字段: 空值不覆盖(alarm_live首分片就绪后才异步推送)
                    String cam = i.getStringExtra("alarm_cam");
                    if (cam != null && !cam.isEmpty()) alarmCam = cam;
                    String room = i.getStringExtra("alarm_room");
                    if (room != null && !room.isEmpty()) alarmRoom = room;
                    String lu = i.getStringExtra("alarm_live");
                    if (lu != null && !lu.isEmpty()) liveUrl = lu;
                    if (alarm && !alarmActive) { // 新报警边沿: 清上一轮剪辑与直播状态, 防误播旧片段
                        clipUrl = "";
                        clipList.clear();
                        alarmCam = "";
                        alarmRoom = "";
                        liveUrl = "";
                        liveRetries = 0;
                        // 边沿当条广播已带上新值则立即回填
                        if (cam != null) alarmCam = cam;
                        if (room != null) alarmRoom = room;
                        if (lu != null) liveUrl = lu;
                    }
                    alarmActive = alarm;
                    if (alarm && !AlarmPlayer.isCanceled()) {
                        btnAlarmCancel.setVisibility(View.VISIBLE);
                        btnReplay.setVisibility(View.GONE);
                        enterAlarmVideo();
                        // 直播地址后到(EXE首分片就绪后异步推送): 快照模式下立即切直播
                        if (!liveUrl.isEmpty() && !livePlaying && player == null
                                && videoView.getVisibility() != View.VISIBLE) {
                            tryLivePlayback();
                        }
                    } else {
                        btnAlarmCancel.setVisibility(View.GONE);
                        exitAlarmVideo();
                        refreshReplayEntry();
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
        startScrollTick();
    }

    @Override
    protected void onStop() {
        super.onStop();
        stopClock();
        stopScrollTick();
        exitAlarmVideo(); // 离开页面整体退出视频态(销毁播放器+停轮询), 回前台由下一包数据重建
        if (receiver != null) {
            unregisterReceiver(receiver);
            receiver = null;
        }
    }

    /** EXE失联兜底滚动(每秒检查): 监测中且>3秒无任何数据包 → 注入波形断点 */
    private void startScrollTick() {
        if (scrollTask != null) return;
        scrollTask = new Runnable() {
            @Override
            public void run() {
                if (HeartRateService.sRunning && lastDataWallMs > 0) {
                    long now = System.currentTimeMillis();
                    if (now - lastDataWallMs > 3000) {
                        wave.push(0);        // 断点滚动一格
                        lastDataWallMs = now; // 拉平基线, 下秒再注入, 直至恢复
                    }
                }
                scrollHandler.postDelayed(this, 1000);
            }
        };
        scrollHandler.postDelayed(scrollTask, 1000);
    }

    private void stopScrollTick() {
        if (scrollTask != null) {
            scrollHandler.removeCallbacks(scrollTask);
            scrollTask = null;
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

    // ==================== 期3 报警视频面板 ====================

    /** 进入报警视频态: 波形区替换为摄像头画面(优先HLS直播, 快照轮询兜底/顶缺+回放按钮+相机tab), 幂等 */
    private void enterAlarmVideo() {
        if (videoPanel.getVisibility() != View.VISIBLE) {
            wave.setVisibility(View.GONE);
            videoPanel.setVisibility(View.VISIBLE);
            imgSnap.setVisibility(View.VISIBLE);
            videoView.setVisibility(View.GONE);
            btnPlay.setVisibility(View.VISIBLE);
            btnPlayGravityCorner(false);
        }
        updateRoomLabel();
        buildClipTabs();
        if (alarmActive && !liveUrl.isEmpty()) {
            tryLivePlayback(); // 幂等: 已在播放中则内部直接返回
        } else {
            startSnapPolling(); // 幂等: 旋转重建/回前台后恢复轮询
        }
    }

    /** 退出报警视频态: 销毁播放器+停直播重试+停轮询, 恢复波形(取消报警/报警结束) */
    private void exitAlarmVideo() {
        if (videoPanel == null || videoPanel.getVisibility() != View.VISIBLE) return;
        stopPlayer();
        cancelLiveRetry();
        livePlaying = false;
        pendingLive = false;
        txtLiveBadge.setVisibility(View.GONE);
        txtRoom.setVisibility(View.GONE);
        stopSnapPolling();
        videoPanel.setVisibility(View.GONE);
        wave.setVisibility(View.VISIBLE);
    }

    /** 面板左上角红色房间标签: 有绑定房间名才显示 */
    private void updateRoomLabel() {
        if (alarmRoom == null || alarmRoom.isEmpty()) {
            txtRoom.setVisibility(View.GONE);
        } else {
            txtRoom.setText("房间: " + alarmRoom);
            txtRoom.setVisibility(View.VISIBLE);
        }
    }

    /** 回看入口可见性: 非报警周期且剪辑已到手时显示"回看报警视频"按钮 */
    private void refreshReplayEntry() {
        boolean show = !alarmActive && !clipList.isEmpty();
        btnReplay.setVisibility(show ? View.VISIBLE : View.GONE);
    }

    /** 解析EXE推送的全量剪辑列表 [{"cam":"卧室","url":"http://..."}]; 单剪辑tab隐藏 */
    private void parseClips(String json) {
        try {
            org.json.JSONArray arr = new org.json.JSONArray(json);
            java.util.List<String[]> list = new java.util.ArrayList<>();
            for (int i = 0; i < arr.length(); i++) {
                org.json.JSONObject o = arr.getJSONObject(i);
                String url = o.optString("url", "");
                if (url.isEmpty()) continue;
                list.add(new String[]{o.optString("cam", "监控"), url});
            }
            if (!list.isEmpty()) clipList = list;
        } catch (Exception ignored) {
        }
        // 单地址兼容(EXE旧版/仅clip_url): 凑一条"监控"tab
        if (clipList.isEmpty() && clipUrl != null && !clipUrl.isEmpty()) {
            clipList.add(new String[]{"监控", clipUrl});
        }
    }

    /** 重建相机剪辑tab(>1个剪辑时显示), 选中项高亮 */
    private void buildClipTabs() {
        if (clipTabs == null) return;
        clipTabs.removeAllViews();
        boolean multi = clipList.size() > 1;
        clipTabsScroll.setVisibility(multi ? View.VISIBLE : View.GONE);
        if (!multi) return;
        for (int i = 0; i < clipList.size(); i++) {
            final int idx = i;
            TextView t = new TextView(this);
            t.setText(clipList.get(i)[0]);
            t.setPadding(28, 12, 28, 12);
            t.setTextSize(14);
            t.setClickable(true);
            t.setOnClickListener(v -> {
                currentClipIdx = idx;
                highlightTabs();
                playCurrent();
            });
            clipTabs.addView(t);
        }
        highlightTabs();
    }

    /** tab选中态: 选中白字高亮, 未选中灰字 */
    private void highlightTabs() {
        for (int i = 0; i < clipTabs.getChildCount(); i++) {
            TextView t = (TextView) clipTabs.getChildAt(i);
            t.setTextColor(i == currentClipIdx ? 0xFFFFFFFF : 0xFF9E9E9E);
        }
    }

    /** 回放按钮: 按当前tab索引流式播放剪辑(禁用系统播放器跳转, 单实例规则; 先停HLS直播) */
    private void playCurrent() {
        if (clipList.isEmpty()) {
            Toast.makeText(this, "剪辑生成中, 稍候几秒再试", Toast.LENGTH_SHORT).show();
            return;
        }
        if (currentClipIdx >= clipList.size()) currentClipIdx = 0;
        String url = clipList.get(currentClipIdx)[1];
        cancelLiveRetry();
        livePlaying = false;
        pendingLive = false;
        txtLiveBadge.setVisibility(View.GONE);
        btnPlayGravityCorner(false);
        stopSnapPolling();
        imgSnap.setVisibility(View.GONE);
        btnPlay.setVisibility(View.GONE);
        videoView.setVisibility(View.VISIBLE);
        if (videoView.isAvailable()) {
            startPlayer(videoView.getSurfaceTexture(), url);
        } else {
            pendingPlay = true; // surface就绪回调里启动
        }
    }

    private void startPlayer(SurfaceTexture st, String url) {
        stopPlayer(); // 单实例规则: 先销毁旧播放器
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                    .build());
            player.setDataSource(url);
            player.setSurface(new Surface(st));
            player.setOnPreparedListener(MediaPlayer::start);
            player.setOnCompletionListener(mp -> endClipPlayback());
            player.setOnErrorListener((mp, what, extra) -> {
                endClipPlayback();
                return true;
            });
            player.prepareAsync();
        } catch (Exception e) {
            endClipPlayback();
        }
    }

    /** 播放结束/出错: 销毁播放器; 报警中切回实时快照轮询, 报警后回看则直接恢复波形 */
    private void endClipPlayback() {
        stopPlayer();
        videoView.setVisibility(View.GONE);
        imgSnap.setVisibility(View.VISIBLE);
        btnPlay.setVisibility(View.VISIBLE);
        if (alarmActive) {
            startSnapPolling();
        } else {
            exitAlarmVideo();
            refreshReplayEntry();
        }
    }

    private void stopPlayer() {
        MediaPlayer p = player;
        player = null;
        pendingPlay = false;
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

    /** 报警期间2fps轮询联动摄像头快照(加载EXE的/camera/snapshot JPEG; 有绑定名带cam参数, 未知名EXE回退默认) */
    private void startSnapPolling() {
        if (snapTask != null) return;
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        String server = sp.getString("server", "");
        if (server.isEmpty()) return;
        String base = "http://" + server + ":" + sp.getString("port", "8765") + "/camera/snapshot";
        final String url = (alarmCam == null || alarmCam.isEmpty())
                ? base : base + "?cam=" + android.net.Uri.encode(alarmCam);
        if (snapExec == null) {
            snapExec = Executors.newSingleThreadScheduledExecutor();
        }
        snapTask = snapExec.scheduleWithFixedDelay(() -> {
            okhttp3.OkHttpClient client = HeartRateService.pollClientStatic();
            if (client == null) return; // 服务已停止, 不再轮询
            try {
                okhttp3.Response resp = client
                        .newCall(new okhttp3.Request.Builder().url(url).build()).execute();
                try {
                    if (resp.body() != null && resp.code() == 200) {
                        byte[] data = resp.body().bytes();
                        android.graphics.Bitmap bm = BitmapFactory.decodeByteArray(data, 0, data.length);
                        if (bm != null) main.post(() -> {
                            if (imgSnap != null && player == null
                                    && videoPanel.getVisibility() == View.VISIBLE) {
                                imgSnap.setImageBitmap(bm);
                            }
                        });
                    }
                } finally {
                    resp.close();
                }
            } catch (Exception ignored) {
            }
        }, 0, SNAP_INTERVAL_MS, TimeUnit.MILLISECONDS);
    }

    private void stopSnapPolling() {
        if (snapTask != null) {
            snapTask.cancel(false);
            snapTask = null;
        }
    }

    // ==================== HLS直播(报警视频联动, 2026-09-25) ====================
    // 与EXE侧camera/hls_stream.py配合: ffmpeg转码640x360@15fps 1秒GOP, m3u8分片窗口3秒;
    // MediaPlayer原生支持HLS, 低内存设备友好。直播地址(EXE首分片就绪后推送)形如
    // http://ip:port/camera/live/index.m3u8?cam=名

    /** 尝试HLS直播: 复用单实例MediaPlayer+TextureView; 失败重试≤5次×1.2秒, 等待期快照顶上 */
    private void tryLivePlayback() {
        if (liveUrl == null || liveUrl.isEmpty() || livePlaying) return;
        cancelLiveRetry();
        stopSnapPolling();
        imgSnap.setVisibility(View.GONE);
        btnPlay.setVisibility(View.VISIBLE); // 直播中保留"回放"入口(右下角)
        btnPlayGravityCorner(true);
        videoView.setVisibility(View.VISIBLE);
        livePlaying = true;
        if (videoView.isAvailable()) {
            startLivePlayer(videoView.getSurfaceTexture());
        } else {
            pendingLive = true; // surface就绪回调里启动
        }
    }

    private void startLivePlayer(SurfaceTexture st) {
        stopPlayer(); // 单实例规则: 先销毁旧播放器
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                    .build());
            player.setDataSource(liveUrl);
            player.setSurface(new Surface(st));
            player.setOnPreparedListener(mp -> { // 首分片解析成功: 显示"● 实时"角标并开播
                txtLiveBadge.setVisibility(View.VISIBLE);
                mp.start();
            });
            player.setOnErrorListener((mp, what, extra) -> {
                scheduleLiveRetry();
                return true;
            });
            player.setOnCompletionListener(mp -> scheduleLiveRetry()); // 直播流中断视为失败重试
            player.prepareAsync();
        } catch (Exception e) {
            scheduleLiveRetry();
        }
    }

    /** 直播失败: 销毁播放器, 快照顶上, 1.2秒后重试; 累计≤5次, 超限本轮报警内回退纯快照 */
    private void scheduleLiveRetry() {
        stopPlayer();
        livePlaying = false;
        pendingLive = false;
        txtLiveBadge.setVisibility(View.GONE);
        videoView.setVisibility(View.GONE);
        imgSnap.setVisibility(View.VISIBLE);
        btnPlayGravityCorner(false);
        startSnapPolling();
        if (liveRetries >= 5) {
            fallbackToSnapshot();
            return;
        }
        liveRetries++;
        cancelLiveRetry();
        liveRetryTask = () -> tryLivePlayback();
        liveHandler.postDelayed(liveRetryTask, 1200);
    }

    /** 重试超限回退: 清空本轮直播地址(广播自动切换逻辑随之失效), 纯快照轮询到报警结束 */
    private void fallbackToSnapshot() {
        liveUrl = "";
        txtLiveBadge.setVisibility(View.GONE);
        btnPlay.setVisibility(View.VISIBLE);
        btnPlayGravityCorner(false);
    }

    private void cancelLiveRetry() {
        if (liveRetryTask != null) {
            liveHandler.removeCallbacks(liveRetryTask);
            liveRetryTask = null;
        }
    }

    /** 回放按钮位置: 直播时右下角(不挡画面), 快照/剪辑态居中 */
    private void btnPlayGravityCorner(boolean corner) {
        android.widget.FrameLayout.LayoutParams lp =
                (android.widget.FrameLayout.LayoutParams) btnPlay.getLayoutParams();
        int g = corner ? (android.view.Gravity.BOTTOM | android.view.Gravity.END)
                : android.view.Gravity.CENTER;
        if (lp.gravity != g) {
            lp.gravity = g;
            lp.setMargins(0, 0, corner ? 24 : 0, corner ? 24 : 0);
            btnPlay.setLayoutParams(lp);
        }
    }
}
