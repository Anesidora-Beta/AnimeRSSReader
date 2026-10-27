"""arrkit —— 零依赖的模块化微内核。

给「一个进程、一堆功能模块、可能被别人嵌进去」的程序用。
四个能力 + 一条契约：

* 路由（:class:`~arrkit.http.Router`）：顺序匹配，handler 只认 Request/Response；
* 事件（:class:`~arrkit.bus.Bus`）：模块之间只走事件，不互相 import；
* 任务（:class:`~arrkit.jobs.Jobs`）：``ctx.every()`` 代替裸线程，退出统一收尾；
* 视图（:class:`~arrkit.views.ViewRegistry`）：模块自带前端入口，内核不知道界面长什么样；
* 契约（:class:`~arrkit.module.Module`）：setup / bootstrap / apply_settings / teardown。

端口与适配器分开（``arrkit.ports`` / ``arrkit.adapters``），换传输层或资源来源
不用改内核。

最小用法::

    from arrkit import Module, create_kernel

    class Hello(Module):
        id, name, version = "hello", "问候", "0.1.0"
        def setup(self, ctx):
            ctx.router.get("/api/hello", lambda req: {"ok": True})

    k = create_kernel(modules=[Hello], port=8899)
    k.serve()
"""
from .version import API_VERSION, __version__

from .adapters import FakeClock, FilesystemAssets, MemoryAssets, StdlibTransport, SystemClock
from .admin import AdminApi
from .bus import Bus, Subscription
from .context import Context, local_ip
from .dispatcher import Dispatcher, Mount
from .discovery import discover, merge_unique
from .hosting import export, wsgi_app
from .http import HTML, JSON, TEXT, Request, Response, Router
from .jobs import Job, Jobs
from .kernel import Kernel, KernelConfig, create_kernel
from .module import (Module, ModuleError, ModuleRegistry, load_modules,
                     unload_modules)
from .ports import AssetPort, ClockPort, TransportPort
from .settings import SettingError, Settings
from .testing import Client
from .views import View, ViewError, ViewRegistry

__all__ = [
    "__version__", "API_VERSION",
    # 原语
    "Request", "Response", "Router", "JSON", "TEXT", "HTML",
    "Dispatcher", "Mount",
    # 能力
    "Bus", "Subscription", "Jobs", "Job", "Settings", "SettingError",
    "View", "ViewRegistry", "ViewError",
    # 契约与组装
    "Module", "ModuleRegistry", "ModuleError", "load_modules", "unload_modules",
    "Kernel", "KernelConfig", "create_kernel", "Context", "local_ip",
    "AdminApi",
    # 发现与宿主接入
    "discover", "merge_unique", "export", "wsgi_app",
    # 端口 / 适配器
    "AssetPort", "ClockPort", "TransportPort",
    "FilesystemAssets", "MemoryAssets", "StdlibTransport",
    "SystemClock", "FakeClock",
    # 测试
    "Client",
]
