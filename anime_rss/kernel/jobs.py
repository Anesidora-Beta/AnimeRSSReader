"""定时任务：模块里不要再自己 ``threading.Thread``。

模块需要“每 N 秒做一次”时，写一行就够了：

    ctx.jobs.every(300, self._tick, name="hello.tick")

好处是收尾统一：进程退出时 ``jobs.stop()`` 会把这些线程一起停掉，
不会出现“窗口关了但后台还在抓”的僵尸任务；任务出错也只影响它自己，
并且能在 ``/api/jobs`` 里看到失败次数和最后一条错误。

实现用**单个调度线程 + 每次执行一个工作线程**：

* 单个调度线程：只有一处时间逻辑，不会每个任务各睡各的、互相漂移；
* 每次执行单独起线程：某个任务卡住（抓网络超时）不会把别的任务一起堵死；
* 同一任务上次还没跑完就跳过这一拍，避免堆积。
"""
import threading
import time

__all__ = ["Jobs", "Job"]


class Job:
    """一条定时任务。不要直接 new，用 ``Jobs.every()`` / ``Jobs.later()``。"""

    __slots__ = ("name", "fn", "interval", "next_at", "runs", "errors",
                 "last_at", "last_error", "running", "cancelled", "once")

    def __init__(self, name, fn, interval, next_at, once=False):
        self.name = name
        self.fn = fn
        self.interval = interval          # None = 只跑一次
        self.next_at = next_at            # monotonic 时间点
        self.runs = 0
        self.errors = 0
        self.last_at = 0.0                # wall clock，给界面看
        self.last_error = ""
        self.running = False
        self.cancelled = False
        self.once = once

    def to_dict(self, now=None, wall=None):
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        return {
            "name": self.name,
            "interval": self.interval,
            "once": self.once,
            "runs": self.runs,
            "errors": self.errors,
            "running": self.running,
            "cancelled": self.cancelled,
            "lastAt": int(self.last_at) if self.last_at else 0,
            "lastAgo": int(wall - self.last_at) if self.last_at else 0,
            "nextIn": max(0, round(self.next_at - now, 1)) if not self.cancelled else 0,
            "lastError": self.last_error,
        }

    def __repr__(self):
        return "<Job %s every=%s runs=%d>" % (self.name, self.interval, self.runs)


class Jobs:
    """任务表。``start()`` 之后由后台线程按时间点触发。"""

    MAX_WAIT = 300.0                      # 最长睡多久就醒一次（防止一直不醒）
    MIN_WAIT = 0.02

    def __init__(self, log=None, clock=time.monotonic):
        self._jobs = {}                   # name -> Job（保持插入顺序）
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._thread = None
        self._stop = threading.Event()
        self._workers = set()
        self._log = log
        self._clock = clock
        self._started_at = 0.0

    # ------------------------------------------------------------ 登记
    def every(self, seconds, fn, name=None, run_now=False):
        """每 ``seconds`` 秒跑一次 ``fn``。返回 Job（可当句柄读统计）。"""
        seconds = float(seconds)
        if seconds <= 0:
            raise ValueError("间隔必须大于 0 秒")
        return self._add(name, fn, seconds, 0.0 if run_now else seconds, once=False)

    def later(self, delay, fn, name=None):
        """``delay`` 秒之后跑一次。"""
        delay = float(delay)
        if delay < 0:
            raise ValueError("延迟不能为负")
        return self._add(name, fn, None, delay, once=True)

    def _add(self, name, fn, interval, delay, once):
        if not callable(fn):
            raise TypeError("任务必须可调用")
        key = name or getattr(fn, "__name__", "") or "job"
        with self._cond:
            old = self._jobs.get(key)
            if old is not None and not old.cancelled:
                raise ValueError("定时任务重名：%s（先 cancel 再登记）" % key)
            job = Job(key, fn, interval, self._clock() + delay, once=once)
            self._jobs[key] = job
            self._cond.notify_all()
            return job

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """启动调度线程；重复调用无副作用。"""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._started_at = time.time()
            t = threading.Thread(target=self._loop, name="jobs", daemon=True)
            self._thread = t
            t.start()
        return True

    def stop(self, timeout=3.0):
        """停调度线程并等工作线程收尾（超时就不等了，daemon 线程随进程退出）。"""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout)
        self.wait_idle(timeout)
        with self._cond:
            for job in self._jobs.values():
                job.cancelled = True
        return True

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 管理
    def cancel(self, name):
        with self._cond:
            job = self._jobs.get(name)
            if job is None or job.cancelled:
                return False
            job.cancelled = True
            self._cond.notify_all()
            return True

    def cancel_all(self):
        with self._cond:
            for job in self._jobs.values():
                job.cancelled = True
            self._cond.notify_all()

    def trigger(self, name):
        """立刻跑一次（不影响原来的节奏）；正在跑或已取消返回 False。"""
        with self._cond:
            job = self._jobs.get(name)
            if job is None or job.cancelled or job.running:
                return False
        self._dispatch(job)
        return True

    def get(self, name):
        with self._lock:
            return self._jobs.get(name)

    def names(self):
        with self._lock:
            return [n for n, j in self._jobs.items() if not j.cancelled]

    def status(self):
        now, wall = self._clock(), time.time()
        with self._lock:
            jobs = [j.to_dict(now, wall) for j in self._jobs.values() if not j.cancelled]
        jobs.sort(key=lambda d: d["name"])
        return {"ok": True, "running": self.running,
                "startedAt": int(self._started_at), "jobs": jobs}

    def __len__(self):
        return len(self.names())

    # ------------------------------------------------------------ 调度
    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._next_due()
                if job is None:
                    with self._cond:
                        self._cond.wait(self._wait_for())
                    continue
            except Exception as e:                    # noqa: BLE001 - 调度线程不能死
                self._say("调度出错：%s: %s" % (type(e).__name__, e))
                self._stop.wait(1.0)
                continue
            self._dispatch(job)

    def _next_due(self):
        now = self._clock()
        with self._lock:
            for job in self._jobs.values():
                if job.cancelled or job.running:
                    continue
                if job.next_at <= now:
                    return job
        return None

    def _wait_for(self):
        """距离下一个任务还有多久（秒），限制在 [MIN_WAIT, MAX_WAIT]。"""
        now = self._clock()
        waits = [j.next_at - now for j in self._jobs.values()
                 if not j.cancelled and not j.running]
        if not waits:
            return self.MAX_WAIT
        return max(self.MIN_WAIT, min(self.MAX_WAIT, min(waits)))

    def _dispatch(self, job):
        """起一个工作线程跑任务；返回线程对象，没起返回 None。"""
        with self._cond:
            if job.cancelled or job.running:
                return None
            job.running = True
        t = threading.Thread(target=self._execute, args=(job,),
                             name="job:%s" % job.name, daemon=True)
        with self._lock:
            self._workers.add(t)
        t.start()
        return t

    def _execute(self, job):
        ok, err = True, ""
        try:
            job.fn()
        except Exception as e:                        # noqa: BLE001 - 单个任务失败不影响别的
            ok = False
            err = "%s: %s" % (type(e).__name__, e)
            self._say("任务 %s 失败：%s" % (job.name, err))
        finally:
            with self._cond:
                job.running = False
                job.runs += 1
                job.last_at = time.time()
                if ok:
                    job.last_error = ""
                else:
                    job.errors += 1
                    job.last_error = err
                if job.once or job.cancelled:
                    job.cancelled = True
                else:
                    step = job.interval or 1.0
                    nxt = job.next_at + step
                    now = self._clock()
                    while nxt <= now:              # 错过太多拍就跳到下一拍，不补跑
                        nxt += step
                    job.next_at = nxt
                self._cond.notify_all()
            with self._lock:
                self._workers.discard(threading.current_thread())

    # ------------------------------------------------------------ 测试辅助
    def run_pending(self, now=None):
        """同步执行所有到期任务，返回执行条数（测试里代替等待）。"""
        now = self._clock() if now is None else now
        due = []
        with self._lock:
            for job in self._jobs.values():
                if not job.cancelled and not job.running and job.next_at <= now:
                    due.append(job)
        for job in due:
            t = self._dispatch(job)
            if t is not None:
                t.join(5.0)
        return len(due)

    def wait_idle(self, timeout=3.0):
        """等工作线程跑完，返回是否全部结束。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                alive = [t for t in self._workers if t.is_alive()]
                if not alive:
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
            alive[0].join(min(left, 0.1))

    # ------------------------------------------------------------ 内部
    def _say(self, msg):
        if self._log is not None:
            try:
                self._log(msg)
            except Exception:
                pass
