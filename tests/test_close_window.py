#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关窗后的窗口监视测试。

关窗时最容易出问题的两件事：
  * 把主窗口重新开出来当「弹窗背景」，它会换个位置/尺寸闪一下（已去掉）
  * 最小化的窗口被误判成「已经关了」，于是在窗口还在时就把询问框弹出来

这里用假的进程和假的窗口列表，验证 ``_watch_window`` 的判断：
真的关掉才算关掉、最小化不算、进程退出也能收尾。

跑法： ``python -m unittest discover -s tests -v``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as APP                                        # noqa: E402


class _Proc:
    """假进程：按预先排好的剧本回 poll()"""

    pid = 4242

    def __init__(self, polls):
        self.polls = list(polls)
        self.terminated = False

    def poll(self):
        if self.polls:
            return self.polls.pop(0)
        return 0

    def terminate(self):
        self.terminated = True


def _win(rect, min_=False):
    return {"rect": rect, "min": min_}


class WatchWindowTest(unittest.TestCase):

    def setUp(self):
        self._real = APP._pid_windows
        self.ui = APP.WindowSupervisor("http://127.0.0.1:1/", "1240,880",
                                       "ask", server=None)

    def tearDown(self):
        APP._pid_windows = self._real

    def _script(self, frames):
        """frames: 每次采样要返回的窗口列表；用完之后一律返回空"""
        box = {"frames": list(frames)}

        def fake(pid):
            if box["frames"]:
                return box["frames"].pop(0)
            return []

        APP._pid_windows = fake

    def test_window_gone_returns_last_rect(self):
        self._script([[_win((10, 10, 1240, 880))],
                      [_win((10, 10, 1240, 880))],
                      [], []])
        rect, why = self.ui._watch_window(_Proc([None] * 20))
        self.assertEqual(why, "window-gone")
        self.assertEqual(rect, (10, 10, 1240, 880))

    def test_minimized_is_not_closed(self):
        # 第 3 帧开始最小化：窗口还在，不能当成关窗；进程随后退出
        self._script([[_win((10, 10, 1240, 880))],
                      [_win((-32000, -32000, 1240, 880), min_=True)],
                      [_win((-32000, -32000, 1240, 880), min_=True)]])
        rect, why = self.ui._watch_window(_Proc([None, None, None, 0]))
        self.assertEqual(why, "exited")
        self.assertEqual(rect, (10, 10, 1240, 880))   # 保留最小化前的坐标

    def test_process_exit_without_window_is_abortable(self):
        # 窗口一直没出现（启动失败）：rect 为空，交给上层判成 abort
        self._script([[], []])
        rect, why = self.ui._watch_window(_Proc([None, 0]))
        self.assertIsNone(rect)
        self.assertEqual(why, "exited")

    def test_stop_wins(self):
        self._script([[_win((10, 10, 1240, 880))]] * 5)
        self.ui._stopping = True
        rect, why = self.ui._watch_window(_Proc([None] * 5))
        self.assertEqual(why, "stop")
        self.assertIsNone(rect)

    def test_rect_helper_skips_minimized_and_picks_largest(self):
        self._script([[_win((0, 0, 300, 200)),
                       _win((10, 10, 1240, 880)),
                       _win((-32000, -32000, 1600, 1200), min_=True)]])
        self.assertEqual(APP._pid_window_rect(1), (10, 10, 1240, 880))

    def test_rect_helper_none_when_only_minimized(self):
        self._script([[_win((-32000, -32000, 1240, 880), min_=True)]])
        self.assertIsNone(APP._pid_window_rect(1))


class NoGhostWindowTest(unittest.TestCase):
    """关窗路径里不应该再出现「重开一个窗口当背景」的动作"""

    def test_no_reopen_left(self):
        for name in ("_reopen_behind", "_close_ghost"):
            self.assertFalse(hasattr(APP.WindowSupervisor, name),
                             f"{name} 会重开窗口，导致主窗口跳位和闪白")


if __name__ == "__main__":
    unittest.main()
