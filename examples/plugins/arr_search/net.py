# -*- coding: utf-8 -*-
"""抓取原语：带 UA、超时、一次重试、短 TTL 缓存。

刻意只用标准库（主程序一条 ``requirements.txt`` 就是「零第三方依赖」，
插件不该破坏这个前提）。缓存是进程内的，用于两件事：

* 同一个关键词翻页时，第一页不必因为用户点「再深一点」而重抓；
* 前端连续输入、同一查询被反复触发时，不打爆源站。

不做磁盘缓存 —— 搜索结果的时效性比命中率重要，而且旧帖本来就一直在那儿。
"""
import gzip
import io
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

__all__ = ["fetch", "fetch_json", "fetch_text", "clear_cache", "cache_stats"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
      "AnimeRSSReader-Search/1.0 (+https://github.com/Anesidora-Beta/AnimeRSSReader)")

DEFAULT_TIMEOUT = 15
DEFAULT_TTL = 120          # 秒；0 表示不缓存
MAX_CACHE = 96             # 条数上限，够覆盖一次「10 页 × 8 源」的浏览
MAX_BYTES = 4 << 20        # 单条响应上限，防止误抓到超大页面把内存吃了

_lock = threading.Lock()
_cache = {}                # url -> (expire_at, bytes)
_host_fail = {}            # host -> 连续失败次数
_stats = {"hit": 0, "miss": 0, "error": 0}


def _decode_body(raw, encoding):
    enc = (encoding or "").lower()
    try:
        if enc == "gzip" or (raw[:2] == b"\x1f\x8b"):
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


def fetch(url, timeout=None, retries=1, ttl=None, headers=None, data=None):
    """GET（给了 ``data`` 就是 POST）并返回字节。失败抛 ``RuntimeError``。"""
    timeout = float(timeout or DEFAULT_TIMEOUT)
    ttl = DEFAULT_TTL if ttl is None else max(0, float(ttl))
    key = url if not data else url + "#" + str(hash(data))

    if ttl:
        with _lock:
            hit = _cache.get(key)
            if hit and hit[0] > time.time():
                _stats["hit"] += 1
                return hit[1]
            _stats["miss"] += 1

    head = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, data=data, headers=head)

    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BYTES)
                body = _decode_body(raw, resp.headers.get("Content-Encoding"))
                if ttl and body:
                    with _lock:
                        if len(_cache) >= MAX_CACHE:
                            oldest = min(_cache, key=lambda k: _cache[k][0])
                            _cache.pop(oldest, None)
                        _cache[key] = (time.time() + ttl, body)
                _host_fail.pop(urllib.parse.urlsplit(url).netloc, None)
                return body
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (403, 404, 410):        # 重试没意义
                break
        except Exception as e:                   # noqa: BLE001 - 网络异常种类太多
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < retries:
            time.sleep(0.4 + 0.4 * attempt)

    with _lock:
        _stats["error"] += 1
        netloc = urllib.parse.urlsplit(url).netloc
        _host_fail[netloc] = _host_fail.get(netloc, 0) + 1
    raise RuntimeError("抓取失败（%s）：%s" % (last or "未知原因", url))


def fetch_text(url, encoding="utf-8", **kw):
    return fetch(url, **kw).decode(encoding, "ignore")


def fetch_json(url, **kw):
    import json
    return json.loads(fetch(url, **kw).decode("utf-8", "ignore"))


def clear_cache():
    with _lock:
        _cache.clear()


def cache_stats():
    with _lock:
        return {"entries": len(_cache), "hits": _stats["hit"],
                "misses": _stats["miss"], "errors": _stats["error"],
                "downHosts": dict(_host_fail)}
