package com.hrmlink.hrserver;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;

/**
 * 多渠道手机推送模块（对标 EXE push_notifier.py）
 *
 * 三渠道, 可同时启用, URL/参数拼接严格对齐 py:
 * - MeoW (鸿蒙): GET https://api.chuckfang.com/{昵称}/{title}/{msg}
 * - Bark  (iOS): GET {server}/{key}/{title}/{msg}?level=&sound=&group=  默认 https://api.day.app
 * - ntfy  (安卓/全平台): GET {server}/{topic}/publish?title=&message=[&priority=][&tags=]
 *   默认 https://ntfy.sh, token 时加 Authorization: Bearer 头
 *
 * 设计(对齐 py):
 * - 事件驱动: 仅在告警/断连等关键事件时推送, 心率正常不产生任何请求
 * - 多目标: 昵称/Key/主题用逗号/分号/空白分隔, 逐台推送, 任一成功即成功
 * - 网络隔离: 全部 OkHttp enqueue 异步执行, 失败仅记日志, 绝不阻塞调用方
 * - 回调统一切主线程(无 Activity, 用主线程 Handler post)
 */
public final class PushChannels {

    public interface TestCallback {
        void onResult(String channel, boolean ok, String message);
    }

    /** 单请求结果回调(内部用, 在 OkHttp 工作线程) */
    private interface ResultCb {
        void onResult(boolean ok, String note);
    }

    private static final String TAG = "HRServer";

    // 对齐 push_notifier.py 三个渠道默认API
    private static final String MEOW_API = "https://api.chuckfang.com";
    private static final String BARK_API = "https://api.day.app";
    private static final String NTFY_API = "https://ntfy.sh";
    private static final String[] BARK_LEVELS = {"active", "timeSensitive", "passive", "critical"};

    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static volatile OkHttpClient sHttp;

    private PushChannels() {
    }

    /** 全项目共享单例 OkHttpClient(1GB 设备省资源, InfluxWriter 等模块可复用) */
    public static OkHttpClient httpClient() {
        if (sHttp == null) {
            synchronized (PushChannels.class) {
                if (sHttp == null) {
                    sHttp = new OkHttpClient.Builder()
                            .connectTimeout(10, TimeUnit.SECONDS)
                            .readTimeout(15, TimeUnit.SECONDS)
                            .callTimeout(20, TimeUnit.SECONDS)
                            .build();
                }
            }
        }
        return sHttp;
    }

    /** 向所有已启用渠道广播推送(全异步, 无回调, 失败仅记日志), 对齐 EXE _push_all */
    public static void push(Context c, String title, String body) {
        Context app = c.getApplicationContext();
        // 渠道顺序对齐 EXE NotifierManager.load_config: MeoW → Bark → ntfy
        if (Prefs.getBool(app, Prefs.MEOW_ENABLED, false)) pushMeow(app, title, body, null);
        if (Prefs.getBool(app, Prefs.BARK_ENABLED, false)) pushBark(app, title, body, null);
        if (Prefs.getBool(app, Prefs.NTFY_ENABLED, false)) pushNtfy(app, title, body, null);
    }

    /** 单渠道测试推送: channel = "bark" | "ntfy" | "meow", 结果回调在主线程 */
    public static void test(Context c, String channel, TestCallback cb) {
        Context app = c.getApplicationContext();
        String title = "测试推送";
        String msg = "HRHub服务端测试消息: 收到即表示该推送渠道配置正常";
        if ("meow".equals(channel)) {
            pushMeow(app, title, msg, cb);
        } else if ("bark".equals(channel)) {
            pushBark(app, title, msg, cb);
        } else if ("ntfy".equals(channel)) {
            pushNtfy(app, title, msg, cb);
        } else {
            deliver(channel, cb, false, "未知推送渠道: " + channel);
        }
    }

    // ---- 各渠道构建与派发 ----

    /** MeoW 渠道(鸿蒙): GET {MEOW_API}/{昵称}/{title}/{msg}, 昵称必填且不能含斜杠 */
    private static void pushMeow(Context app, final String title, final String msg, final TestCallback cb) {
        List<String> nicks = parseTargets(Prefs.getStr(app, Prefs.MEOW_NICKNAME, ""));
        if (nicks.isEmpty()) {
            failFast("MeoW", cb, "未填写昵称");
            return;
        }
        for (String n : nicks) {
            if (n.contains("/")) {
                failFast("MeoW", cb, "昵称不能包含斜杠/, 请只填写MeoW App中注册的昵称");
                return;
            }
        }
        List<Request> reqs = new ArrayList<>();
        try {
            for (String n : nicks) {
                reqs.add(new Request.Builder()
                        .url(MEOW_API + "/" + enc(n) + "/" + enc(title) + "/" + enc(msg))
                        .get().build());
            }
        } catch (IllegalArgumentException e) {
            failFast("MeoW", cb, "URL构建失败: " + e.getMessage());
            return;
        }
        dispatch("MeoW", reqs, title, msg, cb);
    }

    /** Bark 渠道(iOS): GET {server}/{key}/{title}/{msg}[?level=&sound=&group=], 多Key逐台 */
    private static void pushBark(Context app, final String title, final String msg, final TestCallback cb) {
        List<String> keys = parseTargets(Prefs.getStr(app, Prefs.BARK_DEVICE_KEY, ""));
        if (keys.isEmpty()) {
            failFast("Bark", cb, "未填写Bark推送Key");
            return;
        }
        String server = normServer(Prefs.getStr(app, Prefs.BARK_SERVER, ""), BARK_API);
        String level = Prefs.getStr(app, Prefs.BARK_LEVEL, "").trim().toLowerCase(Locale.US);
        String sound = Prefs.getStr(app, Prefs.BARK_SOUND, "").trim();
        String group = Prefs.getStr(app, Prefs.BARK_GROUP, "").trim();
        boolean levelOk = false;
        for (String l : BARK_LEVELS) {
            if (l.equals(level)) {
                levelOk = true;
                break;
            }
        }
        List<Request> reqs = new ArrayList<>();
        try {
            for (String key : keys) {
                StringBuilder url = new StringBuilder(server).append("/")
                        .append(enc(key)).append("/")
                        .append(enc(title)).append("/")
                        .append(enc(msg));
                // 缺省参数不拼(对齐 py BarkPush._push_key)
                List<String> params = new ArrayList<>();
                if (levelOk) params.add("level=" + level);
                if (!sound.isEmpty()) params.add("sound=" + enc(sound));
                if (!group.isEmpty()) params.add("group=" + enc(group));
                if (!params.isEmpty()) url.append('?').append(join("&", params));
                reqs.add(new Request.Builder().url(url.toString()).get().build());
            }
        } catch (IllegalArgumentException e) {
            failFast("Bark", cb, "服务器地址或参数非法: " + e.getMessage());
            return;
        }
        dispatch("Bark", reqs, title, msg, cb);
    }

    /** ntfy 渠道(安卓/全平台): GET {server}/{topic}/publish?title=&message=[&priority=][&tags=], token 走 Bearer 头 */
    private static void pushNtfy(Context app, final String title, final String msg, final TestCallback cb) {
        List<String> topics = new ArrayList<>();
        for (String t : parseTargets(Prefs.getStr(app, Prefs.NTFY_TOPIC, ""))) {
            t = stripSlashes(t);   // 对齐 py: x.strip("/") 后过滤空串
            if (!t.isEmpty()) topics.add(t);
        }
        if (topics.isEmpty()) {
            failFast("ntfy", cb, "未填写ntfy订阅主题");
            return;
        }
        String server = normServer(Prefs.getStr(app, Prefs.NTFY_SERVER, ""), NTFY_API);
        int priority = Prefs.getInt(app, Prefs.NTFY_PRIORITY, 0);   // 0=不传
        String tags = Prefs.getStr(app, Prefs.NTFY_TAGS, "").trim();
        String token = Prefs.getStr(app, Prefs.NTFY_TOKEN, "").trim();
        List<Request> reqs = new ArrayList<>();
        try {
            for (String t : topics) {
                StringBuilder url = new StringBuilder(server).append("/")
                        .append(enc(t)).append("/publish?title=")
                        .append(enc(title)).append("&message=").append(enc(msg));
                if (priority >= 1 && priority <= 5) url.append("&priority=").append(priority);
                if (!tags.isEmpty()) url.append("&tags=").append(enc(tags));
                Request.Builder rb = new Request.Builder().url(url.toString()).get();
                if (!token.isEmpty()) rb.header("Authorization", "Bearer " + token);
                reqs.add(rb.build());
            }
        } catch (IllegalArgumentException e) {
            failFast("ntfy", cb, "服务器地址或参数非法: " + e.getMessage());
            return;
        }
        dispatch("ntfy", reqs, title, msg, cb);
    }

    /**
     * 多目标派发(对齐 _push_multi): 单目标直发; 多目标全部发出,
     * 任一成功即成功, 汇总说明"设备N:结果"。cb 为 null 时仅逐台记日志。
     */
    private static void dispatch(final String channel, List<Request> reqs,
                                 final String title, final String msg, final TestCallback cb) {
        if (reqs.isEmpty()) return;
        if (reqs.size() == 1) {
            httpGetAsync(reqs.get(0), new ResultCb() {
                @Override
                public void onResult(boolean ok, String note) {
                    logResult(channel, ok, title, msg, note);
                    deliver(channel, cb, ok, note);
                }
            });
            return;
        }
        final boolean[] okAny = {false};
        final List<String> notes = Collections.synchronizedList(new ArrayList<String>());
        final AtomicInteger done = new AtomicInteger(0);
        for (int i = 0; i < reqs.size(); i++) {
            final Request req = reqs.get(i);
            final int no = i + 1;
            httpGetAsync(req, new ResultCb() {
                @Override
                public void onResult(boolean ok, String note) {
                    logResult(channel, ok, title, msg, note);
                    if (ok) okAny[0] = true;
                    notes.add("设备" + no + ":" + note);
                    if (done.incrementAndGet() == reqs.size()) {
                        deliver(channel, cb, okAny[0], join("; ", notes));
                    }
                }
            });
        }
    }

    // ---- HTTP 执行与响应判定(对齐 _http_get_json / _http_error_note) ----

    private static void httpGetAsync(Request req, final ResultCb cb) {
        httpClient().newCall(req).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                Log.e(TAG, "推送请求异常: " + e);
                cb.onResult(false, "网络请求失败: " + e.getMessage());
            }

            @Override
            public void onResponse(Call call, Response resp) {
                boolean ok;
                String note;
                String body = readBody(resp);
                if (!resp.isSuccessful()) {   // 4xx/5xx: HTTP 层错误
                    ok = false;
                    note = httpErrorNote(resp.code(), body);
                } else {
                    // 解析JSON响应: MeoW {"status":200} / Bark {"code":200} / ntfy {"id":...}
                    try {
                        JSONObject d = new JSONObject(body);
                        int code = d.has("status") ? d.optInt("status", 0)
                                : (d.has("code") ? d.optInt("code", 0) : 0);
                        if (code == 200 || (code == 0 && d.has("id"))) {
                            ok = true;
                            note = "推送成功";
                        } else if (code == 404) {
                            ok = false;
                            note = "目标不存在(昵称/Key/主题未注册或服务器地址错误)";
                        } else if (code == 429) {
                            ok = false;
                            note = "发送频率超限, 请稍后再试";
                        } else {
                            ok = false;
                            note = "服务端返回: " + d;
                        }
                    } catch (JSONException e) {
                        Log.e(TAG, "推送响应解析失败: " + e);
                        ok = false;
                        note = "网络请求失败: 响应JSON解析失败";
                    }
                }
                cb.onResult(ok, note);
            }
        });
    }

    /** HTTP 层错误说明(状态码提示 + 服务端响应体摘录, 对齐 py _http_error_note) */
    private static String httpErrorNote(int code, String body) {
        body = body == null ? "" : body.trim();
        if (body.length() > 200) body = body.substring(0, 200);
        Log.e(TAG, "推送HTTP " + code + ": " + body);
        String hint;
        switch (code) {
            case 400:
                hint = "请求被服务端拒绝, 请检查参数(昵称/Key/did/服务器地址)";
                break;
            case 401:
                hint = "鉴权失败, 请检查访问令牌";
                break;
            case 403:
                hint = "内容被拒绝或当前IP受限";
                break;
            case 404:
                hint = "接口路径不存在, 请检查服务器地址";
                break;
            case 429:
                hint = "发送频率超限, 请稍后再试";
                break;
            case 500:
                hint = "服务器内部错误, 请稍后再试";
                break;
            default:
                hint = "";
                break;
        }
        String note = "HTTP " + code + (hint.isEmpty() ? "" : ": " + hint);
        if (!body.isEmpty()) note += ", 服务端说明: " + body;
        return note;
    }

    private static String readBody(Response resp) {
        try {
            ResponseBody b = resp.body();
            if (b == null) return "";
            return b.string();
        } catch (Exception e) {
            return "";
        } finally {
            resp.close();
        }
    }

    // ---- 字符串工具(对齐 push_notifier.py 各私有函数) ----

    /** 解析多设备标识(昵称/Key/主题): 逗号/分号/空白分隔, 去重保序(对齐 _parse_targets) */
    private static List<String> parseTargets(String raw) {
        List<String> out = new ArrayList<>();
        if (raw == null) return out;
        Set<String> seen = new LinkedHashSet<>();
        for (String t : raw.trim().split("[,，;；\\s]+")) {
            if (!t.isEmpty() && seen.add(t)) out.add(t);
        }
        return out;
    }

    /** 规范服务器地址: 空用默认; 缺协议前缀时自动补 https://(对齐 _norm_server) */
    private static String normServer(String server, String def) {
        String s = server == null ? "" : server.trim();
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        if (s.isEmpty()) return def;
        if (!s.contains("://")) s = "https://" + s;
        return s;
    }

    /** 去除首尾斜杠(对齐 Python str.strip("/")) */
    private static String stripSlashes(String t) {
        int b = 0, e = t.length();
        while (b < e && t.charAt(b) == '/') b++;
        while (e > b && t.charAt(e - 1) == '/') e--;
        return t.substring(b, e);
    }

    /**
     * URL 路径段/参数值编码, 对齐 py urllib.parse.quote(safe=""):
     * 保留 [A-Za-z0-9_.~-], 其余按 UTF-8 百分号编码(空格为 %20 而非 +, 路径段正确)。
     */
    private static String enc(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            if ((b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') || (b >= '0' && b <= '9')
                    || b == '_' || b == '.' || b == '-' || b == '~') {
                sb.append((char) b);
            } else {
                // b & 0xFF: 防止负 byte 符号扩展导致 %02X 输出 8 位(如 FFFFFF80)
                sb.append('%').append(String.format(Locale.US, "%02X", b & 0xFF));
            }
        }
        return sb.toString();
    }

    private static String join(String sep, List<String> items) {
        return String.join(sep, new ArrayList<>(items));   // 拷贝后拼接避免并发修改
    }

    // ---- 结果处理 ----

    /** 结果记日志(对齐 py worker: 成功 info / 失败 warning) */
    private static void logResult(String channel, boolean ok, String title, String msg, String note) {
        if (ok) {
            Log.i(TAG, "[" + channel + "]推送成功: [" + title + "] " + msg);
        } else {
            Log.w(TAG, "[" + channel + "]推送失败: " + note);
        }
    }

    /** 前置校验失败即返回(不发请求) */
    private static void failFast(String channel, TestCallback cb, String note) {
        Log.w(TAG, "[" + channel + "]推送失败: " + note);
        deliver(channel, cb, false, note);
    }

    /** 测试回调切主线程派发(无 Activity, 用主线程 Handler) */
    private static void deliver(final String channel, final TestCallback cb, final boolean ok, final String msg) {
        if (cb == null) return;
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                cb.onResult(channel, ok, msg);
            }
        });
    }
}
