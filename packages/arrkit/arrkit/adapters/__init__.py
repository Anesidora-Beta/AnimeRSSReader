"""适配器层：端口的具体实现。

内核默认用这里的东西；宿主想换实现，传参替换即可，不改内核一行。
"""
from .filesystem_assets import FilesystemAssets
from .memory_assets import MemoryAssets
from .stdlib_transport import StdlibTransport
from .system_clock import FakeClock, SystemClock

__all__ = ["FilesystemAssets", "MemoryAssets", "StdlibTransport",
           "SystemClock", "FakeClock"]
