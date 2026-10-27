"""应用上下文：一次运行所需的全部部件，由组装层创建、传给各功能模块。

刻意保持“哑”——只有字段和几个纯计算，不含业务规则。
这样同一份业务代码既能跑在自己起的服务里，也能跑在宿主提供的环境里。
"""
import socket
import time


def local_ip():
    """取一个本机在局域网里的地址，用来给手机扫码/输入。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Context:
    __slots__ = ("store", "settings", "refresher", "schedule", "auto", "ui",
                 "assets", "router", "port", "lan_mode", "data_dir",
                 "runtime_dir", "static_dir", "app_name", "version",
                 "started_at", "extra",
                 "modules", "bus", "jobs", "views", "log")

    def __init__(self, store=None, settings=None, refresher=None, schedule=None,
                 auto=None, ui=None, assets=None, router=None, port=8899,
                 lan_mode=False, data_dir="", runtime_dir="", static_dir="",
                 app_name="", version="", started_at=None, bus=None, jobs=None,
                 views=None, log=None):
        self.store = store
        self.settings = settings
        self.refresher = refresher
        self.schedule = schedule
        self.auto = auto
        self.ui = ui
        self.assets = assets
        self.router = router
        self.port = port
        self.lan_mode = lan_mode
        self.data_dir = data_dir
        # 日志 / 备份 / 浏览器配置；没单独指定就跟用户数据放一起
        self.runtime_dir = runtime_dir or data_dir
        self.static_dir = static_dir
        self.app_name = app_name
        self.version = version
        self.started_at = started_at or time.time()
        self.extra = {}                       # 给后续模块留的扩展口袋
        self.modules = None                   # 由 load_modules() 填入注册表
        self.bus = bus                        # 事件总线（模块间通信）
        self.jobs = jobs                      # 定时任务（代替裸线程）
        self.views = views                    # 前端视图注册表
        self.log = log                        # 统一的日志函数（可为 None）

    def module(self, module_id, default=None):
        """按 id 取一个已加载模块；没有就返回 default，调用方不必判空。"""
        reg = self.modules
        return reg.get(module_id, default) if reg is not None else default

    # ---------------------------------------------------- 模块间通信的快捷方式
    def on(self, event, fn, once=False):
        """订阅事件（等价 ``ctx.bus.on``，但没有总线时返回 None 而不是崩）。"""
        return self.bus.on(event, fn, once=once) if self.bus is not None else None

    def emit(self, event, payload=None):
        """发事件；返回投递数，没有总线时是 0。"""
        return self.bus.emit(event, payload) if self.bus is not None else 0

    def every(self, seconds, fn, name=None, run_now=False):
        """登记定时任务（等价 ``ctx.jobs.every``）；没有任务表时返回 None。"""
        if self.jobs is None:
            return None
        return self.jobs.every(seconds, fn, name=name, run_now=run_now)

    def later(self, delay, fn, name=None):
        if self.jobs is None:
            return None
        return self.jobs.later(delay, fn, name=name)

    def view(self, view_id, default=None):
        """取一个已注册的前端视图声明。"""
        return self.views.get(view_id, default) if self.views is not None else default

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

    @property
    def db_path(self):
        import os
        return os.path.join(self.data_dir, "reader.db")

    def lan_url(self):
        return "http://%s:%d/" % (local_ip(), self.port)

    def base_url(self, req):
        return req.base_url(self.port)
