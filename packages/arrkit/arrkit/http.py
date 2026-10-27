"""HTTP 原语：Request / Response / Router。

这一层不认识任何服务器实现，也不认识任何业务。
它是「同一份 handler 既能跑在自带服务上，也能跑在宿主框架里」的技术前提。
"""
import json
import urllib.parse

JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"

__all__ = ["Request", "Response", "Router", "JSON", "TEXT", "HTML"]


class Request:
    """一次请求的只读视图。handler 只依赖它。"""

    __slots__ = ("method", "path", "segments", "query", "headers", "_body", "params")

    def __init__(self, method, path, query=None, headers=None, body=b""):
        self.method = (method or "GET").upper()
        self.path = path or "/"
        self.segments = [s for s in self.path.strip("/").split("/") if s != ""]
        self.query = query or {}
        self.headers = headers or {}
        self._body = body or b""
        self.params = {}                      # 由 Router 在匹配后填入

    # ------------------------------------------------------------ 取参
    def q(self, name, default=""):
        v = self.query.get(name)
        return default if v is None or v == "" else v

    def qi(self, name, default=0):
        try:
            return int(self.q(name, default))
        except (TypeError, ValueError):
            return default

    def param(self, name, default=""):
        v = self.params.get(name)
        return default if v is None else v

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

    # ------------------------------------------------------------ 便捷构造
    @classmethod
    def json(cls, obj, status=200, cache="no-store"):
        return cls(obj, JSON, status, cache)

    @classmethod
    def text(cls, s, status=200, cache="no-store"):
        return cls(s, TEXT, status, cache)

    @classmethod
    def html(cls, s, status=200, cache="no-store"):
        return cls(s, HTML, status, cache)

    @classmethod
    def err(cls, msg, status=400):
        return cls.json({"ok": False, "error": msg}, status)

    @classmethod
    def redirect(cls, location, status=302):
        return cls(b"", TEXT, status, headers={"Location": location})

    def with_headers(self, **kw):
        merged = dict(self.headers)
        merged.update(kw)
        return Response(self.body, self.ctype, self.status, self.cache, merged)

    def __repr__(self):
        return "<Response %d %s %dB>" % (self.status, self.ctype.split(";")[0],
                                         len(self.body))


def _split(pattern):
    p = (pattern or "/").strip("/")
    return [] if not p else p.split("/")


def _match(pattern, segments):
    """匹配路径片段，返回参数字典；不匹配返回 None。

    支持 ``{name}``（一段）与 ``{name:path}``（吃掉剩余全部）。
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
    """顺序匹配的路由表：注册顺序即优先级。"""

    def __init__(self):
        self.routes = []                      # [(method, [seg...], fn)]

    # ------------------------------------------------------------ 注册
    def add(self, method, pattern, fn):
        self.routes.append(((method or "GET").upper(), _split(pattern), fn))
        return fn

    def get(self, pattern, fn=None):
        return self._maybe("GET", pattern, fn)

    def post(self, pattern, fn=None):
        return self._maybe("POST", pattern, fn)

    def put(self, pattern, fn=None):
        return self._maybe("PUT", pattern, fn)

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

    # ------------------------------------------------------------ 匹配
    def match(self, req):
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
        result = fn(req)
        # 允许 handler 偷懒直接返回 dict / list / str，省掉一层 Response 包装
        if result is None or isinstance(result, Response):
            return result
        if isinstance(result, (dict, list)):
            return Response.json(result)
        return Response.text(str(result))

    # ------------------------------------------------------------ 观察
    def paths(self):
        return ["%s %s" % (m, "/" + "/".join(p)) for m, p, _ in self.routes]

    def find(self, method, path):
        """按方法+路径找 handler（给宿主挂路由用），找不到返回 None。"""
        segments = _split(path)
        for m, pattern, fn in self.routes:
            if m != (method or "GET").upper():
                continue
            if _match(pattern, segments) is not None:
                return fn
        return None

    def __len__(self):
        return len(self.routes)
