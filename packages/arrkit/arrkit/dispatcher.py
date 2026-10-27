"""派发：先查静态挂载，再查路由表，最后 404。

属于内核逻辑（不认识具体服务器），因此和传输适配器分开。
"""
import urllib.parse

from .http import Request, Response

__all__ = ["Dispatcher", "Mount"]


class Mount:
    """把某个目录挂到某个 URL 前缀下。"""

    def __init__(self, prefix, directory, cache="public, max-age=86400",
                 index="index.html", transform=None):
        self.prefix = prefix if prefix.endswith("/") else prefix + "/"
        self.directory = directory
        self.cache = cache
        self.index = index
        self.transform = transform

    def match(self, path):
        """命中返回文件名，否则 None。"""
        if not path.startswith(self.prefix):
            return None
        return path[len(self.prefix):] or self.index

    def __repr__(self):
        return "<Mount %s -> %s>" % (self.prefix, self.directory)


class Dispatcher:
    """把 (method, path, headers, body) 变成 Response。

    这是内核唯一的「入站入口」：无论请求来自 stdlib 服务、WSGI、还是宿主
    直接调用 ``kernel.handle()``，最后都落到这里。
    """

    def __init__(self, router=None, assets=None, mounts=None):
        self.router = router
        self.assets = assets
        self.mounts = list(mounts or [])

    def handle(self, method, raw_path, headers, body=b""):
        u = urllib.parse.urlsplit(raw_path or "/")
        path = u.path or "/"

        for mount in self.mounts:
            name = mount.match(path)
            if name is None:
                continue
            data = self.assets.read(mount.directory, name)
            if data is None:
                return Response.text("Not Found", 404)
            if mount.transform is not None:
                data = mount.transform(name, data)
            return Response(data, self.assets.content_type(name), cache=mount.cache)

        if self.router is not None:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            req = Request(method, path.rstrip("/") or "/", query, headers, body)
            resp = self.router.dispatch(req)
            if resp is not None:
                return resp

        return Response.text("Not Found", 404)

    def __call__(self, method, raw_path, headers, body=b""):
        return self.handle(method, raw_path, headers, body)
