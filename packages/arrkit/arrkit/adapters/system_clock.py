"""时钟端口的默认实现。"""
import time

from ..ports.clock import ClockPort

__all__ = ["SystemClock", "FakeClock"]


class SystemClock(ClockPort):
    def monotonic(self):
        return time.monotonic()

    def time(self):
        return time.time()


class FakeClock(ClockPort):
    """手动推进的时钟，测试定时任务用（一点也不用真等）。"""

    def __init__(self, start=0.0):
        self._now = float(start)

    def monotonic(self):
        return self._now

    def time(self):
        return self._now

    def advance(self, seconds):
        self._now += float(seconds)
        return self._now

    def set(self, value):
        self._now = float(value)
        return self._now
