"""应用上下文：一次运行所需的全部部件，由内核创建、传给各模块。

刻意保持「哑」——只有字段和几个纯计算，不含任何业务规则。
这样同一份业务代码既能跑在自带服务里，也能跑在宿主提供的环境里。
"""
import socket
import time

__all__ = ["Context", "local_ip"]


def local_ip():
    """取本机在局域网里的地址（给手机扫码/输入用）。取不到就回环。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


class Context:
    __slots__ = ("router", "assets", "bus", "jobs", "views", "settings",
                 "config", "services", "data_dir", "static_dir", "app_name",
                 "version", "port", "host", "started_at", "extra", "modules",
                 "log", "clock")

    def __init__(self, router=None, assets=None, bus=None, jobs=None, views=None,
                 settings=None, config=None, services=None, data_dir="",
                 static_dir="", app_name="", version="", port=0,
                 host="127.0.0.1", started_at=None, log=None, clock=None):
        self.router = router
        self.assets = assets
        self.bus = bus
        self.jobs = jobs
        self.views = views
        self.settings = settings
        self.config = config
        self.services = dict(services or {})   # 宿主注入的其它能力（db、mail…）
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.app_name = app_name
        self.version = version
        self.port = port
        self.host = host
        self.started_at = started_at or time.time()
        self.extra = {}                        # 模块之间共享的临时口袋
        self.modules = None                    # 由 load_modules() 填入注册表
        self.log = log
        self.clock = clock

    # ------------------------------------------------------------ 取模块
    def module(self, module_id, default=None):
        """按 id 取一个已加载模块；没有就返回 default，调用方不必判空。"""
        reg = self.modules
        return reg.get(module_id, default) if reg is not None else default

    def service(self, name, default=None):
        """取宿主/其它模块登记的服务对象（``ctx.services["db"]``）。"""
        return (self.services or {}).get(name, default)

    # ------------------------------------------------------------ 模块间通信
    def on(self, event, fn, once=False):
        """订阅事件（等价 ``ctx.bus.on``，没有总线时返回 None 而不是崩）。"""
        return self.bus.on(event, fn, once=once) if self.bus is not None else None

    def emit(self, event, payload=None):
        """发事件；返回投递数，没有总线时是 0。"""
        return self.bus.emit(event, payload) if self.bus is not None else 0

    def every(self, seconds, fn, name=None, run_now=False):
        """登记定时任务；没有任务表时返回 None。"""
        if self.jobs is None:
            return None
        return self.jobs.every(seconds, fn, name=name, run_now=run_now)

    def later(self, delay, fn, name=None):
        if self.jobs is None:
            return None
        return self.jobs.later(delay, fn, name=name)

    def view(self, view_id, default=None):
        return self.views.get(view_id, default) if self.views is not None else default

    # ------------------------------------------------------------ 杂项
    def say(self, msg):
        """统一日志出口：宿主给了 log 就用它的，否则静默。"""
        if self.log is not None:
            try:
                self.log(msg)
            except Exception:
                pass

    @property
    def uptime(self):
        return int(time.time() - self.started_at)

    def lan_url(self):
        return "http://%s:%d/" % (local_ip(), self.port)

    def base_url(self, req):
        return req.base_url(self.port)

    def __repr__(self):
        names = self.modules.ids() if self.modules is not None else []
        return "<Context %s modules=%s>" % (self.app_name or "arrkit", names)
