package com.hrmlink.hrserver;

import android.content.Context;
import android.os.storage.StorageManager;
import android.os.Environment;

import java.io.File;

/**
 * 存档根目录解析(§4.3.1 拍板: 三类存档逐项选 U盘/板载):
 * - 选 U 盘但未挂载 → 自动回退板载(仅对新文件生效, 旧文件不迁移)
 * - U 盘判定: getExternalFilesDirs 的非主存储卷(OTG 挂载后出现)且可写
 * 用法: 写文件前按各自 Prefs 键取根目录, 目录不存在时 mkdirs。
 */
public final class StorageUtil {

    private StorageUtil() {
    }

    /** 存档根目录: pref 传 Prefs.CLIP_STORAGE/CSV_STORAGE/PUSHLOG_STORAGE */
    public static File archiveRoot(Context c, String prefKey, String subdir) {
        boolean wantUsb = "usb".equals(Prefs.getStr(c, prefKey, "internal"));
        File usb = usbRoot(c);
        File base = (wantUsb && usb != null) ? usb : new File(c.getFilesDir(), "archive");
        File dir = new File(base, subdir);
        if (!dir.isDirectory()) dir.mkdirs();
        return dir;
    }

    /** U 盘挂载根(OTG/外置卷; 未挂载或只读返回 null) */
    public static File usbRoot(Context c) {
        try {
            File[] dirs = c.getExternalFilesDirs(null);
            for (File d : dirs) {
                if (d == null) continue;
                if (Environment.isExternalStorageRemovable(d) && d.canWrite()) {
                    return d;
                }
            }
        } catch (Exception ignore) {
        }
        return null;
    }

    /** U 盘状态文案(设置→存储组状态行) */
    public static String usbStateText(Context c) {
        File usb = usbRoot(c);
        return usb != null ? "U 盘: 已挂载 → " + usb.getAbsolutePath()
                : "U 盘: 未挂载 (存档将使用板载存储)";
    }

    /** 清理过期文件(mtime 早于 beforeMs), 返回删除数——CSV 保留期用 */
    public static int purgeOlderThan(File dir, long beforeMs, String suffix) {
        int n = 0;
        File[] fs = dir.listFiles();
        if (fs == null) return 0;
        for (File f : fs) {
            if (f.isFile() && f.getName().toLowerCase(java.util.Locale.US).endsWith(suffix)
                    && f.lastModified() < beforeMs && f.delete()) {
                n++;
            }
        }
        return n;
    }
}
