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
                 "static_dir", "app_name", "version", "started_at", "extra")

    def __init__(self, store=None, settings=None, refresher=None, schedule=None,
                 auto=None, ui=None, assets=None, router=None, port=8899,
                 lan_mode=False, data_dir="", static_dir="", app_name="",
                 version="", started_at=None):
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
        self.static_dir = static_dir
        self.app_name = app_name
        self.version = version
        self.started_at = started_at or time.time()
        self.extra = {}                       # 给后续模块留的扩展口袋

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
