# -*- coding: utf-8 -*-
"""源站适配基类：一个源 = 一个类，只回答「第 N 页怎么拿、拿到后怎么解析」。

组合性全靠 ``caps`` 这一张能力表：查询层看到 ``caps`` 才知道哪些条件下推得下去，
看到 ``max_pages`` 才知道这个源能往历史里挖多深。加第七个源时只写 ``search()``，
不用改查询层、不用改接口层、不用改前端。
"""
from ..model import make_item

__all__ = ["Provider", "Page", "ProviderError"]


class ProviderError(RuntimeError):
    """源站返回了看不懂的东西，或者这个请求它压根不支持。"""


class Page:
    """一页结果。``has_more=False`` 就是「到此为止」，查询层立刻停止翻页。"""

    __slots__ = ("items", "has_more", "note")

    def __init__(self, items=(), has_more=False, note=""):
        self.items = list(items)
        self.has_more = bool(has_more)
        self.note = note

    def __repr__(self):
        return "<Page %d items%s>" % (len(self.items), " +more" if self.has_more else "")


class Provider:
    """所有源站的共同形状。子类只需要填四个类属性和实现 ``search``。"""

    id = ""                       # 唯一 id，前端 chip 与查询里的 src:xxx 用它
    name = ""                     # 显示名
    home = ""                     # 站点主页（给用户点回去看）
    note = ""                     # 一句话说明这个源的脾气
    kind = "online"               # online / local

    #: 能力表，取值见 CAPS 注释。查询层据此决定下推哪些条件。
    #:   keyword  支持关键词
    #:   date     支持时间区间（服务端过滤）
    #:   paging   支持翻页（能往更旧的历史里走）
    #:   fansub   支持字幕组过滤
    #:   category 支持分类过滤
    #:   local    本地库，不联网
    caps = frozenset()

    page_size = 0                 # 单页大约几条（0 = 不确定）
    max_pages = 3                 # 最深翻到第几页

    def __init__(self, store=None):
        self.store = store        # 只有 local 源用得上

    # ------------------------------------------------------------ 子类实现
    def search(self, q, page):
        """返回 ``Page``。抛异常会被查询层捕获并记到该源的错误里。"""
        raise NotImplementedError

    def page_budget(self, q, pages):
        """这个源愿意为本次查询翻几页。

        默认照单全收。不擅长关键词检索的源可以压低预算 —— 例如萌番组，
        关键词它只能在本地筛，硬翻 10 页纯属浪费请求。让每个源自己决定
        「为这句话花多少力气」，比在编排层写 if 更能组合。
        """
        return pages

    # ------------------------------------------------------------ 公共
    def describe(self):
        """给 ``/api/search/providers`` 的自我介绍。"""
        return {
            "id": self.id, "name": self.name, "home": self.home,
            "note": self.note, "kind": self.kind,
            "caps": sorted(self.caps), "pageSize": self.page_size,
            "maxPages": self.max_pages,
        }

    def __repr__(self):
        return "<Provider %s>" % (self.id or type(self).__name__)


def build(source, provider_name, title, link="", **kw):
    """薄封装，让各 provider 少写一个参数。"""
    return make_item(source, title, link, source_name=provider_name, **kw)
