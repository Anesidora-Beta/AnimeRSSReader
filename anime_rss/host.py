"""方向 B：把这个项目当成别人的插件接进去。

宿主要么接管整张路由表，要么只用 WSGI，两种都很短：

```python
# ① 宿主有自己的路由表
from anime_rss.host import export
app = export(my_host, data_dir="./data/anime")
...
app.stop()

# ② 宿主是 Flask（任何 WSGI 容器同理）
from anime_rss.host import wsgi_app
site = wsgi_app(data_dir="./data/anime")
app.add_url_rule("/anime/<path:p>", "anime", lambda p: ...)
```

宿主对象只要实现下面任意一个方法即可：

* ``mount(router)``           整个 ``Router`` 交给你，自己遍历
* ``add_route(method, path, fn)``
* ``route(method, path, fn)``

都没有就抛 ``TypeError`` —— 与其猜，不如让调用方明确写清楚。
"""
from .app import API_PREFIX, App, AppConfig, create_app

__all__ = ["export", "wsgi_app", "create_app", "App", "AppConfig", "API_PREFIX"]


def export(host, **config):
    """把路由注册进宿主，返回 App（宿主关停时记得调 ``app.stop()``）。

    默认会启动任务表（自动刷新等）；只想挂路由就用 ``start=False``。
    """
    start = config.pop("start", True)
    app = create_app(**config)
    if host is None:
        if start:
            app.start()
        return app

    routes = [("/" + "/".join(pattern), method, fn)
              for method, pattern, fn in app.router.routes]

    if callable(getattr(host, "mount", None)):
        host.mount(app.router)
    elif callable(getattr(host, "add_route", None)):
        for path, method, fn in routes:
            host.add_route(method, path, fn)
    elif callable(getattr(host, "route", None)):
        for path, method, fn in routes:
            host.route(method, path, fn)
    else:
        app.stop()
        raise TypeError("宿主需要 mount(router) / add_route(method, path, fn) / "
                        "route(method, path, fn) 之一；纯 WSGI 宿主请用 wsgi_app()")

    if start:
        app.start()
    return app


def wsgi_app(**config):
    """返回一个 WSGI callable。挂到哪个前缀由宿主决定，路由内部仍写 /api/...。

    返回的函数带 ``.app``（App 实例）与 ``.stop()``，宿主关停时调一下即可。
    """
    config.setdefault("fetch_on_start", False)
    app = create_app(**config)

    def wsgi(environ, start_response):
        return app.wsgi(environ, start_response)

    wsgi.app = app
    wsgi.stop = app.stop
    return wsgi


def demo():                                    # pragma: no cover - 手工跑着看
    """10 行宿主的样子：直接用内核的 handle()，不引入任何框架。"""
    import json
    import tempfile

    app = create_app(data_dir=tempfile.mkdtemp(prefix="arr-host-"))
    try:
        resp = app.handle("GET", "/api/v1/health")
        print(resp.status, json.loads(resp.body))
        resp = app.handle("GET", "/api/v1/items?limit=1")
        print(resp.status, len(json.loads(resp.body).get("items") or []), "条")
    finally:
        app.stop()


if __name__ == "__main__":                     # python -m anime_rss.host
    demo()
