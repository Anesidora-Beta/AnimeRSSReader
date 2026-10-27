"""静态资源端口的内存实现：把资源塞进 dict，适合单文件打包与测试。

    assets = MemoryAssets({"/assets/app.js": b"..."})   # 键用绝对「路径」
    kernel = Kernel(..., asset_port=assets)

内核只按 ``(directory, name)`` 取值，所以这里把两个拼回去当键。
"""
import posixpath

from ..ports.assets import AssetPort
from .filesystem_assets import TYPES
import os

__all__ = ["MemoryAssets"]


class MemoryAssets(AssetPort):
    def __init__(self, files=None, types=None):
        self._files = {}
        self._types = dict(TYPES)
        if types:
            self._types.update(types)
        for key, value in (files or {}).items():
            self.put(key, value)

    def put(self, path, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._files[_norm(path)] = data
        return self

    def read(self, directory, name):
        safe = os.path.basename(name or "")
        if not safe:
            return None
        return self._files.get(_norm(os.path.join(directory or "/", safe)))

    def content_type(self, name):
        return self._types.get(os.path.splitext(name)[1].lower(),
                               "application/octet-stream")

    def paths(self):
        return sorted(self._files)


def _norm(path):
    p = str(path).replace("\\", "/")
    return posixpath.normpath(p) if p.startswith("/") else "/" + posixpath.normpath(p)
