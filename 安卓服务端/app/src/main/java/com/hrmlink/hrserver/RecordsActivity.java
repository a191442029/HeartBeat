package com.hrmlink.hrserver;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 推送记录页: 展示 AlarmEngine 内存环形缓冲(最近50条, 最新在前),
 * 含设备断连/恢复、心率告警、疑似心律不齐; 重启应用后清空。
 */
public class RecordsActivity extends Activity {

    private ArrayAdapter<String> adapter;
    private final ArrayList<String> rows = new ArrayList<>();
    private TextView txtEmpty;
    private ListView list;
    private final SimpleDateFormat fmt =
            new SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_records);
        list = findViewById(R.id.list_records);
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
        findViewById(R.id.btn_records_refresh).setOnClickListener(v -> refresh());
        findViewById(R.id.btn_records_clear).setOnClickListener(v -> {
            AlarmEngine.clearHistory();
            refresh();
            Toast.makeText(this, "已清空推送记录", Toast.LENGTH_SHORT).show();
        });
        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();   // 从设置页回来时刷新
    }

    private void refresh() {
        List<AlarmEngine.AlarmRecord> history = AlarmEngine.getHistory();
        rows.clear();
        for (AlarmEngine.AlarmRecord r : history) {
            rows.add(fmt.format(new Date(r.timestamp)) + "  [" + kindName(r.kind) + "] " + r.title);
            rows.add(r.body);
        }
        adapter.notifyDataSetChanged();
        boolean empty = rows.isEmpty();
        list.setVisibility(empty ? View.GONE : View.VISIBLE);
        txtEmpty.setVisibility(empty ? View.VISIBLE : View.GONE);
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
}
