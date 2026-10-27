"""事件总线：模块之间唯一允许的通信方式。

三条规则，很短但很关键：

* 模块之间**不许互相 import**，要说话就 ``ctx.emit("thing.happened", {...})``；
* 订阅方只拿到 payload 字典，拿不到发布方的内部对象，改发布方不会连带炸订阅方；
* 一个订阅方抛异常**不影响**其它订阅方，也不影响发布方（记日志后跳过）。

``emit`` 是同步的：handler 里不要做慢操作（抓网络、睡等）。耗时的活交给
``ctx.every()``，或者让 handler 自己起线程。

事件名约定 ``命名空间.动作``：``item.new``、``sync.done``、``app.started``。
第三方自造前缀，不要占用别人的。
"""
import threading

__all__ = ["Bus", "Subscription"]


class Subscription:
    """一次订阅的句柄，``.cancel()`` 之后不再收到事件。"""

    __slots__ = ("event", "fn", "once", "cancelled", "_bus")

    def __init__(self, bus, event, fn, once=False):
        self._bus = bus
        self.event = event
        self.fn = fn
        self.once = once
        self.cancelled = False

    def cancel(self):
        if self.cancelled:
            return False
        self.cancelled = True
        self._bus.off(self.event, self.fn)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cancel()
        return False

    def __repr__(self):
        return "<Subscription %s%s>" % (self.event, " once" if self.once else "")


class Bus:
    """线程安全的事件总线，分发在锁外做（防止 handler 里再 emit 死锁）。"""

    WILDCARD = "*"

    def __init__(self, log=None):
        self._subs = {}                       # event -> [Subscription]
        self._lock = threading.RLock()
        self._log = log
        self._counts = {}                     # event -> 已分发次数
        self._errors = 0

    # ------------------------------------------------------------ 订阅
    def on(self, event, fn, once=False):
        """订阅事件。``event`` 写 ``"*"`` 表示收全部事件（handler 收两参）。"""
        if not callable(fn):
            raise TypeError("handler 必须可调用")
        sub = Subscription(self, event, fn, once)
        with self._lock:
            self._subs.setdefault(event, []).append(sub)
        return sub

    def once(self, event, fn):
        return self.on(event, fn, once=True)

    def off(self, event=None, fn=None):
        """取消订阅：都省略 = 清空。返回取消掉的条数。"""
        with self._lock:
            if event is None:
                n = sum(len(v) for v in self._subs.values())
                self._subs.clear()
                return n
            if fn is None:
                gone = self._subs.pop(event, [])
                for sub in gone:
                    sub.cancelled = True
                return len(gone)
            keep, gone = [], []
            for sub in self._subs.get(event, []):
                (gone if sub.fn is fn else keep).append(sub)
            if keep:
                self._subs[event] = keep
            else:
                self._subs.pop(event, None)
            for sub in gone:
                sub.cancelled = True
            return len(gone)

    # ------------------------------------------------------------ 发布
    def emit(self, event, payload=None):
        """同步分发，返回成功投递的订阅数（抛异常的算尝试过、不计成功）。"""
        if not isinstance(event, str) or not event:
            raise ValueError("事件名不能为空")
        with self._lock:
            subs = list(self._subs.get(event, ())) + \
                list(self._subs.get(self.WILDCARD, ()))
            self._counts[event] = self._counts.get(event, 0) + 1
            # 一次性订阅先摘掉，避免 handler 里再 emit 导致重复触发
            for sub in subs:
                if sub.once and not sub.cancelled:
                    sub.cancelled = True
                    bucket = self._subs.get(sub.event)
                    if bucket and sub in bucket:
                        bucket.remove(sub)
                    if bucket is not None and not bucket:
                        self._subs.pop(sub.event, None)

        delivered = 0
        for sub in subs:
            if sub.cancelled and not sub.once:
                continue
            try:
                if sub.event == self.WILDCARD:
                    sub.fn(event, payload)
                else:
                    sub.fn(payload)
                delivered += 1
            except Exception as e:            # noqa: BLE001 - 订阅方的问题不拖垮别人
                self._errors += 1
                self._say("事件 %s 的订阅者出错：%s: %s"
                          % (event, type(e).__name__, e))
        return delivered

    # ------------------------------------------------------------ 观察
    def count(self, event=None):
        with self._lock:
            if event is None:
                return sum(self._counts.values())
            return self._counts.get(event, 0)

    def events(self):
        with self._lock:
            return {k: len(v) for k, v in self._subs.items() if v}

    def status(self):
        with self._lock:
            return {
                "events": self.events(),
                "subscriptions": sum(len(v) for v in self._subs.values()),
                "emitted": sum(self._counts.values()),
                "errors": self._errors,
            }

    def subscribers(self, event):
        with self._lock:
            return [s.fn for s in self._subs.get(event, ())]

    def __len__(self):
        with self._lock:
            return sum(len(v) for v in self._subs.values())

    # ------------------------------------------------------------ 内部
    def _say(self, msg):
        if self._log is not None:
            try:
                self._log(msg)
            except Exception:
                pass
