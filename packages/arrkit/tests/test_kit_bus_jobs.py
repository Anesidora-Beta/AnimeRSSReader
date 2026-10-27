import unittest

from arrkit.adapters import FakeClock
from arrkit.bus import Bus
from arrkit.jobs import Jobs


class BusTest(unittest.TestCase):
    def test_emit_and_payload(self):
        bus, got = Bus(), []
        bus.on("a.b", lambda p: got.append(p))
        self.assertEqual(bus.emit("a.b", {"n": 1}), 1)
        self.assertEqual(got, [{"n": 1}])

    def test_once(self):
        bus, got = Bus(), []
        bus.once("x", lambda p: got.append(1))
        bus.emit("x")
        bus.emit("x")
        self.assertEqual(got, [1])

    def test_wildcard_gets_name_and_payload(self):
        bus, got = Bus(), []
        bus.on("*", lambda e, p: got.append((e, p)))
        bus.emit("one", 1)
        bus.emit("two", 2)
        self.assertEqual(got, [("one", 1), ("two", 2)])

    def test_subscriber_error_does_not_break_others(self):
        bus, got = Bus(), []

        def boom(_):
            raise RuntimeError("坏了")

        bus.on("e", boom)
        bus.on("e", lambda p: got.append(p))
        self.assertEqual(bus.emit("e", 1), 1)     # 只有成功的那一个计数
        self.assertEqual(got, [1])
        self.assertEqual(bus.status()["errors"], 1)

    def test_cancel_and_off(self):
        bus, got = Bus(), []
        sub = bus.on("e", lambda p: got.append(p))
        self.assertTrue(sub.cancel())
        self.assertFalse(sub.cancel())
        bus.emit("e")
        self.assertEqual(got, [])

        bus.on("e", lambda p: got.append(2))
        self.assertEqual(bus.off("e"), 1)
        self.assertEqual(len(bus), 0)
        bus.on("z", lambda p: None)
        self.assertEqual(bus.off(), 1)

    def test_empty_event_rejected(self):
        with self.assertRaises(ValueError):
            Bus().emit("")

    def test_handler_must_be_callable(self):
        with self.assertRaises(TypeError):
            Bus().on("e", None)


class JobsTest(unittest.TestCase):
    def test_every_runs_and_reschedules(self):
        clock, jobs, hits = FakeClock(), None, []
        jobs = Jobs(clock=clock)
        job = jobs.every(10, lambda: hits.append(1), name="t")
        self.assertEqual(job.interval, 10)
        clock.advance(10)
        self.assertEqual(jobs.run_pending(), 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(job.runs, 1)
        clock.advance(10)
        jobs.run_pending()
        self.assertEqual(len(hits), 2)

    def test_later_runs_once(self):
        clock = FakeClock()
        jobs = Jobs(clock=clock)
        hits = []
        jobs.later(3, lambda: hits.append(1), name="once")
        jobs.run_pending()
        self.assertEqual(hits, [])                # 还没到点
        clock.advance(3)
        jobs.run_pending()
        clock.advance(100)
        jobs.run_pending()
        self.assertEqual(hits, [1])
        self.assertEqual(jobs.names(), [])

    def test_duplicate_name_rejected(self):
        jobs = Jobs()
        jobs.every(5, lambda: None, name="dup")
        with self.assertRaises(ValueError):
            jobs.every(5, lambda: None, name="dup")
        jobs.cancel("dup")
        jobs.every(5, lambda: None, name="dup")   # 取消后可以重用名字

    def test_bad_arguments(self):
        jobs = Jobs()
        with self.assertRaises(ValueError):
            jobs.every(0, lambda: None)
        with self.assertRaises(ValueError):
            jobs.later(-1, lambda: None)
        with self.assertRaises(TypeError):
            jobs.every(1, "not callable")

    def test_error_is_recorded_not_raised(self):
        clock = FakeClock()
        jobs = Jobs(clock=clock)

        def boom():
            raise ValueError("炸了")

        job = jobs.every(1, boom, name="bad")
        clock.advance(1)
        jobs.run_pending()                        # 不该抛
        self.assertEqual(job.errors, 1)
        self.assertIn("炸了", job.last_error)

    def test_skip_while_running(self):
        clock = FakeClock()
        jobs = Jobs(clock=clock)
        blocked = []

        def slow():
            clock.advance(100)                    # 假装跑了很久
            blocked.append(1)

        job = jobs.every(1, slow, name="slow")
        clock.advance(1)
        jobs.run_pending()
        self.assertEqual(job.runs, 1)

    def test_trigger_and_status(self):
        jobs = Jobs()
        hits = []
        jobs.every(3600, lambda: hits.append(1), name="manual")
        self.assertTrue(jobs.trigger("manual"))
        jobs.wait_idle(2)
        self.assertEqual(hits, [1])
        st = jobs.status()
        self.assertEqual(st["jobs"][0]["name"], "manual")
        self.assertFalse(jobs.trigger("nope"))

    def test_start_stop(self):
        jobs = Jobs()
        jobs.every(3600, lambda: None, name="idle")
        self.assertTrue(jobs.start())
        self.assertFalse(jobs.start())            # 重复启动无副作用
        self.assertTrue(jobs.running)
        jobs.stop(1)
        self.assertFalse(jobs.running)


if __name__ == "__main__":
    unittest.main()
