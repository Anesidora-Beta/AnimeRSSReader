"""时钟端口：任务调度的时间来源。

抽出来只为一件事——测试里不用真的等 300 秒。
默认实现见 ``adapters/system_clock.py``。
"""
import time
from abc import ABC, abstractmethod

__all__ = ["ClockPort"]


class ClockPort(ABC):
    @abstractmethod
    def monotonic(self):
        """单调递增秒数，用于计算间隔（不受系统改时间影响）。"""

    def time(self):
        """墙上时间，用于展示「几秒前」。"""
        return time.time()
