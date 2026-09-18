package com.hrmlink.hrserver;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;

import java.util.UUID;

/**
 * BLE 采集模块（对标 EXE Blegetheartbeat.py 的角色）
 *
 * 职责: 扫描(设备管理页用) / 连接手环 / 订阅 0x2A37 心率通知 / 解析 / 自动重连
 * 数据一律经 HeartBus 发布, 本模块不关心任何下游消费者。
 *
 * 解析规则(严格对齐 EXE _parse_heart_rate):
 *   len<2 丢弃; flags=data[0]; flags&0x01==1 → uint16 小端(需 len>=3), 否则 uint8。
 *
 * 重连策略: 意外断开后退避重连 5/10/20/30 秒封顶, 连接成功清零;
 *           手动 disconnect() 不重连。另带 15 秒无数据看门狗(手环静默掉线恢复)。
 */
public class BleManager {

    private static final String TAG = "HRServer";

    /** 心率服务/特征/描述符 UUID (标准 GATT) */
    private static final UUID SVC_HEART_RATE = UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb");
    private static final UUID CHR_MEASUREMENT = UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb");
    private static final UUID DESC_CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private static final long[] BACKOFF_SECONDS = {5, 10, 20, 30};
    private static final long SCAN_TIMEOUT_MS = 10000;
    private static final long WATCHDOG_PERIOD_MS = 5000;
    private static final long WATCHDOG_STALE_MS = 15000;

    public interface ScanListener {
        void onDeviceFound(String name, String address);

        void onScanFinished(int count);

        /** 扫描失败(蓝牙栈错误码); default保持旧实现兼容 */
        default void onScanError(String msg) {
        }
    }

    public interface ConnListener {
        void onConnecting();

        void onConnected(String name);

        void onDisconnected(String reason);
    }

    private final Handler main = new Handler(Looper.getMainLooper());

    private volatile BluetoothGatt gatt;
    private volatile boolean connected = false;
    private volatile boolean manualStop = false;
    private volatile boolean connecting = false;
    private volatile boolean scanning = false;

    private String devName = "";
    private String devAddress = "";
    private int reconnectAttempt = 0;
    private int scanFound = 0;
    private long reconnectDeadline = 0;   // >0 表示有待执行的重连任务
    private Runnable reconnectTask;       // 待执行的重连任务(可精确取消)

    // 多候选智能重连(对齐EXE智能重连: 候选=最后连接优先+收藏设备去重, 轮换尝试;
    // 所有候选各失败一轮记 roundFail, 连续 MAX_ROUNDS 轮全失败自动停止)
    private final java.util.ArrayList<String[]> candList = new java.util.ArrayList<>(); // {name, address}
    private int candIdx = 0;
    private int roundFail = 0;

    private ConnListener connListener;
    private ScanListener scanListener;

    private final Runnable watchdog = new Runnable() {
        @Override
        public void run() {
            if (connected && HeartBus.get().getLastDataMs() > 0
                    && SystemClock.elapsedRealtime() - HeartBus.get().getLastDataMs() > WATCHDOG_STALE_MS) {
                android.util.Log.w(TAG, "看门狗: " + WATCHDOG_STALE_MS / 1000 + "秒无心率数据, 主动触发重连");
                BluetoothGatt g = gatt;
                if (g != null) g.disconnect();  // 触发 onConnectionStateChange 走统一断连流程
            }
            main.postDelayed(this, WATCHDOG_PERIOD_MS);
        }
    };

    public void setConnListener(ConnListener l) {
        connListener = l;
    }

    public boolean isConnected() {
        return connected;
    }

    public boolean isScanning() {
        return scanning;
    }

    public String getDevName() {
        return devName;
    }

    // ---------------- 权限 ----------------

    /** Android 8.1 权限模型: BLUETOOTH/BLUETOOTH_ADMIN 安装时授予, 定位需运行时申请 */
    public static boolean hasBlePermissions(Context ctx) {
        return ctx.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED
                && ctx.checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    public static void requestBlePermissions(Activity act, int requestCode) {
        act.requestPermissions(new String[]{
                android.Manifest.permission.ACCESS_FINE_LOCATION,
                android.Manifest.permission.ACCESS_COARSE_LOCATION}, requestCode);
    }

    // ---------------- 扫描 ----------------

    @SuppressLint("MissingPermission")
    public boolean startScan(Context ctx, ScanListener listener, long timeoutMs) {
        if (scanning) return true;
        BluetoothAdapter adapter = getAdapter(ctx);
        if (adapter == null || !adapter.isEnabled()) {
            android.util.Log.w(TAG, "扫描失败: 蓝牙未开启");
            return false;
        }
        scanListener = listener;
        scanFound = 0;
        scanning = true;
        adapter.getBluetoothLeScanner().startScan(mScanCallback);
        main.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (scanning) stopScan();
            }
        }, timeoutMs > 0 ? timeoutMs : SCAN_TIMEOUT_MS);
        android.util.Log.i(TAG, "BLE扫描已启动");
        return true;
    }

    public boolean startScan(Context ctx, ScanListener listener) {
        return startScan(ctx, listener, SCAN_TIMEOUT_MS);
    }

    @SuppressLint("MissingPermission")
    public void stopScan() {
        if (!scanning) return;
        scanning = false;
        Context ctx = appContext;
        BluetoothAdapter adapter = ctx != null ? getAdapter(ctx) : null;
        if (adapter != null && adapter.isEnabled()) {
            try {
                adapter.getBluetoothLeScanner().stopScan(mScanCallback);
            } catch (Exception e) {
                android.util.Log.w(TAG, "停止扫描异常: " + e);
            }
        }
        android.util.Log.i(TAG, "BLE扫描结束, 发现 " + scanFound + " 个命名设备");
        final ScanListener l = scanListener;
        final int n = scanFound;
        if (l != null) main.post(new Runnable() {
            @Override
            public void run() {
                l.onScanFinished(n);
            }
        });
    }

    private final ScanCallback mScanCallback = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            // 只回调有名字的设备(对齐 EXE filter_empty=True)
            String name = result.getDevice().getName();
            if (name == null || name.isEmpty()) return;
            scanFound++;
            final ScanListener l = scanListener;
            if (l != null) {
                final String fname = name;
                final String faddr = result.getDevice().getAddress();
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        l.onDeviceFound(fname, faddr);
                    }
                });
            }
        }

        @Override
        public void onScanFailed(int errorCode) {
            android.util.Log.e(TAG, "BLE扫描失败: " + errorCode);
            scanning = false;
            final ScanListener l = scanListener;
            if (l != null) {
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        l.onScanError("扫描失败(蓝牙错误码" + errorCode + ")");
                    }
                });
            }
        }
    };

    // ---------------- 连接 ----------------

    /**
     * 连接手环(自动重连开启)。address 为最后连接设备 MAC;
     * 重连候选 = 最后连接优先 + 收藏设备(按MAC去重), 对齐 EXE 智能重连候选顺序。
     */
    public void connect(Context ctx, String name, String address) {
        Context app = ctx.getApplicationContext();
        appContext = app;
        devName = name == null ? "" : name;
        devAddress = address == null ? "" : address;
        manualStop = false;
        connecting = true;
        reconnectAttempt = 0;
        roundFail = 0;
        candIdx = 0;
        buildCandidates(app, devName, devAddress);
        cancelReconnectTask();
        HeartBus.get().setDeviceName(devName);
        notifyConnecting();
        openGatt(app);
    }

    /** 构建重连候选: [最后连接] + 收藏设备(地址去重), JSON格式同EXE favorite_devices */
    private void buildCandidates(Context ctx, String name, String address) {
        candList.clear();
        if (address != null && !address.isEmpty()) {
            candList.add(new String[]{name == null ? "" : name, address});
        }
        try {
            org.json.JSONArray arr = new org.json.JSONArray(
                    Prefs.getStr(ctx, Prefs.FAV_DEVICES, "[]"));
            for (int i = 0; i < arr.length(); i++) {
                org.json.JSONObject o = arr.getJSONObject(i);
                String fa = o.optString("address", "");
                if (fa.isEmpty()) continue;
                boolean dup = false;
                for (String[] c : candList) {
                    if (c[1].equalsIgnoreCase(fa)) {
                        dup = true;
                        break;
                    }
                }
                if (!dup) candList.add(new String[]{o.optString("name", ""), fa});
            }
        } catch (Exception e) {
            android.util.Log.w(TAG, "收藏列表解析失败, 重连候选仅最后连接设备");
        }
        android.util.Log.i(TAG, "重连候选 " + candList.size() + " 台(最后连接优先+收藏去重)");
    }

    /** 手动断开: 不自动重连(设备管理页"断开"按钮/服务停止用) */
    public void disconnect() {
        manualStop = true;
        cancelReconnectTask();
        BluetoothGatt g = gatt;
        if (g != null) {
            g.disconnect(); // 触发回调统一走 onDisconnected 清理
        } else {
            connected = false;
            connecting = false;
        }
    }

    /** 服务停止时彻底清理(等同手动断开+停看门狗) */
    public void shutdown() {
        main.removeCallbacks(watchdog);
        disconnect();
    }

    private void openGatt(Context app) {
        BluetoothAdapter adapter = getAdapter(app);
        if (adapter == null || !adapter.isEnabled()) {
            android.util.Log.e(TAG, "连接失败: 蓝牙不可用");
            connecting = false;
            notifyDisconnected("蓝牙不可用");
            return;
        }
        BluetoothDevice device;
        try {
            device = adapter.getRemoteDevice(devAddress);
        } catch (Exception e) {
            android.util.Log.e(TAG, "MAC地址无效: " + devAddress);
            connecting = false;
            notifyDisconnected("MAC地址无效");
            return;
        }
        android.util.Log.i(TAG, "正在连接 " + devName + " (" + devAddress + ") 第" + (reconnectAttempt + 1) + "次尝试");
        // 第二参数 autoConnect=false: 直连模式, 断连靠我们自己的退避重连(可控性更好)
        try {
            gatt = device.connectGatt(app, false, mGattCallback);
        } catch (Exception e) {
            android.util.Log.e(TAG, "connectGatt 异常: " + e);
            connecting = false;
            notifyDisconnected("连接异常: " + e.getMessage());
        }
    }

    private final BluetoothGattCallback mGattCallback = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                android.util.Log.i(TAG, "GATT已连接, 开始服务发现");
                g.discoverServices();
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                String reason = "status=" + status;
                if (status == 133) reason = "GATT_ERROR_133";
                android.util.Log.w(TAG, "GATT断开: " + reason + (manualStop ? " (手动)" : ""));
                cleanupGatt(g);
                boolean wasConnected = connected;
                connected = false;
                connecting = false;
                if (wasConnected || !manualStop) {
                    HeartBus.get().publishDeviceStatus(false);
                }
                notifyDisconnected(reason);
                if (!manualStop) {
                    scheduleReconnect();
                }
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                android.util.Log.e(TAG, "服务发现失败: status=" + status);
                g.disconnect();
                return;
            }
            BluetoothGattService svc = g.getService(SVC_HEART_RATE);
            BluetoothGattCharacteristic chr = svc == null ? null : svc.getCharacteristic(CHR_MEASUREMENT);
            if (chr == null) {
                android.util.Log.e(TAG, "设备无心率服务(0x180D/0x2A37), 断开");
                g.disconnect();
                return;
            }
            // 本地通知开关 + CCCD 订阅(两步缺一不可)
            g.setCharacteristicNotification(chr, true);
            BluetoothGattDescriptor cccd = chr.getDescriptor(DESC_CCCD);
            if (cccd == null) {
                android.util.Log.e(TAG, "未找到CCCD描述符, 断开");
                g.disconnect();
                return;
            }
            cccd.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
            boolean ok = g.writeDescriptor(cccd);
            android.util.Log.i(TAG, "已请求心率通知订阅: " + ok);
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor descriptor, int status) {
            if (DESC_CCCD.equals(descriptor.getUuid())) {
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    android.util.Log.i(TAG, "心率通知订阅成功, " + devName + " 已连接");
                    connected = true;
                    connecting = false;
                    reconnectAttempt = 0;   // 成功清零退避
                    roundFail = 0;
                    // 实际连上的设备写回"最后连接"(对齐EXE连接成功更新当前设备; 候选轮换换人时生效)
                    if (appContext != null) {
                        String saved = Prefs.getStr(appContext, Prefs.DEV_ADDRESS, "");
                        if (!devAddress.equalsIgnoreCase(saved)) {
                            Prefs.putStr(appContext, Prefs.DEV_NAME, devName);
                            Prefs.putStr(appContext, Prefs.DEV_ADDRESS, devAddress);
                            android.util.Log.i(TAG, "最后连接设备已更新为 " + devName);
                        }
                    }
                    HeartBus.get().publishDeviceStatus(true);
                    notifyConnected();
                    // 启动看门狗(幂等)
                    main.removeCallbacks(watchdog);
                    main.postDelayed(watchdog, WATCHDOG_PERIOD_MS);
                } else {
                    android.util.Log.e(TAG, "CCCD写入失败: status=" + status);
                    g.disconnect();
                }
            }
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic characteristic) {
            if (!CHR_MEASUREMENT.equals(characteristic.getUuid())) return;
            int hr = parseHeartRate(characteristic.getValue());
            if (hr < 0) {
                // 脏包/短包不入库(对齐 EXE: 长度校验+丢弃)
                android.util.Log.w(TAG, "心率数据格式异常已丢弃: " + bytesHex(characteristic.getValue()));
                return;
            }
            HeartBus.get().publishHeartRate(hr);
        }
    };

    /** 返回 -1 表示脏包(对齐 EXE 解析规则) */
    private static int parseHeartRate(byte[] data) {
        if (data == null || data.length < 2) return -1;
        int flags = data[0] & 0xFF;
        if ((flags & 0x01) == 0x01) {
            if (data.length < 3) return -1;
            return (data[1] & 0xFF) | ((data[2] & 0xFF) << 8);
        }
        return data[1] & 0xFF;
    }

    // ---------------- 重连(多候选轮换, 对齐EXE智能重连) ----------------

    private static final int MAX_ROUNDS = 3;          // 连续3轮全部候选失败→自动停止(EXE #12同款)
    private static final long ROUND_PAUSE_MS = 30000; // 轮间暂停

    private void scheduleReconnect() {
        // 当前候选退避序列(5/10/20/30)走完后 → 切换下一候选; 全部候选失败 → 记一轮
        if (reconnectAttempt >= BACKOFF_SECONDS.length) {
            reconnectAttempt = 0;
            candIdx++;
            if (candIdx >= candList.size()) {
                candIdx = 0;
                roundFail++;
                if (roundFail >= MAX_ROUNDS) {
                    android.util.Log.w(TAG, "连续" + MAX_ROUNDS + "轮全部候选重连失败, 停止自动重连");
                    notifyDisconnected("连续" + MAX_ROUNDS + "轮重连失败, 已停止(请检查手环电量/距离后重新开始监测)");
                    return;
                }
                android.util.Log.i(TAG, "第" + roundFail + "轮候选全部失败, " + ROUND_PAUSE_MS / 1000 + "秒后从头再来");
                final Context app = appContext;
                reconnectDeadline = SystemClock.elapsedRealtime() + ROUND_PAUSE_MS;
                reconnectTask = new Runnable() {
                    @Override
                    public void run() {
                        reconnectTask = null;
                        reconnectDeadline = 0;
                        if (manualStop || connected || connecting || app == null) return;
                        switchCandidate();
                        openGatt(app);
                    }
                };
                main.postDelayed(reconnectTask, ROUND_PAUSE_MS);
                return;
            }
            switchCandidate();
        }
        long delay = BACKOFF_SECONDS[reconnectAttempt] * 1000L;
        reconnectAttempt++;
        android.util.Log.i(TAG, "将在 " + delay / 1000 + " 秒后重连 " + devName
                + " (设备" + (candIdx + 1) + "/" + candList.size() + ", 第" + roundFail + "轮)");
        // 候选>1时把轮换进度带进断连文案(通知栏可见, 对齐EXE智能重连状态提示)
        notifyDisconnected(candList.size() > 1
                ? "自动重连中, 尝试设备 " + (candIdx + 1) + "/" + candList.size() + ": " + devName
                : "自动重连中");
        final Context app = appContext;
        reconnectDeadline = SystemClock.elapsedRealtime() + delay;
        reconnectTask = new Runnable() {
            @Override
            public void run() {
                reconnectTask = null;
                reconnectDeadline = 0;
                if (manualStop || connected || connecting) return;
                if (app == null) return;
                openGatt(app);   // 不重置 reconnectAttempt, 让退避继续升级
            }
        };
        main.postDelayed(reconnectTask, delay);
    }

    /** 切到当前轮换候选: 更新目标设备与总线设备名 */
    private void switchCandidate() {
        String[] c = candList.get(candIdx);
        devName = c[0];
        devAddress = c[1];
        HeartBus.get().setDeviceName(devName);
        android.util.Log.i(TAG, "切换重连候选: " + devName + " (" + devAddress + ")");
    }

    private void cancelReconnectTask() {
        if (reconnectTask != null) {
            main.removeCallbacks(reconnectTask);
            reconnectTask = null;
        }
        reconnectDeadline = 0;
    }

    private void cleanupGatt(BluetoothGatt g) {
        try {
            g.close();
        } catch (Exception e) {
            android.util.Log.w(TAG, "gatt.close 异常: " + e);
        }
        if (g == gatt) gatt = null;
    }

    // ---------------- 通知回调(主线程) ----------------

    private void notifyConnecting() {
        main.post(new Runnable() {
            @Override
            public void run() {
                if (connListener != null) connListener.onConnecting();
            }
        });
    }

    private void notifyConnected() {
        final String n = devName;
        main.post(new Runnable() {
            @Override
            public void run() {
                if (connListener != null) connListener.onConnected(n);
            }
        });
    }

    private void notifyDisconnected(final String reason) {
        main.post(new Runnable() {
            @Override
            public void run() {
                if (connListener != null) connListener.onDisconnected(reason);
            }
        });
    }

    // ---------------- 工具 ----------------

    private static Context appContext;

    private static BluetoothAdapter getAdapter(Context ctx) {
        BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        return bm == null ? null : bm.getAdapter();
    }

    private static String bytesHex(byte[] data) {
        if (data == null) return "null";
        StringBuilder sb = new StringBuilder();
        for (byte b : data) sb.append(String.format("%02X ", b));
        return sb.toString().trim();
    }
}
