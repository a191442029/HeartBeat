package com.hrmlink.hrserver;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * 摄像头墙（MVP=快照宫格, ui_preview.html 定稿; 对标 EXE 摄像头墙 Tab）:
 * - camListJson() 解析相机列表 → 每行 2 格自适应排布（2-6 路均可, 奇数路末尾占位）
 * - 每格 500ms 轮询最新快照(mtime 变化才 decode)——帧源复用常驻拉流线程顺带抓的 snap.jpg,
 *   零额外解码进程; 真机联调后再评估升 HLS/fMP4 实时流(开发计划 §8 遗留)
 * - 点击单格放大(AlertDialog 全屏, 同帧源继续刷新); 角标="● 相机名 · 默认"
 */
public class CameraWallActivity extends Activity {

    private LinearLayout grid;
    private TextView txtEmpty, txtCount;
    private final List<Cell> cells = new ArrayList<>();

    private static final class Cell {
        String cam;
        ImageView img;
        long mts = 0;
    }

    private AlertDialog zoomDlg;
    private ImageView zoomImg;
    private String zoomCam = "";
    private long zoomMts = 0;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            pump();
            mainHandler.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_camera_wall);
        grid = findViewById(R.id.wall_grid);
        txtEmpty = findViewById(R.id.wall_empty);
        txtCount = findViewById(R.id.wall_count);
        findViewById(R.id.btn_back).setOnClickListener(v -> finish());

        buildGrid();
        txtEmpty.setVisibility(cells.isEmpty() ? View.VISIBLE : View.GONE);
        txtCount.setText(cells.size() + " 路");
    }

    @Override
    protected void onResume() {
        super.onResume();
        pump();
        mainHandler.postDelayed(tick, 500);
    }

    @Override
    protected void onPause() {
        super.onPause();
        mainHandler.removeCallbacks(tick);
        if (zoomDlg != null) {
            zoomDlg.dismiss();
            zoomDlg = null;
        }
    }

    // ---------------- 宫格构建 ----------------

    private void buildGrid() {
        cells.clear();
        List<String> names = new ArrayList<>();
        List<Boolean> defs = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(CameraManager.get().camListJson());
            for (int i = 0; i < arr.length(); i++) {
                names.add(arr.getJSONObject(i).optString("name", ""));
                defs.add(arr.getJSONObject(i).optBoolean("is_default", false));
            }
        } catch (Exception ignore) {
        }
        int rows = (names.size() + 1) / 2;
        for (int r = 0; r < rows; r++) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setLayoutParams(new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
            for (int k = 0; k < 2; k++) {
                int idx = r * 2 + k;
                View cell;
                if (idx < names.size()) {
                    cell = buildCell(names.get(idx), defs.get(idx));
                } else {
                    // 奇数路末尾占位(保持左右等分)
                    cell = new View(this);
                    cell.setLayoutParams(marginParams());
                }
                row.addView(cell);
            }
            grid.addView(row);
        }
    }

    private View buildCell(String name, boolean isDefault) {
        FrameLayout box = new FrameLayout(this);
        box.setLayoutParams(marginParams());
        box.setBackgroundResource(R.drawable.bg_card);

        ImageView img = new ImageView(this);
        img.setScaleType(ImageView.ScaleType.FIT_CENTER);
        box.addView(img, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        TextView tag = new TextView(this);
        tag.setText("● " + name + (isDefault ? " · 默认" : ""));
        tag.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        tag.setTextColor(0xFFFFFFFF);
        tag.setBackgroundResource(R.drawable.bg_minicam_tag);
        tag.setPadding(dp(8), dp(3), dp(8), dp(3));
        FrameLayout.LayoutParams tlp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.START | Gravity.TOP);
        tlp.setMargins(dp(8), dp(8), 0, 0);
        box.addView(tag, tlp);

        box.setOnClickListener(v -> showZoom(name));
        Cell c = new Cell();
        c.cam = name;
        c.img = img;
        cells.add(c);
        return box;
    }

    private LinearLayout.LayoutParams marginParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.MATCH_PARENT, 1f);
        lp.setMargins(dp(6), dp(6), dp(6), dp(6));
        return lp;
    }

    // ---------------- 帧刷新 ----------------

    private void pump() {
        CameraManager cam = CameraManager.get();
        for (Cell c : cells) {
            File f = cam.snapshotFile(c.cam);
            if (f == null || !f.isFile()) continue;
            long m = f.lastModified();
            if (m == c.mts) continue;
            c.mts = m;
            Bitmap bp = BitmapFactory.decodeFile(f.getAbsolutePath());
            if (bp != null) c.img.setImageBitmap(bp);
        }
        // 放大窗同步刷新
        if (zoomDlg != null && zoomDlg.isShowing() && !zoomCam.isEmpty()) {
            File f = cam.snapshotFile(zoomCam);
            if (f != null && f.isFile() && f.lastModified() != zoomMts) {
                zoomMts = f.lastModified();
                Bitmap bp = BitmapFactory.decodeFile(f.getAbsolutePath());
                if (bp != null) zoomImg.setImageBitmap(bp);
            }
        }
    }

    private void showZoom(String name) {
        zoomCam = name;
        zoomMts = 0;
        zoomImg = new ImageView(this);
        zoomImg.setScaleType(ImageView.ScaleType.FIT_CENTER);
        zoomImg.setPadding(dp(8), dp(8), dp(8), dp(8));
        zoomDlg = new AlertDialog.Builder(this)
                .setTitle(name)
                .setView(zoomImg)
                .setPositiveButton("✕ 关闭", null)
                .create();
        zoomDlg.show();
        if (zoomDlg.getWindow() != null) {
            zoomDlg.getWindow().setLayout(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        }
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP,
                v, getResources().getDisplayMetrics()));
    }
}
