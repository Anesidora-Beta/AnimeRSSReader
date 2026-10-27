# -*- coding: utf-8 -*-
"""源注册表：内置 7 条腿 + 第三方随时插进来的腿，对上层是同一张表。

组合性在这里落地：查询层只认 ``Provider`` 这个形状，不认具体站点；
所以「再写一个源」等于「再交一个类」，既不碰接口层也不碰前端 —— 前端的
源开关是从 ``/api/search/providers`` 读出来的，新源自动出现在可选列表里。

第三方插件的接法（在自己的 ``setup(ctx)`` 里）：

.. code-block:: python

    from arr_search.providers.base import Provider
    ctx.extra.setdefault("search.providers", []).append(MyProvider)

同 id 的第三方实现会**顶掉**内置实现，方便本地魔改某个源的行为。
"""
from .acgrip import AcgRip
from .animegarden import AnimeGarden
from .bangumi import Bangumi
from .base import Page, Provider, ProviderError
from .dmhy import Dmhy
from .localdb import LocalDB
from .mikan import Mikan
from .nyaa import Nyaa

__all__ = ["Page", "Provider", "ProviderError", "build", "default_ids",
           "BUILTIN", "DEFAULT_IDS", "LocalDB"]

#: 内置实现（顺序即前端的默认展示顺序）
BUILTIN = (AnimeGarden, Dmhy, Nyaa, AcgRip, Mikan, Bangumi, LocalDB)

#: 默认开的源：本地库单列，联网的四个「能往历史里走」的先开
DEFAULT_IDS = ("animegarden", "dmhy", "nyaa", "acgrip", "bangumi", "local")


def build(extra=(), store=None):
    """实例化全部源，返回 ``{id: provider}``。``extra`` 里的同 id 项优先。"""
    out = {}
    for cls in BUILTIN:
        try:
            out[cls.id] = cls(store)
        except Exception:                            # noqa: BLE001 - 单个源坏了不拖垮其它
            continue
    for cls in extra or ():
        try:
            inst = cls(store) if isinstance(cls, type) else cls
            if getattr(inst, "id", ""):
                out[inst.id] = inst
        except Exception:                            # noqa: BLE001
            continue
    return out


def default_ids():
    return list(DEFAULT_IDS)
