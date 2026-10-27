"""传输端口：内核只认「有人能帮我把 dispatch 挂到 socket 上」。

换成 aiohttp / uvicorn / 宿主的网络栈，只要实现这一个协议；
Router / Module / handler 一行都不用改。
"""
from abc import ABC, abstractmethod

__all__ = ["TransportPort", "BoundServer"]


class BoundServer(ABC):
    """已经绑定端口、可以开始收请求的服务实例。"""

    @property
    @abstractmethod
    def address(self):
        """实际监听的 (host, port)。传 port=0 时这里能拿到系统分配的端口。"""

    @abstractmethod
    def serve_forever(self):
        """阻塞收请求，直到 :meth:`shutdown`。"""

    @abstractmethod
    def shutdown(self):
        """让 serve_forever 尽快返回（可在别的线程里调）。"""

    @abstractmethod
    def close(self):
        """释放 socket。"""


class TransportPort(ABC):
    """把「派发函数」变成「能收请求的服务」。"""

    @abstractmethod
    def bind(self, dispatch, host, port):
        """``dispatch(method, raw_path, headers, body) -> Response``。

        返回 :class:`BoundServer`。绑定失败（端口占用等）应抛 ``OSError``。
        """
