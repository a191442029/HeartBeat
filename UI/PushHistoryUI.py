from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox, QWidget
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from push_notifier import load_push_history, clear_push_history
from .basicwidgets import hint_label


class PushHistoryUI(QWidget):
    """推送记录页面: 展示最近200条推送结果(含测试推送), 新记录在上"""

    def __init__(self):
        super().__init__()
        self._setup_ui()
        self.refresh()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)  # 页面级边距由主窗口统一包装提供
        layout.setSpacing(8)
        self.setLayout(layout)

        layout.addWidget(hint_label("记录所有渠道的每次推送(含测试), 保留最近200条; 多设备时会汇总各设备结果。"))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["时间", "渠道", "结果", "标题", "内容/说明"])
        # "内容/说明"列固定初始宽度并可拖拽调整, 内容超出时表格底部出现横向滚动条
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.horizontalHeader().resizeSection(4, 400)
        for col in range(4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        layout.addWidget(self.table)

        # 底部按钮行: 刷新/清空属于推送记录, 置于表格下方, 与左侧"保存数据到文件"按钮行对齐
        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        btn_layout.addWidget(self.refresh_btn)
        self.clear_btn = QPushButton("清空记录")
        self.clear_btn.clicked.connect(self._clear)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def showEvent(self, event):
        """切到本页时自动刷新"""
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        """从存储读取记录并填充表格(最新在前)"""
        records = load_push_history()
        self.table.setRowCount(0)
        for rec in reversed(records):
            if not isinstance(rec, dict):
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(rec.get("time", ""))))
            self.table.setItem(r, 1, QTableWidgetItem(str(rec.get("channel", ""))))
            ok = bool(rec.get("ok"))
            result_item = QTableWidgetItem("成功" if ok else "失败")
            result_item.setForeground(QColor("#2e7d32" if ok else "#c62828"))
            self.table.setItem(r, 2, result_item)
            title_item = QTableWidgetItem(str(rec.get("title", "")))
            self.table.setItem(r, 3, title_item)
            content = str(rec.get("msg", ""))
            note = str(rec.get("note", ""))
            if note and note != "推送成功":
                content = f"{content} ({note})" if content else note
            content_item = QTableWidgetItem(content)
            # 列宽不够被截断时, 悬停可看全文
            content_item.setToolTip(content)
            self.table.setItem(r, 4, content_item)
            self.table.setRowHeight(r, 28)

    def _clear(self):
        ret = QMessageBox.question(self, "确认", "确定清空全部推送记录?", 
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        clear_push_history()
        self.refresh()
