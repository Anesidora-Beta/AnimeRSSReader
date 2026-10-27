"""端口层：内核依赖的全部抽象接口。

这里**不允许** import 任何具体实现，也不允许 import 内核其它模块。
依赖方向永远是 ``adapters → ports ← kernel``。
"""
from .assets import AssetPort
from .clock import ClockPort
from .transport import BoundServer, TransportPort

__all__ = ["AssetPort", "ClockPort", "BoundServer", "TransportPort"]
