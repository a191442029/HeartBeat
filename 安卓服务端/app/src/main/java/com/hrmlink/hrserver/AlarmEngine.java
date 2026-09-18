package com.hrmlink.hrserver;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Calendar;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;

/**
 * 告警引擎 —— 1:1 对标 EXE push_notifier.py(NotifierManager) + irregularity_detector.py
 *
 * 职责(本引擎不做任何网络请求, 推送发送由外部渠道模块负责):
 *  - 高/低心率告警: 默认规则 + 自定义时段规则( [起始,结束) 半开区间, 支持跨零点,
 *    按顺序取首条命中, 无命中走默认规则 ); 持续判定(连续超过 duration 秒才推) +
 *    各类别(规则×过高/过低)独立计时与独立冷却, 互不共CD;
 *    恢复正常即清空该方向 since 残留, 新一轮异常从零重新计时
 *  - 疑似心律不齐: 滑动窗口无序性检测(静息门控+趋势门控+标准差/大幅跳变占比
 *    双指标同时命中+连续窗口确认+独立冷却), 断连清窗
 *  - 设备断连/恢复提醒: 状态边沿触发(对齐 EXE watch_device_connection 2秒轮询的
 *    边沿检测, 无独立冷却计时, 状态不变不重复推送)
 *
 * 线程模型: HeartBus 监听回调统一在主线程派发, 本引擎全部状态仅在主线程读写;
 * start/stop 约定主线程调用; reloadConfig 若在子线程调用会自动切到主线程执行。
 */
public class AlarmEngine {

    private static final String TAG = "HRServer";

    /** 告警类别 kind: 1=过高 2=过低 3=疑似心律不齐 4=设备失联 5=设备恢复 */
    public static final int KIND_HIGH = 1;
    public static final int KIND_LOW = 2;
    public static final int KIND_IRREGULAR = 3;
    public static final int KIND_DEV_LOST = 4;
    public static final int KIND_DEV_BACK = 5;

    /** 告警出口: Service 将其接到 PushChannels.push, 网络发送由外部负责 */
    public interface AlarmListener {
        void onAlarm(String title, String body, int kind);
    }

    private final Context appContext;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private volatile AlarmListener listener;
    private boolean started = false;

    // ---- 默认规则配置(loadConfig 从 Prefs 读取, 对齐 NotifierManager.load_config) ----
    private int defMaxHr;      // 心率上限告警阈值, 0=不检测
    private int defMinHr;      // 心率下限告警阈值, 0=不检测
    private int defDuration;   // 异常持续判定(秒)
    private int defCooldown;   // 告警冷却期(秒)

    /** 自定义时段规则列表(每条含独立上下限/持续/冷却), 对齐 py self.periods */
    private final List<PeriodRule> periods = new ArrayList<>();

    // ---- 高/低心率状态: key="规则tag:high/low" -> 独立计时, 对齐 py self._hr_state ----
    private final HashMap<String, HrState> hrState = new HashMap<>();

    // ---- 疑似心律不齐检测器(独立启停; null=未启用, 对齐 py self.irr_detector) ----
    private IrrDetector irrDetector;

    // ---- 设备连接边沿检测(对齐 watch_device_connection 的 _last_watch_connected) ----
    private Boolean lastConnected;   // null=未初始化(首个状态事件仅记录, 不推送)

    /** HeartBus 数据入口: hr>0 为心率数据, status 变化驱动断连/恢复提醒 */
    private final HeartBus.Listener busListener = new HeartBus.Listener() {
        @Override
        public void onHeartRate(int hr, String ts, String status) {
            onBusEvent(hr, status);
        }
    };

    /** 读 Prefs 初始化 */
    public AlarmEngine(Context context) {
        appContext = context.getApplicationContext();
        loadConfig();
    }

    /** Service 把它接到 PushChannels.push */
    public void setAlarmListener(AlarmListener l) {
        listener = l;
    }

    /** 注册 HeartBus 监听, 开始工作(可 stop 后重复调用) */
    public void start() {
        if (started) return;
        started = true;
        loadConfig();   // (重)建规则与检测器, 保证 stop 清空后可正常重新启动
        // 首次运行仅记录初始状态, 不推送(对齐 watch_device_connection 首轮行为)
        lastConnected = "connected".equals(HeartBus.get().getStatus());
        HeartBus.get().addListener(busListener);
        Log.i(TAG, "AlarmEngine 启动: 时段规则" + periods.size() + "条, 心律不齐检测="
                + (irrDetector != null ? "开" : "关"));
    }

    /** 注销监听, 停所有定时器, 状态清空 */
    public void stop() {
        HeartBus.get().removeListener(busListener);
        mainHandler.removeCallbacksAndMessages(null);
        started = false;
        hrState.clear();        // 高/低心率计时状态清空
        irrDetector = null;     // 心律不齐窗口清空
        lastConnected = null;
        Log.i(TAG, "AlarmEngine 停止, 状态已清空");
    }

    /**
     * 设置页改参数后热重载: 重读 Prefs。
     * 对齐 EXE load_config: 规则集变化后清空各类别计时状态, 重建心律不齐检测器
     * (窗口/连续计数/冷却全清), 即冷却状态一并清空。
     */
    public void reloadConfig() {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            loadConfig();
        } else {
            // 引擎状态仅允许主线程读写, 子线程调用时切回主线程
            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    loadConfig();
                }
            });
        }
    }

    // ==================== 配置加载(对齐 load_config / _load_periods) ====================

    /** 重读 Prefs 并重建全部规则状态; 必须主线程调用 */
    private void loadConfig() {
        try {
            defMaxHr = Prefs.getInt(appContext, Prefs.PUSH_MAX_HR, 150);
            defMinHr = Prefs.getInt(appContext, Prefs.PUSH_MIN_HR, 45);
            defDuration = Prefs.getInt(appContext, Prefs.PUSH_ABNORMAL_DURATION, 600);
            defCooldown = Prefs.getInt(appContext, Prefs.PUSH_COOLDOWN_SECONDS, 300);
            periods.clear();
            periods.addAll(parsePeriods(Prefs.getStr(appContext, Prefs.PUSH_PERIODS, "[]")));
            // 规则集变化后清空各类别计时状态(对齐 py: self._hr_state = {})
            hrState.clear();
            // 疑似心律不齐检测器: 独立于渠道开关; 重建即窗口/连续计数/冷却全清(对齐 py)
            if (Prefs.getBool(appContext, Prefs.IRR_ENABLED, false)) {
                irrDetector = new IrrDetector(
                        Prefs.getInt(appContext, Prefs.IRR_WINDOW_SECONDS, 300),
                        Prefs.getInt(appContext, Prefs.IRR_SD_THRESHOLD, 50),
                        Prefs.getInt(appContext, Prefs.IRR_JUMP_BPM, 5),
                        Prefs.getInt(appContext, Prefs.IRR_JUMP_RATIO_PCT, 100) / 100.0,
                        Prefs.getInt(appContext, Prefs.IRR_REST_MAX_HR, 200),
                        Prefs.getInt(appContext, Prefs.IRR_SUSTAIN_WINDOWS, 2),
                        Prefs.getInt(appContext, Prefs.IRR_COOLDOWN_MINUTES, 10) * 60000L);
            } else {
                irrDetector = null;
            }
            Log.i(TAG, "推送配置已加载: 时段规则" + periods.size() + "条, 心律不齐检测="
                    + (irrDetector != null ? "开" : "关"));
        } catch (Exception e) {
            Log.e(TAG, "加载推送配置失败: " + e);
        }
    }

    /**
     * 解析时段规则 JSON 数组(对齐 _load_periods)。
     * 字段: enabled/start/end/max_hr/min_hr/duration/cooldown
     * (兼容 EXE 原生键名 max/min/sustain); max或min为0表示该项不检测;
     * duration为持续判定秒数; cooldown为冷却秒数(独立不共用)。
     * 单条规则非法时跳过该条, 不影响其余规则。
     */
    private List<PeriodRule> parsePeriods(String raw) {
        List<PeriodRule> out = new ArrayList<>();
        if (raw == null) return out;
        raw = raw.trim();
        if (raw.isEmpty()) return out;
        JSONArray arr;
        try {
            arr = new JSONArray(raw);
        } catch (Exception e) {
            Log.e(TAG, "解析时段规则失败, 忽略: " + e);
            return out;
        }
        for (int i = 0; i < arr.length(); i++) {
            JSONObject p = arr.optJSONObject(i);
            if (p == null) continue;   // 非对象元素跳过(对齐 py: not isinstance(dict))
            try {
                String s = normHhmm(p.optString("start", ""));
                String e = normHhmm(p.optString("end", ""));
                if (s.isEmpty() || e.isEmpty() || s.equals(e)) {
                    Log.w(TAG, "时段规则起止时间非法, 已跳过: " + p);
                    continue;
                }
                PeriodRule r = new PeriodRule();
                r.enabled = getBoolPy(p, "enabled", true);
                r.start = s;
                r.end = e;
                r.max = Math.max(0, getIntStrict(p, new String[]{"max_hr", "max"}, 0));
                r.min = Math.max(0, getIntStrict(p, new String[]{"min_hr", "min"}, 0));
                r.sustain = Math.max(1, getIntStrict(p, new String[]{"duration", "sustain"}, defDuration));
                r.cooldown = Math.max(0, getIntStrict(p, new String[]{"cooldown"}, defCooldown));
                out.add(r);
            } catch (Exception ex) {
                Log.w(TAG, "时段规则字段非法, 已跳过: " + p + " (" + ex + ")");
            }
        }
        return out;
    }

    /**
     * 依次取首个存在的键的整数值; 值必须为数字或数字字符串(对齐 py int() 语义,
     * 含 int("120")/int(120.9)=120 的宽解析), 否则抛异常由上层跳过该条规则。
     */
    private static int getIntStrict(JSONObject p, String[] keys, int def) throws Exception {
        for (String k : keys) {
            if (!p.has(k)) continue;
            Object v = p.get(k);
            if (v instanceof Number) return ((Number) v).intValue();
            if (v instanceof String) {
                try {
                    return Integer.parseInt(((String) v).trim());
                } catch (NumberFormatException nfe) {
                    throw new IllegalArgumentException("字段非数值: " + k);
                }
            }
            throw new IllegalArgumentException("字段类型非法: " + k);
        }
        return def;
    }

    /** 布尔取值, 对齐 py bool() 语义: null/0/空串为假, 非空串/非零为真 */
    private static boolean getBoolPy(JSONObject p, String key, boolean def) {
        if (!p.has(key)) return def;
        Object v = p.opt(key);
        if (v instanceof Boolean) return (Boolean) v;
        if (v == null) return false;
        if (v instanceof Number) return ((Number) v).doubleValue() != 0;
        if (v instanceof String) return !((String) v).isEmpty();
        return true;
    }

    /**
     * 时间规范化为 HH:MM; 非法输入返回空串(兼容 9:05 / 0905 / 905 等手改格式, 对齐 _norm_hhmm)
     */
    private static String normHhmm(String value) {
        String v = value == null ? "" : value.trim();
        if (v.isEmpty()) return "";
        int h, m;
        int colon = v.indexOf(':');
        if (colon >= 0) {
            String hs = v.substring(0, colon).trim();
            String ms = v.substring(colon + 1).trim();
            if (hs.isEmpty() || ms.isEmpty() || v.indexOf(':', colon + 1) >= 0) return "";
            try {
                h = Integer.parseInt(hs);
                m = Integer.parseInt(ms);
            } catch (NumberFormatException nfe) {
                return "";
            }
        } else if (v.length() == 3 || v.length() == 4) {
            // 905 -> 9:05, 0905 -> 09:05
            try {
                if (v.length() == 3) {
                    h = Integer.parseInt(v.substring(0, 1));
                    m = Integer.parseInt(v.substring(1));
                } else {
                    h = Integer.parseInt(v.substring(0, 2));
                    m = Integer.parseInt(v.substring(2));
                }
            } catch (NumberFormatException nfe) {
                return "";
            }
        } else {
            return "";
        }
        if (h < 0 || h > 23 || m < 0 || m > 59) return "";
        return String.format(Locale.US, "%02d:%02d", h, m);
    }

    private static int toMinutes(String hhmm) {
        return Integer.parseInt(hhmm.substring(0, 2)) * 60 + Integer.parseInt(hhmm.substring(3, 5));
    }

    // ==================== 高/低心率告警(对齐 check_heart_rate / _active_rule) ====================

    /**
     * 生效规则: 首个命中的时段规则, 否则默认规则。
     * 时段为 [起始, 结束) 半开区间(含起始整分, 不含结束整分), 支持跨零点。
     */
    private Rule activeRule() {
        Calendar cal = Calendar.getInstance();
        int t = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE);
        for (int i = 0; i < periods.size(); i++) {
            PeriodRule p = periods.get(i);
            if (!p.enabled) continue;
            int s = toMinutes(p.start);
            int e = toMinutes(p.end);
            if ((s <= e && s <= t && t < e) || (s > e && (t >= s || t < e))) {
                Rule r = new Rule();
                r.tag = "p" + i;
                r.label = "[" + p.start + "-" + p.end + "]";
                r.max = p.max;
                r.min = p.min;
                r.sustain = p.sustain;
                r.cooldown = p.cooldown;
                return r;
            }
        }
        Rule r = new Rule();
        r.tag = "global";
        r.label = "";
        r.max = defMaxHr;
        r.min = defMinHr;
        r.sustain = defDuration;
        r.cooldown = defCooldown;
        return r;
    }

    /** 心率检查入口(对齐 py check_heart_rate): hr<=0 仅走心律不齐清窗路径 */
    private void checkHeartRate(int hr) {
        if (hr <= 0) {
            // 断连/无效值: 心律不齐检测器需收到无效值以清空窗口
            checkIrregularity(hr);
            return;
        }
        long now = SystemClock.elapsedRealtime();
        Rule rule = activeRule();
        checkHighLow(hr, rule, true, now);   // 过高
        checkHighLow(hr, rule, false, now);  // 过低
        checkIrregularity(hr);
    }

    /**
     * 单方向判定: 各类别(规则×过高/过低)独立计时持续判定与冷却, 互不共CD。
     * 持续判定: 连续超过 sustain 秒才推; 冷却期内不重复推;
     * 恢复正常即清空该类别计时, 新一轮异常从零重新判定持续时长
     * (对齐 EXE 修复过的 BUG: 新一轮异常必须重新计时)。
     */
    private void checkHighLow(int hr, Rule rule, boolean high, long now) {
        int limit = high ? rule.max : rule.min;
        boolean exceeded = limit > 0 && (high ? hr > limit : hr < limit);
        String key = rule.tag + ":" + (high ? "high" : "low");
        if (!exceeded) {
            hrState.remove(key);
            return;
        }
        HrState st = hrState.get(key);
        if (st == null) {
            st = new HrState();
            hrState.put(key, st);
        }
        if (st.since == null) {
            st.since = now;   // 首个异常样本仅开始计时
        } else if (now - st.since >= rule.sustain * 1000L
                && now - st.lastAlert >= rule.cooldown * 1000L) {
            st.lastAlert = now;
            emit("心率告警", rule.label + "心率" + (high ? "过高" : "过低")
                    + ": " + hr + "次/分, 已持续" + rule.sustain + "秒",
                    high ? KIND_HIGH : KIND_LOW);
        }
        // 持续异常期间 since 保持, 冷却到期后自动再次告警
    }

    // ==================== 疑似心律不齐(对齐 _check_irregularity) ====================

    /** 疑似心律不齐检测(独立启停); 判定与冷却在检测器内部, 指标由检测器返回 */
    private void checkIrregularity(int hr) {
        IrrDetector det = irrDetector;
        if (det == null) return;
        Metrics m = det.check(hr, SystemClock.elapsedRealtime());
        if (m == null) return;
        emit("疑似心律不齐",
                String.format(Locale.US,
                        "静息心率无序波动: 平均%.0f次/分, 波动±%.1f, 大幅跳变占比%.0f%%。"
                                + "此为筛查提示非医学诊断, 建议静息复测或就医确认",
                        m.mean, m.sd, m.jumpRatio * 100),
                KIND_IRREGULAR);
    }

    // ============ 设备失联/恢复(对齐 watch_device_connection + notify_device_lost/back) ============

    private void onBusEvent(int hr, String status) {
        boolean connected = "connected".equals(status);
        // 断连/恢复提醒: 状态边沿触发, 无独立冷却计时
        // (对齐 EXE: 2秒轮询边沿检测, 连接状态不变不重复推送)
        if (lastConnected == null) {
            lastConnected = connected;   // 首个状态事件仅记录初始状态, 不推送
        } else if (connected != lastConnected) {
            lastConnected = connected;
            if (connected) {
                notifyDeviceBack();
            } else {
                notifyDeviceLost();
            }
        }
        checkHeartRate(hr);
    }

    /**
     * 设备断连提醒; 同时清空心律不齐窗口防脏数据
     * (对齐 EXE "hr≤0 仍转发给 _check_irregularity 触发 reset")
     */
    private void notifyDeviceLost() {
        IrrDetector det = irrDetector;
        if (det != null) det.reset();
        String name = deviceName();
        emit("设备断开", "蓝牙设备" + (name.isEmpty() ? "" : " " + name) + "已断开连接",
                KIND_DEV_LOST);
    }

    /** 设备重连成功提醒 */
    private void notifyDeviceBack() {
        String name = deviceName();
        emit("设备恢复", "蓝牙设备" + (name.isEmpty() ? "" : " " + name) + "已重新连接",
                KIND_DEV_BACK);
    }

    /** 设备名: 优先 HeartBus, 退回 Prefs(对齐 EXE 取最后连接设备名) */
    private String deviceName() {
        String n = HeartBus.get().getDeviceName();
        if (n == null || n.isEmpty()) n = Prefs.getStr(appContext, Prefs.DEV_NAME, "");
        return n == null ? "" : n;
    }

    // ==================== 推送出口 ====================

    /** 推送发出: 一律回调 AlarmListener, 网络由外部负责; 同一次检测可触发多条 */
    private void emit(String title, String body, int kind) {
        Log.i(TAG, "告警[" + kind + "]: [" + title + "] " + body);
        recordAlarm(title, body, kind);   // 追加推送记录(设置页"推送记录"数据源), 不影响原有行为
        AlarmListener l = listener;
        if (l == null) return;   // 未接渠道时仅记日志
        try {
            l.onAlarm(title, body, kind);
        } catch (Exception e) {
            Log.e(TAG, "告警回调异常: " + e);
        }
    }

    // ==================== 推送记录(内存环形缓冲, 供设置页 RecordsActivity 读取) ====================

    /** 单条推送记录(内存历史, 重启后清空) */
    public static final class AlarmRecord {
        public final long timestamp;   // 告警触发时刻(系统墙钟毫秒)
        public final String title;
        public final String body;
        public final int kind;         // KIND_HIGH/LOW/IRREGULAR/DEV_LOST/DEV_BACK

        AlarmRecord(long timestamp, String title, String body, int kind) {
            this.timestamp = timestamp;
            this.title = title == null ? "" : title;
            this.body = body == null ? "" : body;
            this.kind = kind;
        }
    }

    private static final int HISTORY_CAPACITY = 50;
    private static final ArrayDeque<AlarmRecord> sHistory = new ArrayDeque<>(HISTORY_CAPACITY);

    /** 告警触发点追加(emit 调用); 超容量淘汰最旧一条 */
    private static synchronized void recordAlarm(String title, String body, int kind) {
        if (sHistory.size() >= HISTORY_CAPACITY) sHistory.pollFirst();
        sHistory.addLast(new AlarmRecord(System.currentTimeMillis(), title, body, kind));
    }

    /** 读取最近推送记录(最新在前) */
    public static synchronized List<AlarmRecord> getHistory() {
        ArrayList<AlarmRecord> out = new ArrayList<>(sHistory.size());
        Iterator<AlarmRecord> it = sHistory.descendingIterator();
        while (it.hasNext()) out.add(it.next());
        return out;
    }

    /** 清空推送记录 */
    public static synchronized void clearHistory() {
        sHistory.clear();
    }

    // ==================== 数据结构 ====================

    /** 时段规则(解析并规范化后) */
    private static final class PeriodRule {
        boolean enabled;
        String start;    // "HH:MM"
        String end;      // "HH:MM"
        int max;         // 上限, 0=不检测
        int min;         // 下限, 0=不检测
        int sustain;     // 持续判定秒数
        int cooldown;    // 冷却秒数
    }

    /** 生效规则快照(时段规则或默认规则) */
    private static final class Rule {
        String tag;      // 状态键前缀: "pN" 或 "global"
        String label;    // 推送文案前缀: "[HH:MM-HH:MM]" 或 ""
        int max;
        int min;
        int sustain;
        int cooldown;
    }

    /** 单个告警类别(规则×方向)的计时状态(对齐 py {"since","last"}) */
    private static final class HrState {
        Long since = null;                    // 异常开始时刻, null=尚未计时
        long lastAlert = Long.MIN_VALUE / 2;  // 上次告警时刻(elapsedRealtime; 初始值保证首次可告警)
    }

    /** 心律不齐判定指标(供推送文案) */
    private static final class Metrics {
        final double mean;
        final double sd;
        final double jumpRatio;
        final double trend;

        Metrics(double mean, double sd, double jumpRatio, double trend) {
            this.mean = mean;
            this.sd = sd;
            this.jumpRatio = jumpRatio;
            this.trend = trend;
        }
    }

    /**
     * 疑似心律不齐检测器 —— 1:1 对标 irregularity_detector.py。
     * 基于整秒心率的筛查性算法: 房颤时逐秒心率无规律大幅跳动, 静息窦律逐秒平稳。
     * 判定条件(全部满足才视为异常窗口):
     * 1. 静息门控: 窗口平均心率 <= restMaxHr (运动时波动大属正常)
     * 2. 趋势门控: 前后半窗均值差 <= 5 bpm (排除运动上升/恢复下降的单调趋势)
     * 3. 无序性:   窗口心率标准差 > sdThreshold 且 大幅跳变(>=jumpBpm)占比 > jumpRatio
     * 防误报: 需连续 sustainWindows 个异常窗口才触发; 触发后进入冷却期;
     * 心率断流(<=0)即清空窗口, 避免断连重连后的脏数据误判。
     * 定位: 筛查提示, 非医学诊断, 无法区分房颤/窦性心律不齐/早搏。
     */
    private static final class IrrDetector {

        /** 趋势门控阈值: 前后半窗平均心率差超过此值视为趋势性变化(bpm) */
        static final double TREND_LIMIT_BPM = 5.0;

        final int windowSeconds;      // max(30, 配置值)
        final double sdThreshold;
        final int jumpBpm;
        final double jumpRatio;       // 占比阈值(0~1, 由配置百分数/100)
        final int restMaxHr;
        final int sustainWindows;     // max(1, 配置值)
        final long cooldownMs;

        private final int[] buf;      // 环形队列: 最近N秒心率(int数组, 免ArrayList拷贝)
        private int head = 0;         // 下一写入下标; 满窗时即最旧样本下标
        private int count = 0;        // 已填充样本数
        private int consecutive = 0;  // 连续异常窗口计数
        // 上次告警时刻(elapsedRealtime); 初始取极小值, 对齐 py -inf 保证启动后首次可告警
        private long lastAlert = Long.MIN_VALUE / 2;

        IrrDetector(int windowSeconds, double sdThreshold, int jumpBpm, double jumpRatio,
                    int restMaxHr, int sustainWindows, long cooldownMs) {
            this.windowSeconds = Math.max(30, windowSeconds);
            this.sdThreshold = sdThreshold;
            this.jumpBpm = jumpBpm;
            this.jumpRatio = jumpRatio;
            this.restMaxHr = restMaxHr;
            this.sustainWindows = Math.max(1, sustainWindows);
            this.cooldownMs = cooldownMs;
            this.buf = new int[this.windowSeconds];
        }

        /** 清空状态(断连/停止时调用): 窗口作废, 连续计数清零 */
        void reset() {
            head = 0;
            count = 0;
            consecutive = 0;
        }

        /**
         * 每秒喂入心率(1Hz 样本), 触发时返回告警指标, 否则返回 null。
         * nowElapsed 为 SystemClock.elapsedRealtime 毫秒(防系统时间跳变)。
         */
        Metrics check(int heartRate, long nowElapsed) {
            if (heartRate <= 0) {
                // 心率无效(断连/信号丢失): 窗口作废, 连续计数清零
                reset();
                return null;
            }
            buf[head] = heartRate;
            head = (head + 1) % windowSeconds;
            if (count < windowSeconds) count++;
            if (count < windowSeconds) return null;   // 未满窗不判定

            Metrics m = metrics();
            if (!isAbnormal(m)) {
                consecutive = 0;
                return null;
            }
            consecutive++;
            if (consecutive < sustainWindows) return null;
            if (nowElapsed - lastAlert < cooldownMs) return null;   // 冷却期内不重复推
            lastAlert = nowElapsed;
            return m;
        }

        /**
         * 窗口统计: 均值/标准差(总体)/大幅跳变占比/前后半窗趋势差。
         * 满窗时 head 即最旧样本下标, 按旧→新顺序遍历。
         */
        private Metrics metrics() {
            int n = count;
            long sum = 0;
            for (int i = 0; i < n; i++) sum += buf[i];
            double mean = (double) sum / n;
            double varSum = 0;
            for (int i = 0; i < n; i++) {
                double d = buf[i] - mean;
                varSum += d * d;
            }
            double sd = Math.sqrt(varSum / n);
            int jumps = 0;
            for (int i = 0; i < n - 1; i++) {
                int a = buf[(head + i) % windowSeconds];
                int b = buf[(head + i + 1) % windowSeconds];
                if (Math.abs(a - b) >= jumpBpm) jumps++;
            }
            double jumpRatioHit = n > 1 ? (double) jumps / (n - 1) : 0.0;
            // 趋势: 前后半窗均值差(新半窗 - 旧半窗)
            int half = n / 2;
            long sumFirst = 0;
            long sumSecond = 0;
            for (int i = 0; i < half; i++) sumFirst += buf[(head + i) % windowSeconds];
            for (int i = half; i < n; i++) sumSecond += buf[(head + i) % windowSeconds];
            double trend = Math.abs((double) sumSecond / (n - half) - (double) sumFirst / half);
            return new Metrics(mean, sd, jumpRatioHit, trend);
        }

        private boolean isAbnormal(Metrics m) {
            return m.mean <= restMaxHr
                    && m.trend <= TREND_LIMIT_BPM
                    && m.sd > sdThreshold
                    && m.jumpRatio > jumpRatio;
        }
    }
}
