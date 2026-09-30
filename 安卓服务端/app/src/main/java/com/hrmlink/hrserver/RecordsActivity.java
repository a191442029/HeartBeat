package com.hrmlink.hrserver;

import android.app.Activity;
import android.net.Uri;
import android.os.Bundle;
import android.util.TypedValue;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.VideoView;

import java.io.File;
import java.io.FileOutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 记录页(ui_preview.html 定稿): 上=回放区(点剪辑卡片加载, 左上角标"回放 · 相机 · 时间"),
 * 中=剪辑卡片横排(相机/时间, 选中高亮), 下=推送记录表(AlarmEngine 内存环形缓冲 最近50条) + 导出CSV。
 * 剪辑来源: CameraManager.listClips()(存档根+板载合并, 时间倒序)。
 */
public class RecordsActivity extends Activity {

    /** 剪辑文件名解析: {相机名安全化}_{yyyyMMdd}_{HHmmss}_{ms}.mp4 */
    private static final Pattern CLIP_NAME = Pattern.compile("(.+)_(\\d{8})_(\\d{6})_\\d{3}\\.mp4");

    private ArrayAdapter<String> adapter;
    private final ArrayList<String> rows = new ArrayList<>();
    private TextView txtEmpty, txtReplayTag;
    private VideoView video;
    private View replayHint;
    private LinearLayout clipRow;
    private List<File> clips = new ArrayList<>();
    private int selClip = -1;
    private final SimpleDateFormat fmt =
            new SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_records);
        video = findViewById(R.id.video_replay);
        txtReplayTag = findViewById(R.id.txt_replay_tag);
        replayHint = findViewById(R.id.txt_replay_hint);
        clipRow = findViewById(R.id.clip_row);
        ListView list = findViewById(R.id.list_records);
        txtEmpty = findViewById(R.id.txt_records_empty);
        adapter = new ArrayAdapter<String>(this, android.R.layout.simple_list_item_2, rows) {
            @Override
            public View getView(int position, View convertView, android.view.ViewGroup parent) {
                View v = super.getView(position, convertView, parent);
                TextView t1 = v.findViewById(android.R.id.text1);
                TextView t2 = v.findViewById(android.R.id.text2);
                if (t1 != null) t1.setTextColor(0xFFFFFFFF);
                if (t2 != null) t2.setTextColor(0xFFAAAAAA);
                return v;
            }
        };
        list.setAdapter(adapter);
        findViewById(R.id.btn_records_refresh).setOnClickListener(v -> {
            loadClips();
            refreshRecords();
        });
        findViewById(R.id.btn_records_clear).setOnClickListener(v -> {
            AlarmEngine.clearHistory();
            refreshRecords();
            Toast.makeText(this, "已清空推送记录", Toast.LENGTH_SHORT).show();
        });
        findViewById(R.id.btn_export_csv).setOnClickListener(v -> exportCsv());
        loadClips();
        refreshRecords();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadClips();
        refreshRecords();   // 从设置页回来时刷新
    }

    // ---------------- 剪辑卡片 ----------------

    private void loadClips() {
        clips.clear();
        for (File f : CameraManager.get().listClips()) clips.add(f);
        renderClips();
    }

    private void renderClips() {
        clipRow.removeAllViews();
        if (clips.isEmpty()) {
            clipRow.addView(clipCard("暂无报警剪辑", "", -1));
            return;
        }
        for (int i = 0; i < clips.size(); i++) {
            clipRow.addView(clipCard(clipLabel(clips.get(i)), "", i));
        }
        if (selClip >= clips.size()) selClip = -1;
    }

    private Button clipCard(String label, String sub, final int idx) {
        Button b = new Button(this);
        b.setText(label + (sub.isEmpty() ? "" : "\n" + sub));
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        b.setMinWidth(0);
        b.setMinHeight(0);
        b.setPadding(dp(12), dp(8), dp(12), dp(8));
        b.setTextColor(idx == selClip ? 0xFFFFFFFF : 0xFFBBBBCC);
        b.setBackgroundColor(idx == selClip ? 0xFF3A5F2E : 0xFF23252E);
        if (idx >= 0) {
            b.setOnClickListener(v -> playClip(idx));
        }
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, 0, dp(8), 0);
        b.setLayoutParams(lp);
        return b;
    }

    /** 卡片标签: 相机名 + 时间(从文件名解析; 非法退回文件名) */
    private static String clipLabel(File f) {
        Matcher m = CLIP_NAME.matcher(f.getName());
        if (m.matches()) {
            String cam = m.group(1);
            String d = m.group(2), t = m.group(3);
            return cam + "\n" + d.substring(4, 6) + "-" + d.substring(6, 8)
                    + " " + t.substring(0, 2) + ":" + t.substring(2, 4) + ":" + t.substring(4, 6);
        }
        return f.getName();
    }

    private void playClip(int idx) {
        if (idx < 0 || idx >= clips.size()) return;
        selClip = idx;
        renderClips();
        File f = clips.get(idx);
        replayHint.setVisibility(View.GONE);
        txtReplayTag.setVisibility(View.VISIBLE);
        txtReplayTag.setText("回放 · " + clipLabel(f).replace("\n", " · "));
        video.stopPlayback();
        video.setVideoURI(Uri.fromFile(f));
        video.start();
    }

    // ---------------- 推送记录 ----------------

    private void refreshRecords() {
        List<AlarmEngine.AlarmRecord> history = AlarmEngine.getHistory();
        rows.clear();
        for (AlarmEngine.AlarmRecord r : history) {
            rows.add(fmt.format(new Date(r.timestamp)) + "  [" + kindName(r.kind) + "] " + r.title);
            rows.add(r.body);
        }
        adapter.notifyDataSetChanged();
        boolean empty = rows.isEmpty();
        findViewById(R.id.list_records).setVisibility(empty ? View.GONE : View.VISIBLE);
        txtEmpty.setVisibility(empty ? View.VISIBLE : View.GONE);
    }

    /** 导出CSV: 推送记录存档根(按设置U盘/板载) → pushlog_时间戳.csv */
    private void exportCsv() {
        List<AlarmEngine.AlarmRecord> history = AlarmEngine.getHistory();
        if (history.isEmpty()) {
            Toast.makeText(this, "暂无推送记录", Toast.LENGTH_SHORT).show();
            return;
        }
        File dir = StorageUtil.archiveRoot(this, Prefs.PUSHLOG_STORAGE, "pushlog");
        File out = new File(dir, "pushlog_"
                + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date()) + ".csv");
        FileOutputStream fos = null;
        try {
            fos = new FileOutputStream(out);
            StringBuilder sb = new StringBuilder();
            sb.append("time,kind,title,body\r\n");
            for (AlarmEngine.AlarmRecord r : history) {
                sb.append(new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date(r.timestamp)))
                        .append(',').append(kindName(r.kind))
                        .append(",\"").append(r.title.replace("\"", "\"\""))
                        .append("\",\"").append(r.body.replace("\"", "\"\"")).append("\"\r\n");
            }
            fos.write(sb.toString().getBytes("UTF-8"));
            Toast.makeText(this, "已导出 " + history.size() + " 条 → " + out.getAbsolutePath(),
                    Toast.LENGTH_LONG).show();
        } catch (Exception e) {
            Toast.makeText(this, "导出失败: " + e.getMessage(), Toast.LENGTH_LONG).show();
        } finally {
            if (fos != null) {
                try {
                    fos.close();
                } catch (Exception ignore) {
                }
            }
        }
    }

    private static String kindName(int kind) {
        switch (kind) {
            case AlarmEngine.KIND_HIGH:
                return "心率过高";
            case AlarmEngine.KIND_LOW:
                return "心率过低";
            case AlarmEngine.KIND_IRREGULAR:
                return "心律不齐";
            case AlarmEngine.KIND_DEV_LOST:
                return "设备断开";
            case AlarmEngine.KIND_DEV_BACK:
                return "设备恢复";
            default:
                return "告警";
        }
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
