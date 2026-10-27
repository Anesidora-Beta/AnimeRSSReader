"""静态资源端口的文件系统实现：按 mtime 缓存，改一行刷新即生效。"""
import os

from ..ports.assets import AssetPort

__all__ = ["FilesystemAssets", "TYPES"]

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
}


class FilesystemAssets(AssetPort):
    def __init__(self):
        self._cache = {}                          # (dir, name) -> (mtime, bytes)

    def read(self, directory, name):
        safe = os.path.basename(name)             # 只允许平铺，杜绝目录穿越
        if not safe or not directory:
            return None
        path = os.path.join(directory, safe)
        if not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = (directory, safe)
        hit = self._cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        self._cache[key] = (mtime, data)
        return data

    def content_type(self, name):
        return TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")

    def invalidate(self):
        self._cache.clear()
