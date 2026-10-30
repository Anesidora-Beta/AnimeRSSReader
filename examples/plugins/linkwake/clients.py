"""把链接直接塞给下载器：qBittorrent Web API 与 aria2 JSON-RPC。

只依赖 ``urllib``（在发布版 exe 里实测可用），不引第三方库 —— 插件里多一个
第三方依赖，就等于给「exe 里没有这个包」多开一扇门。

为什么要这两个
--------------
磁力链接「交给系统默认程序」有时候会落到一个没在跑的客户端上，或者干脆
没有默认程序。而 qB / aria2 都是**有 HTTP 接口**的，能确认「真的加进去了」，
失败还能拿到原因。这是唯一能给出可靠反馈的方式。

两类客户端的差别（代码里体现得很清楚）：

* qB 是「会话式」—— 先 ``/auth/login`` 拿 SID，再带着 Cookie 调接口；
* aria2 是「一请求一调用」—— JSON-RPC，token 放在 params 第一个位置。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["ClientError", "qb_test", "qb_add", "aria2_test", "aria2_add",
           "check_endpoint"]


class ClientError(RuntimeError):
    """下载器没连上 / 拒绝了请求。消息是给人看的。"""


def check_endpoint(url, name="下载器"):
    """校验用户填的接口地址。只接受 http/https，其余直接拒。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ClientError("还没填 %s 地址" % name)
    low = u.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ClientError("%s 地址要以 http:// 或 https:// 开头" % name)
    return u


def _host_of(url):
    """从 URL 里取主机名（不带端口）。解析失败返回空串。"""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return ""
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):                 # [::1]:6800
        return host.split("]")[0].lstrip("[").lower()
    return host.rsplit(":", 1)[0].lower() if ":" in host else host.lower()


def _is_private_host(host):
    """本机 / 内网地址。这些地址绝不能走系统代理。"""
    if not host:
        return False
    if host in ("localhost", "::1", "0.0.0.0") or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return (a == 127 or a == 10
            or (a == 192 and b == 168)
            or (a == 172 and 16 <= b <= 31)
            or (a == 169 and b == 254))


# 绕过代理想法的实现放在这里：环境里常有 http_proxy（企业网络、抓包工具、
# 甚至某些 IDE 自己设的），而 qB / aria2 通常就跑在 127.0.0.1 上。不绕过的
# 结果是「明明客户端在跑，却报 502」—— 这个坑踩过一次，别再踩。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(url):
    return _NO_PROXY if _is_private_host(_host_of(url)) else urllib.request


def _fetch(url, data=None, headers=None, timeout=8):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, e.headers
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        text = str(reason).lower()
        if "refused" in text or "10061" in text or "积极拒绝" in str(reason):
            raise ClientError("连不上，端口没人监听（客户端启动了吗？）") from e
        if "timed out" in text or "timeout" in text or "10060" in text:
            raise ClientError("连接超时，检查地址与端口") from e
        raise ClientError("连不上：%s" % reason) from e
    except OSError as e:
        raise ClientError("连接失败：%s" % e) from e


# --------------------------------------------------------------- qBittorrent
def _qb_login(base, user, password, timeout=8):
    """登录拿 SID。qB 的登录接口必须带 Referer，否则 403。"""
    payload = urllib.parse.urlencode(
        {"username": user or "", "password": password or ""}).encode()
    status, body, headers = _fetch(
        base + "/api/v2/auth/login", data=payload, timeout=timeout,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": base + "/", "User-Agent": "arr-linkwake"})
    text = body.decode("utf-8", "replace").strip()
    if status == 403:
        raise ClientError("qBittorrent 拒绝了登录（403）。"
                          "常见原因：开启了「对本地主机免除认证」之外的校验，"
                          "或 IP 被临时封禁，看一眼 qB 的日志")
    if status != 200:
        raise ClientError("qBittorrent 登录失败：HTTP %s" % status)
    if text.lower().startswith("fail") or text == "":
        raise ClientError("qBittorrent 账号或密码不对")
    cookie = (headers.get("Set-Cookie") or "").split(";")[0].strip()
    if not cookie or "SID=" not in cookie:
        # 开了「免认证」时不会下发 SID，后续请求不带 Cookie 也能用
        cookie = ""
    return cookie


def qb_test(base, user="", password="", timeout=8):
    """连通性测试：返回 qB 的版本号。"""
    b = check_endpoint(base, "qBittorrent")
    cookie = _qb_login(b, user, password, timeout=timeout)
    headers = {"Referer": b + "/", "User-Agent": "arr-linkwake"}
    if cookie:
        headers["Cookie"] = cookie
    status, body, _ = _fetch(b + "/api/v2/app/version", headers=headers,
                             timeout=timeout)
    if status != 200:
        raise ClientError("qBittorrent 版本查询失败：HTTP %s" % status)
    return body.decode("utf-8", "replace").strip() or "未知版本"


def qb_add(base, urls, savepath="", user="", password="", timeout=15):
    """把一批链接丢给 qB，返回提交的条数。

    一次请求可以提交多行 ``urls`` —— qB 会逐条加进任务列表，
    所以「全选 → 发到 qB」只发一次请求。
    """
    b = check_endpoint(base, "qBittorrent")
    items = [u.strip() for u in (urls or []) if str(u).strip()]
    if not items:
        raise ClientError("没有要提交的链接")
    cookie = _qb_login(b, user, password, timeout=timeout)
    fields = {"urls": "\n".join(items)}
    if savepath:
        fields["savepath"] = savepath
    payload = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Referer": b + "/", "User-Agent": "arr-linkwake"}
    if cookie:
        headers["Cookie"] = cookie
    status, body, _ = _fetch(b + "/api/v2/torrents/add", data=payload,
                             headers=headers, timeout=timeout)
    text = body.decode("utf-8", "replace").strip()
    if status != 200:
        raise ClientError("qBittorrent 添加失败：HTTP %s" % status)
    if text.lower().startswith("fail"):
        raise ClientError("qBittorrent 拒绝了这批链接（格式不支持或已存在）")
    return len(items)


# ------------------------------------------------------------------- aria2
def _aria2_call(rpc, payload, timeout=12):
    data = json.dumps(payload).encode()
    status, body, _ = _fetch(rpc, data=data, timeout=timeout,
                             headers={"Content-Type": "application/json",
                                      "User-Agent": "arr-linkwake"})
    if status != 200:
        raise ClientError("aria2 返回 HTTP %s" % status)
    try:
        got = json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        raise ClientError("aria2 返回的不是 JSON，地址填的是 RPC 路径吗？") from e
    if "error" in got:
        msg = (got.get("error") or {}).get("message") or "未知错误"
        raise ClientError("aria2 报错：%s" % msg)
    return got.get("result")


def _aria2_params(token, rest):
    p = []
    if token:
        p.append("token:" + token)
    return p + rest


def aria2_test(rpc, token="", timeout=8):
    """连通性测试：返回 aria2 版本号。"""
    r = check_endpoint(rpc, "aria2")
    got = _aria2_call(r, {"jsonrpc": "2.0", "id": "lw",
                          "method": "aria2.getVersion",
                          "params": _aria2_params(token, [])}, timeout=timeout)
    ver = ((got or {}).get("version")) or "未知版本"
    return ver


def aria2_add(rpc, urls, token="", save_dir="", timeout=15):
    """逐条调用 ``aria2.addUri``。

    不用一次传多条的写法：aria2 的 ``addUri`` 把数组当成**同一个任务的多镜像**，
    传一批磁力会被合并成一个任务，那就错了。
    """
    r = check_endpoint(rpc, "aria2")
    items = [u.strip() for u in (urls or []) if str(u).strip()]
    if not items:
        raise ClientError("没有要提交的链接")
    opts = {}
    if save_dir:
        opts["dir"] = save_dir
    sent = 0
    for u in items:
        _aria2_call(r, {"jsonrpc": "2.0", "id": "lw", "method": "aria2.addUri",
                        "params": _aria2_params(token, [[u], opts])},
                    timeout=timeout)
        sent += 1
    return sent
