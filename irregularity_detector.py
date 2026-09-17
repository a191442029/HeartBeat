"""
疑似心律不齐(心率无序波动)检测器 — 基于整秒心率的筛查性算法

背景: 手环 BLE 心率特征无 RR 间期字段, 仅有每秒整数心率, 无法做文献级的
逐搏间期分析(RMSSD/SampEn)。降级方案利用"房颤时逐秒心率无规律大幅跳动,
静息窦律逐秒平稳"的特征, 在滑动窗口内统计心率的无序性。

判定条件(全部满足才视为异常窗口):
1. 静息门控: 窗口平均心率 <= rest_max_hr (运动时波动大属正常)
2. 趋势门控: 前后半窗均值差 <= 5 bpm (排除运动上升/恢复下降的单调趋势)
3. 无序性:   窗口心率标准差 > sd_threshold 且大幅跳变占比 > jump_ratio

防误报: 需连续 sustain_windows 个异常窗口才触发; 触发后进入冷却期;
心率断流(<=0)即清空窗口, 避免断连重连后的脏数据误判。

定位: 筛查提示, 非医学诊断, 无法区分房颤/窦性心律不齐/早搏。
"""
import time
from collections import deque

# 趋势门控阈值: 前后半窗平均心率差超过此值视为趋势性变化(bpm)
TREND_LIMIT_BPM = 5.0


class IrregularityDetector:
    """滑动窗口心率无序性检测, 每秒喂入一次心率值"""

    def __init__(self, window_seconds: int = 60, sd_threshold: float = 5.0,
                 jump_bpm: int = 5, jump_ratio: float = 0.3,
                 rest_max_hr: int = 100, sustain_windows: int = 2,
                 cooldown_seconds: int = 600):
        self.window_seconds = max(30, int(window_seconds))
        self.sd_threshold = float(sd_threshold)
        self.jump_bpm = int(jump_bpm)
        self.jump_ratio = float(jump_ratio)
        self.rest_max_hr = int(rest_max_hr)
        self.sustain_windows = max(1, int(sustain_windows))
        self.cooldown_seconds = int(cooldown_seconds)
        self._buf = deque(maxlen=self.window_seconds)  # 最近N秒心率
        self._consecutive = 0        # 连续异常窗口计数
        self._last_alert = float('-inf')  # 上次告警时间戳(-inf保证启动后首次可告警)

    def reset(self):
        """清空状态(断连/手动停止时调用)"""
        self._buf.clear()
        self._consecutive = 0

    def check(self, heart_rate: int, now: float = None) -> dict:
        """每秒喂入心率, 返回告警指标dict(触发时)或None

        返回dict: {"mean", "sd", "jump_ratio"} 供推送文案使用
        """
        if heart_rate <= 0:
            # 心率无效(断连/信号丢失): 窗口作废, 连续计数清零
            self.reset()
            return None
        if now is None:
            now = time.time()
        self._buf.append(int(heart_rate))
        if len(self._buf) < self.window_seconds:
            return None

        metrics = self._metrics()
        if not self._is_abnormal(metrics):
            self._consecutive = 0
            return None

        self._consecutive += 1
        if self._consecutive < self.sustain_windows:
            return None
        if now - self._last_alert < self.cooldown_seconds:
            return None
        self._last_alert = now
        return metrics

    def _metrics(self) -> dict:
        vals = list(self._buf)
        n = len(vals)
        mean = sum(vals) / n
        sd = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
        diffs = [abs(vals[i + 1] - vals[i]) for i in range(n - 1)]
        jumps = sum(1 for d in diffs if d >= self.jump_bpm)
        jump_ratio = jumps / len(diffs) if diffs else 0.0
        # 趋势: 前后半窗均值差
        half = n // 2
        trend = abs(sum(vals[half:]) / (n - half) - sum(vals[:half]) / half)
        return {"mean": mean, "sd": sd, "jump_ratio": jump_ratio, "trend": trend}

    def _is_abnormal(self, m: dict) -> bool:
        return (m["mean"] <= self.rest_max_hr
                and m["trend"] <= TREND_LIMIT_BPM
                and m["sd"] > self.sd_threshold
                and m["jump_ratio"] > self.jump_ratio)
