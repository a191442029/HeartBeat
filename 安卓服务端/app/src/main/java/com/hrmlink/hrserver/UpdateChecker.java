package com.hrmlink.hrserver;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * 检查更新: GitHub releases latest → 匹配本应用APK → 与本地versionName比较 → 下载 → 跳系统安装器
 * 仓库: github.com/a191442029/HeartBeat, asset命名 HRHub-x.y.z.apk (接收端对应 HRBubble-*)
 * UI入口在设置页"检查更新"按钮; 全程无需存储权限(写应用外部私有目录)
 */
public final class UpdateChecker {

    private static final String API_URL =
            "https://api.github.com/repos/a191442029/HeartBeat/releases/latest";
    private static final String ASSET_PREFIX = "HRHub-";
    private static final OkHttpClient HTTP = new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)   // APK下载较慢, 放宽读超时
            .build();
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    /** 检查结果回调(主线程); error非空=检查失败, hasUpdate=false=已是最新 */
    public interface CheckCallback {
        void onResult(boolean hasUpdate, String newVersion, String apkUrl, String notes, String error);
    }

    /** 下载结果回调(主线程); error非空=失败 */
    public interface DownloadCallback {
        void onDone(File apk, String error);
    }

    /** 检查最新release(网络线程执行, 结果切主线程回调) */
    public static void check(final CheckCallback cb) {
        new Thread(() -> {
            boolean hasUpdate = false;
            String newVersion = null, apkUrl = null, notes = null, error = null;
            try {
                Request req = new Request.Builder().url(API_URL)
                        .header("Accept", "application/vnd.github+json")
                        .header("User-Agent", ASSET_PREFIX + "UpdateCheck")
                        .build();
                try (Response resp = HTTP.newCall(req).execute()) {
                    if (!resp.isSuccessful()) throw new Exception("HTTP " + resp.code());
                    JSONObject rel = new JSONObject(resp.body().string());
                    notes = rel.optString("body", "");
                    JSONArray assets = rel.optJSONArray("assets");
                    if (assets == null) throw new Exception("release 无附件");
                    for (int i = 0; i < assets.length(); i++) {
                        JSONObject a = assets.getJSONObject(i);
                        String name = a.optString("name", "");
                        if (name.startsWith(ASSET_PREFIX) && name.toLowerCase().endsWith(".apk")) {
                            apkUrl = a.optString("browser_download_url", "");
                            Matcher m = Pattern.compile(
                                    Pattern.quote(ASSET_PREFIX) + "([0-9]+(?:\\.[0-9]+)*)").matcher(name);
                            if (m.find()) newVersion = m.group(1);
                            break;
                        }
                    }
                    if (apkUrl == null) throw new Exception("release 中未找到 " + ASSET_PREFIX + "*.apk");
                    hasUpdate = isNewer(newVersion, BuildConfig.VERSION_NAME);
                }
            } catch (Exception e) {
                error = e.getMessage();
            }
            final boolean fHas = hasUpdate;
            final String fv = newVersion, fu = apkUrl, fn = notes, fe = error;
            MAIN.post(() -> cb.onResult(fHas, fv, fu, fn, fe));
        }).start();
    }

    /** 版本号比较: remote > local 返回true(逐段数字比较, 段数不齐补0) */
    static boolean isNewer(String remote, String local) {
        if (remote == null || local == null) return false;
        try {
            String[] r = remote.split("\\."), l = local.split("\\.");
            int n = Math.max(r.length, l.length);
            for (int i = 0; i < n; i++) {
                int ri = i < r.length ? Integer.parseInt(r[i]) : 0;
                int li = i < l.length ? Integer.parseInt(l[i]) : 0;
                if (ri != li) return ri > li;
            }
        } catch (NumberFormatException ignored) {
        }
        return false;
    }

    /** 下载APK到应用外部私有目录update/下(网络线程, 结果切主线程回调) */
    public static void download(final Context ctx, final String url,
                                final String version, final DownloadCallback cb) {
        new Thread(() -> {
            File dir = ctx.getExternalFilesDir("update");
            if (dir == null) dir = new File(ctx.getFilesDir(), "update");
            if (!dir.exists()) dir.mkdirs();
            final File out = new File(dir, ASSET_PREFIX + version + ".apk");
            String err = null;
            try {
                Request req = new Request.Builder().url(url)
                        .header("User-Agent", ASSET_PREFIX + "Update").build();
                try (Response resp = HTTP.newCall(req).execute()) {
                    if (!resp.isSuccessful()) throw new Exception("HTTP " + resp.code());
                    InputStream in = resp.body().byteStream();
                    FileOutputStream fos = new FileOutputStream(out);
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) fos.write(buf, 0, n);
                    fos.close();
                }
            } catch (Exception e) {
                err = e.getMessage();
                if (out.exists()) out.delete();
            }
            final String fe = err;
            MAIN.post(() -> cb.onDone(fe == null ? out : null, fe));
        }).start();
    }

    /** 跳系统安装器(FileProvider共享APK; 未授权未知来源时系统自动引导授权) */
    public static void install(Context ctx, File apk) {
        Uri uri = androidx.core.content.FileProvider.getUriForFile(
                ctx, ctx.getPackageName() + ".fileprovider", apk);
        Intent i = new Intent(Intent.ACTION_VIEW);
        i.setDataAndType(uri, "application/vnd.android.package-archive");
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(i);
    }
}
