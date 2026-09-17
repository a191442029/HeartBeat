from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QSlider, QCheckBox, QBoxLayout, QLabel, QHBoxLayout,
                             QScrollArea, QSizePolicy, QWidget, QVBoxLayout)

# ============================================================
# 统一UI间距规范 (所有TAB页面共用, 保证视觉一致性)
# ============================================================
PAGE_MARGINS = (16, 12, 16, 12)   # 页面四周留白: 左/上/右/下
PAGE_SPACING = 12                 # 页面内区块(GroupBox)之间的垂直间距
GROUP_SPACING = 8                 # GroupBox内部控件之间的间距
FORM_MAX_WIDTH = 640              # 简单表单页(单列)的最大内容宽度, 避免输入框通栏过宽


def page_layout(layout):
    """页面级布局统一边距与间距"""
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(PAGE_SPACING)
    return layout


def group_layout(layout):
    """GroupBox内部布局统一间距"""
    layout.setSpacing(GROUP_SPACING)
    return layout


def hint_label(text: str) -> QLabel:
    """统一的灰色说明文字标签(自动换行)"""
    lbl = QLabel(text)
    lbl.setStyleSheet("color: gray;")
    lbl.setWordWrap(True)
    return lbl


def button_row(*buttons) -> QHBoxLayout:
    """统一按钮行: 左对齐排列若干按钮"""
    lay = QHBoxLayout()
    for b in buttons:
        lay.addWidget(b)
    lay.addStretch()
    return lay


def wrap_scroll(widget: QWidget) -> QScrollArea:
    """把页面内容包进无边框滚动区, 内容超 高/宽 时出滚动条, 保证小屏可用"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(widget)
    return scroll


# 全局统一样式: 仅做轻度统一(标签页内边距/分组标题加粗/表格行高), 不破坏系统原生风格
GLOBAL_QSS = """
QTabWidget::pane { border: 1px solid #c0c0c0; }
QTabBar::tab { padding: 6px 14px; }
QGroupBox { font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QTableWidget { gridline-color: #d8d8d8; }
"""


class Slider_(QSlider):
    def __init__(self, initial_value, value_changed_callback, Range = (0, 255)):
        super().__init__(Qt.Horizontal)
        self.value_changed_callback = value_changed_callback

        self.setRange(*Range)
        self.setValue(initial_value)

        # 连接信号和槽
        self.valueChanged.connect(
            lambda value: self.value_changed_callback(value, ups_=False)
        )

    def mouseReleaseEvent(self, ev):
        self.value_changed_callback(ups_=True)
        return super().mouseReleaseEvent(ev)

class CheackBox_(QCheckBox):
    def __init__(self, text:str, f_layout:QBoxLayout, setC:bool, Ch_slot):
        super().__init__(text)
        self.setChecked(setC)
        self.stateChanged.connect(Ch_slot)
        f_layout.addWidget(self)
