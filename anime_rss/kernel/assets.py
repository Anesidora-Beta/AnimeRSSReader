"""静态资源：按 mtime 缓存读取，支持把任意目录挂到任意前缀。

前端「改一行刷新即生效」靠的就是 mtime 缓存 + 不打包。
"""
import os

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


class Assets:
    def __init__(self):
        self._cache = {}                      # (dir, name) -> (mtime, bytes)

    def read(self, directory, name):
        """读目录下的文件，按 mtime 缓存；不存在返回 None。"""
        safe = os.path.basename(name)         # 只允许平铺，杜绝目录穿越
        if not safe:
            return None
        path = os.path.join(directory, safe)
        if not os.path.isfile(path):
            return None
        key = (directory, safe)
        mtime = os.path.getmtime(path)
        hit = self._cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        with open(path, "rb") as f:
            data = f.read()
        self._cache[key] = (mtime, data)
        return data

    def invalidate(self):
        self._cache.clear()

    @staticmethod
    def content_type(name):
        return TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


class Mount:
    """把某个目录挂到某个 URL 前缀下。transform 可用于注入版本号等。"""

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
        name = path[len(self.prefix):] or self.index
        return name
