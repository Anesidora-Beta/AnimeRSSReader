# -*- coding: utf-8 -*-
"""组合查询：把一句人话编译成「每个源各自能听懂的那部分 + 统一的本地兜底过滤」。

为什么要分成两半
----------------
六个源站的能力完全不同：AnimeGarden 支持时间区间和翻页，动漫花园的搜索 RSS
一次给 500 条但不能翻页，萌番组只能一页 30 条拿回来自己筛。如果按「谁支持什么」
分别写一套查询逻辑，加第七个源就要再想一遍语义，组合性立刻崩掉。

所以这里只做两件事：

1. **下推（push down）**：只把「不会漏结果」的条件交给站点 —— 关键词取必须词
   加最长的一个任选词，时间区间交给支持的站，其余一概不下推；
2. **兜底（local filter）**：拿到结果后按**完整语义**再过一遍，保证换源、混源、
   加源之后，同一句查询的结果集合语义不变。

一句话：站点负责**捞得快**，这里负责**捞得准**。

查询语法（前端搜索框里直接写）
------------------------------
=========================  ============================================
写法                        含义
=========================  ============================================
``鬼灭 无限城``             任一词命中即可（OR）
``+鬼灭 +1080p``            必须全部命中（AND）
``-粤语 -合集``             命中即排除（NOT）
``字幕组:桜都``             字幕组 / 发布者（等价 ``fansub:``）
``类型:动画``               分类（等价 ``type:`` / ``cat:``）
``src:dmhy,nyaa``           只在指定源里搜（等价 ``站点:``）
``after:2022-01-01``        起始时间，也吃相对写法 ``after:3y`` / ``after:90d``
``before:2023-06-30``       截止时间
``size>1G`` / ``size<500M`` 体积区间
``sort:seeders``            排序：date / size / seeders / relevance
``pages:8``                 翻页深度（本次请求翻几页）
``"鬼灭 无限城"``           带空格的整体词（引号内不拆）
=========================  ============================================
"""
import re
import time

__all__ = ["Query"]

#: 字段别名 -> 内部字段名。中文键让前端不用教用户英文。
_FIELDS = {
    "src": "src", "source": "src", "站点": "src", "源": "src",
    "fansub": "fansub", "team": "fansub", "字幕组": "fansub", "发布": "fansub",
    "type": "category", "cat": "category", "类型": "category", "分类": "category",
    "after": "after", "from": "after", "since": "after", "起": "after",
    "before": "before", "to": "before", "until": "before", "止": "before",
    "sort": "sort", "排序": "sort",
    "pages": "pages", "深度": "pages",
    "limit": "limit", "条数": "limit",
}

_SORTS = {"date": "date", "time": "date", "时间": "date",
          "size": "size", "体积": "size",
          "seeders": "seeders", "seeds": "seeders", "做种": "seeders",
          "relevance": "relevance", "相关": "relevance", "相关度": "relevance"}

_REL_RE = re.compile(r"^(\d{1,3})([dwmy])$", re.I)
_REL_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}

_SIZE_CMP_RE = re.compile(r"^size\s*(>=|<=|>|<|=)\s*([\d.]+\s*[KMGT]?(?:i?B)?)$", re.I)


def _tokenize(raw):
    """按空白切词，但尊重引号；``"鬼灭 无限城"`` 算一个词。"""
    out, buf, quote = [], [], ""
    for ch in raw or "":
        if quote:
            if ch == quote:
                out.append("".join(buf))
                buf, quote = [], ""
            else:
                buf.append(ch)
        elif ch in "\"'":
            if buf:
                out.append("".join(buf))
                buf = []
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [t for t in out if t]


def _merge_words(base, extra):
    """把面板传进来的词并进语法解析出的列表（去重、保持先后）。"""
    out = list(base or [])
    if not extra:
        return out
    raw = extra if isinstance(extra, (list, tuple)) else re.split(r"[\s,，、]+", str(extra))
    seen = {w.lower() for w in out}
    for w in raw:
        w = str(w).strip().lower()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _to_bytes(value):
    """``"1G"`` / ``"500M"`` / ``1073741824`` 都转成字节。"""
    from .model import size_bytes
    if isinstance(value, (int, float)):
        return int(value)
    return size_bytes(value)


def _rel_date(value):
    """``3y`` / ``90d`` -> ``YYYY-MM-DD``；已经是日期就原样返回。"""
    s = (value or "").strip()
    m = _REL_RE.match(s)
    if not m:
        return s
    days = int(m.group(1)) * _REL_DAYS[m.group(2).lower()]
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))


def _date_ts(value, end_of_day=False):
    """日期字符串 -> 当天 0 点（或 23:59:59）的时间戳。"""
    s = _rel_date(value)
    if not s:
        return 0
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        t = time.mktime((y, mo, d, 23 if end_of_day else 0,
                         59 if end_of_day else 0, 59 if end_of_day else 0, 0, 0, -1))
        return int(t)
    from .model import to_ts
    return to_ts(s) or 0


class Query:
    """一次搜索请求。构造后就是只读的，可以安全地被多个线程同时使用。"""

    __slots__ = ("raw", "any_terms", "must", "exclude", "fansub", "category",
                 "src", "after", "before", "sort", "limit", "pages", "page",
                 "size_min", "size_max", "unknown", "_after_ts", "_before_ts")

    def __init__(self):
        self.raw = ""
        self.any_terms = []       # OR
        self.must = []            # AND
        self.exclude = []         # NOT
        self.fansub = ""
        self.category = ""
        self.src = []             # 空 = 全部源
        self.after = ""
        self.before = ""
        self.sort = "date"
        self.limit = 60
        self.pages = 3
        self.page = 1
        self.size_min = 0
        self.size_max = 0
        self.unknown = []         # 认不出的字段名，回给前端提示用
        self._after_ts = 0
        self._before_ts = 0

    # ------------------------------------------------------------ 构造
    @classmethod
    def parse(cls, raw="", src=None, after="", before="", sort="", limit=0,
              pages=0, page=1, mode="", must=None, exclude=None, fansub="",
              category="", size_min=0, size_max=0):
        """解析查询串，并用显式参数覆盖（前端的下拉框走覆盖，搜索框走语法）。

        ``mode`` 是给普通用户的三个档：``fast``(1 页) / ``deep``(3 页) /
        ``archive``(10 页)，等价于写 ``pages:1/3/10``。

        ``must`` / ``exclude`` / ``fansub`` / ``size_min`` / ``size_max``
        是给图形筛选面板用的：不写 ``+`` / ``-`` / ``字幕组:`` / ``size>``
        这些符号，直接传结构化条件。两套入口最终落在同一个 Query 上，
        所以界面上「面板」和「语法」怎么混用都不会打架。
        """
        q = cls()
        q.raw = (raw or "").strip()
        q.page = max(1, int(page or 1))

        for tok in _tokenize(q.raw):
            body, sign = tok, ""
            if len(tok) > 1 and tok[0] in "+-":
                sign, body = tok[0], tok[1:]

            m = _SIZE_CMP_RE.match(body)
            if m:
                from .model import size_bytes
                n = size_bytes(m.group(2))
                if m.group(1).startswith(">"):
                    q.size_min = max(q.size_min, n)
                elif m.group(1).startswith("<"):
                    q.size_max = n or q.size_max
                else:
                    q.size_min, q.size_max = n, n
                continue

            name, sep, value = body.partition(":")
            field = _FIELDS.get(name.strip().lower())
            if sep and field and value.strip():
                value = value.strip()
                if field == "src":
                    q.src = [x.strip().lower() for x in value.split(",") if x.strip()]
                elif field == "fansub":
                    q.fansub = value
                elif field == "category":
                    q.category = value
                elif field == "after":
                    q.after = _rel_date(value)
                elif field == "before":
                    q.before = _rel_date(value)
                elif field == "sort":
                    q.sort = _SORTS.get(value.lower(), q.sort)
                elif field == "pages":
                    try:
                        q.pages = max(1, min(int(value), 30))
                    except ValueError:
                        pass
                elif field == "limit":
                    try:
                        q.limit = max(1, min(int(value), 300))
                    except ValueError:
                        pass
                continue
            if sep and field is None and re.match(r"^[\w\u4e00-\u9fff]+$", name):
                q.unknown.append(name)

            low = body.lower()
            if sign == "+":
                q.must.append(low)
            elif sign == "-":
                q.exclude.append(low)
            else:
                q.any_terms.append(low)

        # 显式参数优先（下拉框 / URL 参数）
        if src:
            q.src = [x.strip().lower() for x in
                     (src.split(",") if isinstance(src, str) else src) if str(x).strip()]
        if after:
            q.after = _rel_date(after)
        if before:
            q.before = _rel_date(before)
        if sort:
            q.sort = _SORTS.get(str(sort).lower(), q.sort)
        if limit:
            q.limit = max(1, min(int(limit), 300))
        if pages:
            q.pages = max(1, min(int(pages), 30))
        elif mode in ("fast", "deep", "archive"):
            q.pages = {"fast": 1, "deep": 3, "archive": 10}[mode]

        # 图形面板的条件：和语法里写的**合并**而不是覆盖，
        # 用户在搜索框写 +1080p、在面板填「简日」，两条都该生效。
        q.must = _merge_words(q.must, must)
        q.exclude = _merge_words(q.exclude, exclude)
        if fansub:
            q.fansub = str(fansub).strip()
        if category:
            q.category = str(category).strip()
        if size_min:
            try:
                q.size_min = max(q.size_min, _to_bytes(size_min))
            except (TypeError, ValueError):
                pass
        if size_max:
            try:
                n = _to_bytes(size_max)
                q.size_max = min(q.size_max, n) if q.size_max else n
            except (TypeError, ValueError):
                pass

        q._after_ts = _date_ts(q.after)
        q._before_ts = _date_ts(q.before, end_of_day=True)
        return q

    # ------------------------------------------------------------ 派生
    @property
    def empty(self):
        """一句话都没说（既没词也没条件）—— 这种请求只允许读本地库。"""
        return not (self.any_terms or self.must or self.fansub
                    or self.category or self.after or self.before)

    @property
    def terms(self):
        return tuple(self.must) + tuple(self.any_terms)

    def server_keyword(self):
        """下推给站点的关键词：必须词全给，任选词给最长的一个。

        多给一个任选词会让「服务端按 AND 处理」的站点漏结果，
        OR 语义的完整性由本地过滤保证 —— 宁可多拿，不能少拿。

        注意 ``must`` 非空时**也要带上任选词**：只推必须词的话，
        ``must=1080p`` 这种规格词会把站点整片「1080p 资源」都捞回来，
        本地再按番名筛掉 —— 抓了一堆无关结果，还一条都对不上。
        """
        words = list(self.must)
        if self.any_terms:
            longest = max(self.any_terms, key=len)
            if longest not in words:
                words.append(longest)
        return " ".join(w for w in words if w)

    def serverside(self, caps):
        """按站点能力裁剪后的下推参数。``caps`` 见 providers/base.py。"""
        out = {}
        kw = self.server_keyword()
        if kw and "keyword" in caps:
            out["keyword"] = kw
        if "date" in caps:
            if self.after:
                out["after"] = self.after
            if self.before:
                out["before"] = self.before
        if self.fansub and "fansub" in caps:
            out["fansub"] = self.fansub
        if self.category and "category" in caps:
            out["category"] = self.category
        return out

    def explain(self, caps=frozenset()):
        """给前端看的「下推了什么、本地还要过滤什么」，也是排障入口。"""
        pushed = self.serverside(caps)
        local = []
        if self.any_terms:
            local.append("任一词 OR：%s" % " / ".join(self.any_terms))
        if self.must:
            local.append("必须含：%s" % " / ".join(self.must))
        if self.exclude:
            local.append("排除：%s" % " / ".join(self.exclude))
        if self.fansub:
            local.append("字幕组：%s" % self.fansub)
        if self.category:
            local.append("类型：%s" % self.category)
        if self.size_min or self.size_max:
            local.append("体积：%s ~ %s" % (self.size_min or "-", self.size_max or "-"))
        if self._after_ts or self._before_ts:
            local.append("时间：%s ~ %s" % (self.after or "-", self.before or "-"))
        notes = []
        if self.any_terms and len(self.any_terms) > 1 and "keyword" in caps:
            notes.append("多个任选词只下推最长的一个，其余本地过滤")
        if (self.after or self.before) and "date" not in caps:
            notes.append("该源不支持时间区间，取回后再按时间过滤")
        return {"pushed": pushed, "local": local, "notes": notes}

    def signature(self, provider_ids):
        """结果缓存键。

        必须把**所有参与过滤的条件**都放进键里：``must`` / ``exclude`` /
        ``any_terms`` 缺一个，用图形面板换条件时就会命中上一个条件的缓存，
        用户看到的是「换了筛选没反应」—— 这类 bug 极难从界面上看出来。
        """
        return "|".join([
            ",".join(sorted(provider_ids)), self.raw,
            ",".join(self.any_terms), ",".join(self.must), ",".join(self.exclude),
            self.after, self.before, self.sort, str(self.limit), str(self.pages),
            str(self.page), self.fansub, self.category,
            str(self.size_min), str(self.size_max),
        ])

    # ------------------------------------------------------------ 兜底过滤
    def match(self, it):
        """完整语义校验。站点下推是「宽」的，这里才是最终判定。"""
        title = (it.get("title") or "").lower()
        author = (it.get("author") or "").lower()
        cate = (it.get("category") or "").lower()
        haystack = title + " " + author + " " + cate

        if self.any_terms and not any(t in haystack for t in self.any_terms):
            return False
        if self.must and not all(t in haystack for t in self.must):
            return False
        if self.exclude and any(t in haystack for t in self.exclude):
            return False
        if self.fansub and self.fansub.lower() not in haystack:
            return False
        if self.category and self.category.lower() not in haystack:
            return False

        if self.size_min or self.size_max:
            n = int(it.get("sizeBytes") or 0)
            if not n:
                return False
            if self.size_min and n < self.size_min:
                return False
            if self.size_max and n > self.size_max:
                return False

        ts = int(it.get("ts") or 0)
        if ts:
            if self._after_ts and ts < self._after_ts:
                return False
            if self._before_ts and ts > self._before_ts:
                return False
        elif self._after_ts or self._before_ts:
            # 时间未知的条目在限定时间窗时保守剔除，避免「筛了还混进无关的」
            if not it.get("local"):
                return False
        return True

    def __repr__(self):
        return "<Query %r src=%s pages=%d>" % (self.raw, self.src or "*", self.pages)

    # ------------------------------------------------------------ 转成关注
    def to_watch(self, name="", loose=False):
        """翻译成主程序 ``watches`` 表能存的四个字段。

        两张表的能力不一样，映射不是一比一，凡是丢掉的东西都写进 ``notes``
        让用户看见 —— 悄悄降级比报错更坑人。

        ``loose`` 区分两种意图，这一点很容易做错：

        * ``False``（整次搜索存为关注）：条件怎么写的就怎么盯，
          ``1080p`` 这类必须词也进规则；
        * ``True``（点了某一条结果的「关注」）：用户想盯的是**这部番**，
          所以只要番名，``1080p`` / ``简日`` 这些规格词不进规则 ——
          否则以后出 2160p 版本就永远盯不到了。

        返回 ``(fields, notes)``：``fields`` 可直接喂 ``store.add_watch()``。
        """
        notes = []

        # watches.include 是「全部命中」（AND），而 any_terms 是 OR。
        if loose:
            # 只留番名：有任选词就取第一个（搜索习惯是番名在前），
            # 没有任选词才退回必须词，别让关注规则变成空条件。
            words = [self.any_terms[0]] if self.any_terms else list(self.must)
        else:
            words = list(self.must)
            if self.any_terms:
                first = self.any_terms[0]
                if first not in words:
                    words.append(first)
                if len(self.any_terms) > 1:
                    notes.append("关注只能用「%s」这一个词长期跟，"
                                 "本次检索的 %s 是「任一命中」，关注会严格一些"
                                 % (first, " / ".join(self.any_terms)))
        include = " ".join(w for w in words if w)

        if not include and not self.fansub:
            notes.append("既没有关键词也没有字幕组，这个关注会把所有新帖都收进来")

        if self.after or self.before:
            notes.append("%s ~ %s 只用于这次挂载历史条目；"
                         "关注规则本身没有时间窗，以后的新帖仍会持续进来"
                         % (self.after or "最早", self.before or "最新"))
        dropped = []
        if self.size_min or self.size_max:
            dropped.append("体积")
        if self.category:
            dropped.append("类型")
        if dropped:
            notes.append("关注规则不支持%s条件，已忽略（不影响本次检索）"
                         % "、".join(dropped))

        name = (name or "").strip()
        if not name:
            base = (self.any_terms[0] if self.any_terms
                    else (self.must[0] if self.must else self.fansub) or "未命名关注")
            # 规则里还有番名之外的词就在名字上带出来，
            # 否则关注列表里会出现两条都叫「鬼灭」、规则却不一样的记录
            extra = [w for w in words if w != base]
            name = base + ("（" + " ".join(extra) + "）" if extra else "")

        fields = {
            "name": name,
            "include": include,
            "exclude": " ".join(self.exclude),
            "fansub": self.fansub,
            "sources": ",".join(self.src),
        }
        return fields, notes
