"""把内核挂进别人的程序：两种常规姿态。

```python
# ① 宿主有自己的路由表
from arrkit.hosting import export
k = export(my_host, app_name="我的平台", modules=[Clock])

# ② 宿主是 Flask / Django / gunicorn（任何 WSGI 容器）
from arrkit.hosting import wsgi_app
site = wsgi_app(app_name="我的平台", modules=[Clock])
app.add_url_rule("/anime/<path:p>", "anime", lambda p: site(...))
```

宿主对象只要实现下面任意一个方法即可：

* ``mount(router)``                整个 Router 交给你，自己遍历
* ``add_route(method, path, fn)``
* ``route(method, path, fn)``

都没有就抛 ``TypeError`` —— 与其猜，不如让调用方明确写清楚。
"""
from .http import Router
from .kernel import Kernel, KernelConfig, create_kernel

__all__ = ["export", "wsgi_app", "create_kernel", "Kernel", "KernelConfig"]


def export(host, config=None, start=True, **kwargs):
    """把内核路由注册进宿主，返回 Kernel（宿主关停时记得调 ``k.stop()``）。"""
    kernel = create_kernel(config, **kwargs)

    if host is None:
        if start:
            kernel.start()
        return kernel

    routes = [("/" + "/".join(pattern), method, fn)
              for method, pattern, fn in kernel.router.routes]

    try:
        if callable(getattr(host, "mount", None)):
            host.mount(kernel.router)
        elif callable(getattr(host, "add_route", None)):
            for path, method, fn in routes:
                host.add_route(method, path, fn)
        elif callable(getattr(host, "route", None)):
            for path, method, fn in routes:
                host.route(method, path, fn)
        else:
            raise TypeError(
                "宿主需要 mount(router) / add_route(method, path, fn) / "
                "route(method, path, fn) 之一；纯 WSGI 宿主请用 wsgi_app()")
    except Exception:
        kernel.stop()
        raise

    if start:
        kernel.start()
    return kernel


def wsgi_app(config=None, **kwargs):
    """返回一个 WSGI callable，带 ``.kernel`` 与 ``.stop()``。"""
    kernel = create_kernel(config, **kwargs)

    def wsgi(environ, start_response):
        return kernel.wsgi(environ, start_response)

    wsgi.kernel = kernel
    wsgi.stop = kernel.stop
    return wsgi


def router_of(kernel):
    """给「宿主想自己挑几条路由挂」的场景用。"""
    return kernel.router if isinstance(kernel.router, Router) else kernel.router
