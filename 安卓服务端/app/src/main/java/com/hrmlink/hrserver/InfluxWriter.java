package com.hrmlink.hrserver;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.net.URLEncoder;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

/**
 * InfluxDB 输出模块（对标 EXE influxdb_writer.py）
 *
 * - 写入: 监听 HeartBus, hr>0 时逐点写行协议(对齐 EXE batch_size=1),
 *   失败仅记日志, 绝不影响心率采集主流程。
 * - 测试: 只读探活 /ping(期望204) + /api/v2/orgs 校验 token/org,
 *   不向正式 bucket 写测试点污染数据(对齐 EXE test_connection)。
 * - token 经 Authorization: Token 头携带(与 py InfluxDBClient 一致),
 *   org/bucket 等 query 参数做 URL 编码防特殊字符。
 */
public class InfluxWriter {

    public interface TestCallback {
        void onResult(boolean ok, String message);
    }

    private static final String TAG = "HRServer";
    private static final MediaType TEXT_PLAIN = MediaType.parse("text/plain; charset=utf-8");
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private final Context app;
    private final boolean enabled;   // [InfluxDB] 总开关
    private final String url;        // 如 http://192.168.1.100:8086 (已去尾部斜杠)
    private final String token;
    private final String org;
    private final String bucket;

    private HeartBus.Listener listener;

    /** 读 Prefs [InfluxDB] 配置(快照式: 构造后配置变更需重建实例) */
    public InfluxWriter(Context context) {
        app = context.getApplicationContext();
        enabled = Prefs.getBool(app, Prefs.INFLUX_ENABLED, false);
        String u = Prefs.getStr(app, Prefs.INFLUX_URL, "").trim();
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        url = u;
        token = Prefs.getStr(app, Prefs.INFLUX_TOKEN, "").trim();
        org = Prefs.getStr(app, Prefs.INFLUX_ORG, "").trim();
        bucket = Prefs.getStr(app, Prefs.INFLUX_BUCKET, "").trim();
    }

    /** 注册 HeartBus 监听(回调在主线程, 写入经 OkHttp 异步不阻塞); enabled=false 时不写 */
    public void start() {
        if (listener != null) return;
        listener = new HeartBus.Listener() {
            @Override
            public void onHeartRate(int hr, String ts, String status) {
                // 对齐 EXE: 仅真实心率落库, 断连推送(hr=0)不写
                if (hr > 0) writeHeartRate(hr);
            }
        };
        HeartBus.get().addListener(listener);
    }

    /** 注销监听 */
    public void stop() {
        if (listener != null) {
            HeartBus.get().removeListener(listener);
            listener = null;
        }
    }

    /** 测试连接(全异步, 回调切主线程): /ping 探活 → /api/v2/orgs 校验 token/org(只读) */
    public void test(final TestCallback cb) {
        if (url.isEmpty()) {
            deliver(cb, false, "未配置InfluxDB服务器地址");
            return;
        }
        // 步骤1: 服务可达性检查 GET /ping (InfluxDB v2 成功返回204, 无响应体)
        Request ping = new Request.Builder().url(url + "/ping").get().build();
        PushChannels.httpClient().newCall(ping).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                Log.e(TAG, "InfluxDB服务不可达(/ping 失败): " + e);
                deliver(cb, false, "InfluxDB服务不可达(/ping 失败): " + e.getMessage());
            }

            @Override
            public void onResponse(Call call, Response resp) {
                int code = resp.code();
                String body = readBody(resp);
                if (code < 200 || code >= 300) {
                    Log.e(TAG, "InfluxDB服务不可达(/ping 失败): HTTP " + code + " " + body);
                    deliver(cb, false, "InfluxDB服务不可达(/ping 失败): HTTP " + code);
                    return;
                }
                checkOrg(cb);
            }
        });
    }

    /** 步骤2: token与组织有效性检查(只读API), 对齐 EXE find_organizations(org=...) */
    private void checkOrg(final TestCallback cb) {
        if (org.isEmpty()) {
            deliver(cb, false, "未配置InfluxDB组织");
            return;
        }
        Request req;
        try {
            req = new Request.Builder()
                    .url(url + "/api/v2/orgs?org=" + encQuery(org))
                    .header("Authorization", "Token " + token)
                    .get()
                    .build();
        } catch (IllegalArgumentException e) {
            deliver(cb, false, "InfluxDB服务器地址非法: " + e.getMessage());
            return;
        }
        PushChannels.httpClient().newCall(req).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                Log.e(TAG, "InfluxDB组织校验请求失败: " + e);
                deliver(cb, false, "InfluxDB组织校验请求失败: " + e.getMessage());
            }

            @Override
            public void onResponse(Call call, Response resp) {
                int code = resp.code();
                String body = readBody(resp);
                if (code != 200) {
                    Log.e(TAG, "InfluxDB组织不存在或token无权限: " + org + " (HTTP " + code + " " + body + ")");
                    deliver(cb, false, "InfluxDB组织不存在或token无权限: " + org + " (HTTP " + code + ")");
                    return;
                }
                try {
                    JSONObject d = new JSONObject(body);
                    JSONArray orgs = d.optJSONArray("orgs");
                    if (orgs == null || orgs.length() == 0) {
                        deliver(cb, false, "InfluxDB组织不存在或token无权限: " + org);
                        return;
                    }
                    Log.i(TAG, "InfluxDB连接测试成功(服务可达, token/org有效)");
                    deliver(cb, true, "InfluxDB连接测试成功(服务可达, token/org有效)");
                } catch (JSONException e) {
                    Log.e(TAG, "InfluxDB连接测试失败: " + e);
                    deliver(cb, false, "InfluxDB连接测试失败: 响应解析异常");
                }
            }
        });
    }

    /**
     * 写入单点心率(OkHttp 异步, 失败仅记日志):
     * 行协议 heart_rate,device=heart_rate_monitor value={hr}i {epoch秒}
     * POST {url}/api/v2/write?org=&bucket=&precision=s
     */
    private void writeHeartRate(int hr) {
        if (!enabled || url.isEmpty() || token.isEmpty() || org.isEmpty() || bucket.isEmpty()) return;
        long epochSec = System.currentTimeMillis() / 1000;
        // measurement,tag field=整数值i 时间戳(秒精度) — 对齐 EXE
        // Point("heart_rate").tag("device","heart_rate_monitor").field("value", hr)
        String line = "heart_rate,device=heart_rate_monitor value=" + hr + "i " + epochSec;
        Request req;
        try {
            req = new Request.Builder()
                    .url(url + "/api/v2/write?org=" + encQuery(org)
                            + "&bucket=" + encQuery(bucket) + "&precision=s")
                    .header("Authorization", "Token " + token)
                    .post(RequestBody.create(TEXT_PLAIN, line))
                    .build();
        } catch (IllegalArgumentException e) {
            Log.w(TAG, "写入心率数据到InfluxDB失败: 服务器地址非法 " + e.getMessage());
            return;
        }
        PushChannels.httpClient().newCall(req).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                Log.w(TAG, "写入心率数据到InfluxDB失败: " + e);
            }

            @Override
            public void onResponse(Call call, Response resp) {
                try {
                    if (resp.isSuccessful()) {
                        Log.d(TAG, "已写入心率数据到InfluxDB: " + hr + " BPM");
                    } else {
                        Log.w(TAG, "写入心率数据到InfluxDB失败: HTTP "
                                + resp.code() + " " + readBody(resp));
                    }
                } finally {
                    resp.close();
                }
            }
        });
    }

    private void deliver(final TestCallback cb, final boolean ok, final String message) {
        if (cb == null) return;
        MAIN.post(new Runnable() {
            @Override
            public void run() {
                cb.onResult(ok, message);
            }
        });
    }

    /** query 参数 URL 编码(token 含特殊字符时 query 侧同样安全; token 本体走 Authorization 头) */
    private static String encQuery(String v) {
        try {
            return URLEncoder.encode(v == null ? "" : v, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            return v == null ? "" : v;   // UTF-8 必然支持, 不会走到
        }
    }

    /** 读响应体并截断(错误说明展示用) */
    private static String readBody(Response resp) {
        try {
            ResponseBody b = resp.body();
            if (b == null) return "";
            String s = b.string().trim();
            return s.length() > 200 ? s.substring(0, 200) : s;
        } catch (Exception e) {
            return "";
        }
    }
}
