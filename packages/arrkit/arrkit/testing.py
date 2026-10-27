"""测试工具：不走网络也能像发 HTTP 一样打内核。

    k = create_kernel(modules=[Clock])
    c = Client(k)
    c.get("/_kit/health").json["ok"]        # True
    c.post("/api/clock/tick", {}).status    # 200
"""
import json

from .http import Request, Response

__all__ = ["Client", "Reply"]


class Reply:
    __slots__ = ("status", "body", "headers", "ctype")

    def __init__(self, resp):
        self.status = resp.status
        self.body = resp.body
        self.headers = resp.headers
        self.ctype = resp.ctype

    @property
    def text(self):
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    @property
    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}

    def __repr__(self):
        return "<Reply %d %dB>" % (self.status, len(self.body))


class Client:
    """把 kernel.handle() 包成顺手的样子。"""

    def __init__(self, kernel, headers=None):
        self.kernel = kernel
        self.headers = dict(headers or {})

    def request(self, method, path, body=None, headers=None, query=None):
        merged = dict(self.headers)
        merged.update(headers or {})
        resp = self.kernel.handle(method, path, body=body, headers=merged, query=query)
        return Reply(resp) if isinstance(resp, Response) else Reply(
            Response.json(resp or {}))

    def get(self, path, query=None, headers=None):
        return self.request("GET", path, query=query, headers=headers)

    def post(self, path, body=None, headers=None):
        return self.request("POST", path, body=body, headers=headers)

    def patch(self, path, body=None, headers=None):
        return self.request("PATCH", path, body=body, headers=headers)

    def delete(self, path, body=None, headers=None):
        return self.request("DELETE", path, body=body, headers=headers)

    def json(self, method, path, body=None):
        reply = self.request(method, path, body=body)
        return reply.status, reply.json

    def __repr__(self):
        return "<Client %r>" % (self.kernel,)
