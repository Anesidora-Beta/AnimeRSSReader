"""静态资源端口：内核只认「给目录和文件名，还我字节」。

默认实现读磁盘（改一行刷新即生效）；打包成单文件时换成内存实现即可。
"""
from abc import ABC, abstractmethod

__all__ = ["AssetPort"]


class AssetPort(ABC):
    @abstractmethod
    def read(self, directory, name):
        """读 ``directory`` 下的 ``name``，返回 bytes；不存在返回 None。

        实现**必须**拒绝目录穿越（``..``、绝对路径、符号链接逃逸）。
        """

    @abstractmethod
    def content_type(self, name):
        """按文件名推断 Content-Type，认不出时给 application/octet-stream。"""
