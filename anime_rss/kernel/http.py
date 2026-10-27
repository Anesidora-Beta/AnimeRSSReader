"""HTTP 原语：Request / Response / Router。

这层不依赖任何具体服务器实现，既能被自带的 stdlib 服务用，
也能被宿主（Flask / FastAPI / 自定义）适配 —— 这是「本项目可作为别人的插件」
那一条约束的技术前提。
"""
import json
import urllib.parse

JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"


class Request:
    """一次请求的只读视图。handler 只依赖它，不依赖 BaseHTTPRequestHandler。"""

    __slots__ = ("method", "path", "segments", "query", "headers", "_body", "params")

    def __init__(self, method, path, query=None, headers=None, body=b""):
        self.method = method.upper()
        self.path = path
        self.segments = [s for s in path.strip("/").split("/") if s != ""]
        self.query = query or {}
        self.headers = headers or {}
        self._body = body or b""
        self.params = {}                      # 由 Router 在匹配后填入

    @property
    def body(self):
        """原始请求体（上传备份文件时是文件字节，不是 JSON）"""
        return self._body

    def q(self, name, default=""):
        v = self.query.get(name)
        return default if v is None or v == "" else v

    def json_body(self):
        if not self._body:
            return {}
        try:
            data = json.loads(self._body.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def base_url(self, default_port=0):
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host")
                or "127.0.0.1:%d" % default_port)
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return "%s://%s" % (proto, host)

    def __repr__(self):
        return "<Request %s %s>" % (self.method, self.path)


class Response:
    """handler 的返回值。只描述「回什么」，不负责「怎么写」。"""

    __slots__ = ("body", "ctype", "status", "cache", "headers")

    def __init__(self, body=b"", ctype=JSON, status=200, cache="no-store", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body or b""
        self.ctype = ctype
        self.status = status
        self.cache = cache
        self.headers = headers or {}

    @classmethod
    def json(cls, obj, status=200, cache="no-store"):
        return cls(obj, JSON, status, cache)

    @classmethod
    def text(cls, s, status=200, cache="no-store"):
        return cls(s, TEXT, status, cache)

    @classmethod
    def err(cls, msg, status=400):
        return cls.json({"ok": False, "error": msg}, status)

    def with_headers(self, **kw):
        merged = dict(self.headers)
        merged.update(kw)
        return Response(self.body, self.ctype, self.status, self.cache, merged)


def _split(pattern):
    p = pattern.strip("/")
    return [] if not p else p.split("/")


def _match(pattern, segments):
    """匹配路径片段，返回参数字典；不匹配返回 None。

    支持两种占位符：``{name}`` 匹配一段，``{name:path}`` 吃掉剩余全部。
    """
    params = {}
    for i, part in enumerate(pattern):
        if part.startswith("{") and part.endswith("}"):
            spec = part[1:-1]
            name, _, kind = spec.partition(":")
            if kind == "path":
                rest = segments[i:]
                if not rest:
                    return None
                params[name] = "/".join(rest)
                return params
            if i >= len(segments):
                return None
            params[name] = urllib.parse.unquote(segments[i])
        else:
            if i >= len(segments) or segments[i] != part:
                return None
    return params if len(segments) == len(pattern) else None


class Router:
    """顺序匹配的路由表。注册顺序即优先级。"""

    def __init__(self):
        self.routes = []                      # (method, pattern, fn)

    def add(self, method, pattern, fn):
        self.routes.append((method.upper(), _split(pattern), fn))
        return fn

    def get(self, pattern, fn=None):
        return self._maybe("GET", pattern, fn)

    def post(self, pattern, fn=None):
        return self._maybe("POST", pattern, fn)

    def patch(self, pattern, fn=None):
        return self._maybe("PATCH", pattern, fn)

    def delete(self, pattern, fn=None):
        return self._maybe("DELETE", pattern, fn)

    def _maybe(self, method, pattern, fn):
        if fn is None:                        # 当装饰器用
            def deco(f):
                self.add(method, pattern, f)
                return f
            return deco
        return self.add(method, pattern, fn)

    def match(self, req):
        """返回 (fn, params) 或 None。"""
        method = "GET" if req.method == "HEAD" else req.method
        for m, pattern, fn in self.routes:
            if m != method:
                continue
            params = _match(pattern, req.segments)
            if params is not None:
                return fn, params
        return None

    def dispatch(self, req):
        hit = self.match(req)
        if hit is None:
            return None
        fn, params = hit
        req.params = params
        return fn(req)

    def paths(self):
        return ["%s %s" % (m, "/" + "/".join(p)) for m, p, _ in self.routes]
