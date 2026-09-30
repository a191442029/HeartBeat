package com.hrmlink.hrserver;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 开机自启: 拉起监测前台服务(长期通电常驻场景, 对齐 EXE 开机自启语义)。
 * 需 Manifest RECEIVE_BOOT_COMPLETED 权限; 配合电池优化白名单引导防止被杀。
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            HeartRateService.start(context);
        }
    }
}
