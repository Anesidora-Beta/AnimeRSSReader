# arr_search —— 深度搜索插件（模块化说明）

> 解决一个具体问题：**主程序只拉最新 200 条，旧帖子找不回来。**
> 这个插件让「按需检索源站历史」成为可能，并且可以**任意组合**：源 × 条件 × 深度 × 排序。

安装方式只有一步：把整个 `arr_search/` 目录丢进程序目录下的 `plugins/`，重启。
不用改 `app.py`、不用改 `api/core.py`、不用改前端 HTML。

### 装之前先看这一条：`plugins/` 目录一开始是不存在的

这不是你找错地方。仓库的 `.gitignore` 里有一行 `/plugins/`，意思是**插件目录刻意不进版本库**，
所以 clone 下来、或者解压发行包，都看不到它。主程序也不会自己建这个目录
——`discovery._from_directory()` 遇到不存在的目录直接 `return`，**静默跳过、不报错**。
于是「没装插件」和「插件加载失败」在界面上长得一模一样：侧栏什么都没有。

自己建一个就行，位置看你怎么启动：

| 你怎么启动 | `plugins/` 要建在哪 | 为什么 |
|---|---|---|
| `python app.py`（源码） | `AnimeRSSReader/plugins/`，和 `app.py` 同级 | `_BASE = HERE`，即 `app.py` 所在目录 |
| `dist/AnimeRSSReader.exe`（打包版） | `dist/plugins/`，和 exe 同级 | `_BASE = dirname(sys.executable)` |

两个都用到的话就两处各放一份（源码版和 exe 版互不读取）。

```text
AnimeRSSReader/
├── app.py
├── anime_rss/
└── plugins/                    ← 自己建
    └── arr_search/             ← 整个包放进来（注意：目录名别以 _ 或 . 开头）
        ├── __init__.py         ← 会被 discovery 当作包入口
        ├── module.py
        └── ...
```

放好后一条命令自检，不用靠猜：

```bash
python plugins/arr_search/selfcheck.py --live
```

它会打印程序目录、插件目录、侧栏视图是否注册、7 个源是否就绪，再真搜一次。
（用临时 data_dir，不碰你现有的订阅和收藏。）看到「安装成功」就重启程序，
侧栏会出现「深度搜索」。

---

## 1. 先看结论：它到底能往历史里走多深

实测（2026-10，关键词「鬼灭」，每源 2 页）：

| 源 | 接口 | 单页 | 能翻页 | 实测最旧 |
|---|---|---:|---|---|
| AnimeGarden | `GET /resources?search=&page=&after=&before=` | 100 | ✅ 无限 | **2022-01** |
| 动漫花园 | `/topics/rss?keyword=` | 500（一次给完） | ❌ | **2022-01** |
| Nyaa | `/?q=&p=N`（读 HTML） | 75 | ✅ | **2021-10** |
| ACG.RIP | `/.xml?term=&page=N` | 30 | ✅ | 2024-06（2 页，可更深）|
| 蜜柑计划 | `/RSS/Search?searchstr=` | 100 | ❌ | 2024-06 |
| 萌番组 | `/api/torrent/page/N` | 30 | ✅ 但无搜索 | 按时间区间浏览 |

主程序的 200 条是「刷新时截断」，不是源站的极限 —— 这是本插件存在的全部理由。

一条命令自己验：

```bash
python live_smoke.py 鬼灭 2018-01-01     # 打印每个源取回/命中/最旧日期
python live_smoke.py --list              # 只看源能力表
```

---

## 2. 分层：四层，依赖严格单向

```
        ┌──────────────────────────────────────────────┐
  界面  │ static/view.js  + view.css                   │  声明式，走 /plug/search/
        └───────────────────────┬──────────────────────┘
                                │ HTTP  /api/search/*
        ┌───────────────────────▼──────────────────────┐
  编排  │ module.py   SearchModule(Module)             │  路由 / 设置 / 事件 / 入库
        │            并发扇出 · 合并去重 · 排序 · 缓存  │
        └───────┬───────────────────────┬──────────────┘
                │                       │
        ┌───────▼────────┐      ┌───────▼──────────────┐
  领域  │ query.py       │      │ model.py             │  纯函数，零依赖，可单测
        │ 语法→下推/兜底 │      │ 统一条目·去重·排序   │
        └───────┬────────┘      └──────────────────────┘
                │
        ┌───────▼──────────────────────────────────────┐
  适配  │ providers/{animegarden,dmhy,nyaa,acgrip,     │  一个源一个文件
        │            mikan,bangumi,localdb}.py         │  只回答「第 N 页怎么拿」
        └───────┬──────────────────────────────────────┘
                │
        ┌───────▼────────┐
  基建  │ net.py         │  UA / 超时 / 重试 / TTL 缓存
        └────────────────┘
```

依赖方向：`module → query/model → providers → net`，同层不互相引用，`query` 不认识任何具体站点。

**这套分层不是为了好看，是为了「加第七个源不用改前六层」** —— 见 §7。

---

## 3. 与主程序契约的对应关系

`docs/ARCHITECTURE.md` §4 定的规矩，逐条对上了：

| 契约 | 本插件的做法 |
|---|---|
| `Module` 子类 + `id` / `name` / `version` / `api_version` | `module.py::SearchModule`，`id="search"`，`api_version` 走默认 |
| 路由只占 `/api/<id>/` | `/api/search`、`/api/search/providers`、`/api/search/presets`… |
| 表名 `mod_<id>_` 前缀 | `mod_search_preset`、`mod_search_history` |
| 设置由模块自带 | `ctx.settings.define({group:"search"}, after="read")` |
| 前端视图声明式 | `views` + `assets = "static"`，走 `/plug/search/` |
| 跨模块只走事件 | 只 `ctx.emit("search.done", …)`，不 import 别的模块 |
| 定时任务交内核 | `ctx.every(86400, trim, name="search.trim")` |
| 生命周期 | `setup` / `apply_settings` / `teardown` 全实现 |

**唯一一处偏离**：数据没进主库，而是 `data/search.db`。
原因写在 `storage.py` 顶部 —— 主程序的 `Store` 没有对外暴露连接（`ctx.db` 还只是文档里的目标形态），
插件不去摸别人的 `_db`；表名仍按规范起，将来并库只改连接不改表名。

---

## 4. 组合模型：四个维度都能自由拼

### 4.1 源可组合

`src:dmhy,nyaa` 或界面上的 chip 多选。新增源自动出现在 chip 里 ——
前端是从 `/api/search/providers` 读的，没有硬编码列表。

### 4.2 条件可组合：两种入口，一个 Query

**默认不用记语法。** 界面上是四格看得见的条件：

| 界面上的框 | 等价语法 | 语义 |
|---|---|---|
| 关键词 | `鬼灭 无限城` | 任一命中（OR） |
| 必含 | `+鬼灭 +1080p` | 全部命中（AND） |
| 排除 | `-粤语 -合集` | 命中即排除（NOT） |
| 字幕组 | `字幕组:樱都` | 发布者匹配，输入框带结果里出现的字幕组补全 |
| 体积不小于 | `size>1G` | 体积下限 |

这四格以**独立 HTTP 参数**（`must` / `exclude` / `fansub` / `sizeMin`）传给后端，
和语法解析落在同一个 `Query` 上（`query.py::parse` 里做的是**合并**不是覆盖）——
所以「在必含里填 1080p、在关键词框里写 `+简日`」两条都生效。

语法仍然在（右上角「高级语法」），也仍然值得用：

| 写法 | 含义 |
|---|---|
| `鬼灭 无限城` | 任一命中（OR） |
| `+鬼灭 +1080p` | 必须全部命中（AND） |
| `-粤语 -合集` | 命中即排除（NOT） |
| `字幕组:樱都`、`类型:动画` | 字段过滤（`fansub:` / `type:` 同义） |
| `src:dmhy,nyaa` | 限定源 |
| `after:2022-01-01`、`after:3y`、`before:2023-06-30` | 时间区间，支持相对写法 |
| `size>1G`、`size<500M` | 体积区间 |
| `sort:seeders` | 排序：`date` / `size` / `seeders` / `relevance` |
| `pages:10` | 本次翻几页 |
| `"鬼灭 无限城"` | 引号内不拆词 |

「保存组合」存的是这份语法串（`composeQuery()` 拼出来），点开组合时再
`decomposeQuery()` 拆回四个图形框 —— 组合因此是完整状态快照，而不是只记住关键词。

### 4.3 下推与兜底：组合能成立的关键

各源能力天差地别，如果「谁支持什么就各写一套」，加源就得重想语义。
所以只有一条规则：

> **站点负责捞得快，本地负责捞得准。**
> 下推的只放「不会漏结果」的条件；语义完整性一律由 `Query.match()` 兜底。

具体地（`query.py::serverside`）：

- 关键词：必须词（`+`）**和**最长的那个任选词都下推。
  只推必须词会出事 —— `must=1080p` 会让站点把整片「1080p 资源」捞回来，
  本地再按番名筛掉，白抓一堆还一条都对不上（这个 bug 真踩过）；
- 时间区间：只给声明了 `date` 能力的源（目前只有 AnimeGarden）；
- 字幕组 / 分类：只给声明了对应能力的源；
- `-排除`、体积、OR 组合：一律不下推，本地过滤。

返回结果里带 `explain`，前端能直接看到「哪些下推了、哪些本地过滤」——
排障时不用猜。

### 4.4 策略可组合（每个源自己决定花多少力气）

`Provider.page_budget(q, pages)` 是唯一的「策略钩子」。
默认照单全收；萌番组覆盖为「有关键词就只翻 1 页」——
它没有搜索接口，硬翻 10 页纯属给源站添堵。

编排层不写任何 `if provider.id == ...`。

### 4.5 结果可组合

- **跨源去重**：同 infohash 或同归一化标题算一条，保留字段最全的那条，
  并把缺失字段补齐（`model.dedupe`），`alsoIn` 记录它还出现在哪些源；
- **统一排序**：时间 / 体积 / 做种 / 相关度；
- **可入库**：点「收藏」写进本地库并加星，和阅读器成为一套；
- **可转关注**：搜到的历史帖能直接挂进主程序的关注列表（见 4.6）。

### 4.6 结果去向：收藏 / 关注 / 直接下

一条结果有四个出口，各自解决不同的事：

| 出口 | 写到哪 | 适合 |
|---|---|---|
| 收藏 | 本地库 `search_keep` 频道 + `starred=1` | 这一条现在就要，且不能被 30 天清理带走 |
| 关注（卡片上那个） | 主程序 `watches` + `watch_items` | 这部**番**以后持续盯，新帖自动进关注页 |
| 整次搜索存为关注 | 同上，但规则照抄当前条件 | 「1080p 的鬼灭」这类成套需求 |
| 磁力 | 剪贴板 | 直接丢给下载器 |

#### 为什么要复刻主程序那四行，而不是另存一份

主程序自己就有「补搜老番」的链路（`reader._backfill_watch`）：

```python
got = ctx.refresher.search_for_watch(w)
ctx.store.upsert_items(None, got, link=False)   # link=False：不进「全部更新」
ctx.store.attach_watch(wid, got)
```

`watchbridge.py` 做的是**同一件事**，只是 `got` 来自多源检索而不是单一源。
所以关注页里看到的，和主程序自己补回来的一模一样，不需要在主程序里加任何代码。
两个必须小心的地方：

1. **字段名不同**：插件侧叫 `sourceName`，store 侧要 `author`；
2. **id 必须让 store 自己算**：`attach_watch` 用 `store.item_key(it)` 生成
   `watch_items.item_id`。这里如果塞一个自己算的 id，关注页 JOIN `items` 时
   一条都查不出来 —— 最容易踩且最难发现的坑。所以转出来的 dict **不带 id**。

#### 两条规则不一样，是有意的

`watches` 表只有 `include / exclude / fansub / sources` 四个字段，
和检索条件不是一一对应。`Query.to_watch(loose=...)` 用两种模式处理：

| 动作 | `loose` | 规则 | 为什么 |
|---|---|---|---|
| 点某条结果的「关注」 | `True` | `include=鬼灭` | 用户想盯的是**这部番**。把 `1080p` 写进规则，以后出 2160p 就永远盯不到 |
| 整次搜索存为关注 | `False` | `include=1080p 鬼灭` | 用户要的就是这套条件，照抄才对 |

规则里除了番名还有别的词时，关注名会自动带上（`鬼灭（1080p）`），
免得关注列表里出现两条都叫「鬼灭」、规则却不一样的记录。

`OR` 组合在 `include` 里表达不了（它是 AND），只能取第一个词 —— 搜索习惯是
番名在前，所以取第一个而不是最长的（按长度会选中 `1080p`，那样关注就废了）。
这类降级一律写进响应的 `notes`，前端显示在状态行下面，不偷偷改语义。

时间和体积条件**不进**关注规则：它们只用于本次挂载历史条目。这是有意的 ——
关注是「持续盯」，给它加个时间窗，半年后又失效，比没有更让人困惑。

---

## 5. 目录与职责

```
arr_search/
├── __init__.py          只暴露 MODULE（延迟导入，纯逻辑层因此可脱机单测）
├── module.py            模块本体：路由 / 设置 / 并发扇出 / 去重排序 / 收藏入库
├── query.py             查询语法 → 下推参数 + 本地过滤（语义唯一来源），含 to_watch()
├── model.py             统一条目结构、时间/体积归一、去重、排序（纯函数）
├── watchbridge.py       结果 → 主程序关注列表（字段转换 + upsert + attach）
├── net.py               抓取：UA、超时、一次重试、TTL 缓存、主机失败计数
├── storage.py           组合（preset）与历史：mod_search_* 表
├── providers/
│   ├── base.py          Provider 基类 + 能力表 + Page + RSS 小工具
│   ├── __init__.py      注册表：内置 7 条腿 + 第三方插入
│   ├── animegarden.py   唯一支持服务端时间区间 + 无限翻页，找旧帖主力
│   ├── dmhy.py          搜索 RSS 一次 500 条大窗口，三镜像轮询
│   ├── nyaa.py          RSS 不翻页 → 改读列表页 HTML（正则，不引依赖）
│   ├── acgrip.py        .xml?term=&page= 每页 30
│   ├── mikan.py         RSS/Search 单页 100，只给种子直链
│   ├── bangumi.py       JSON 分页 + 本地筛，越过时间下界即停
│   └── localdb.py       本地库也是一条腿（断网可用）
├── static/              view.js（视图）/ view.css（主题变量对齐主程序）
├── tests/               50 个离线单测 + 18 个集成测试（含夹具）
├── live_smoke.py        联网冒烟：打印每个源能回溯多旧
└── selfcheck.py         装完自检：目录对不对、视图有没有注册、搜不搜得到
```

---

## 6. 接口

### `GET /api/search`

| 参数 | 说明 |
|---|---|
| `q` | 查询串（支持 §4.2 语法） |
| `must` / `exclude` | 图形面板的「必含 / 排除」，空格分隔；与 `q` 里的条件**合并** |
| `fansub` | 字幕组 |
| `sizeMin` / `sizeMax` | 体积上下限，`1G` / `500M` 或字节数 |
| `src` | 源，逗号分隔；空 = 设置里的默认源 |
| `after` / `before` | `YYYY-MM-DD` 或 `3y` / `90d` |
| `sort` | `date`(默认) / `size` / `seeders` / `relevance` |
| `pages` / `mode` | 翻几页；`mode=fast|deep|archive` 等价 1/3/10 页 |
| `page` | 起始页（界面「加载更旧」用） |
| `limit` | 返回条数，1–300 |

返回（节选）：

```json
{
  "ok": true,
  "items": [{"title":"…","source":"animegarden","sourceName":"AnimeGarden",
             "ts":1650000000,"size":"13.5 GiB","magnet":"magnet:?xt=…",
             "seeders":12,"alsoIn":["dmhy"],"local":false}],
  "count": 8, "raw": 592, "matched": 304, "hasMore": true,
  "took": 2350, "cached": false,
  "sources": [{"id":"dmhy","ok":true,"ms":1477,"count":500,"pages":1,"hasMore":false}],
  "explain": [{"id":"dmhy","pushed":{"keyword":"鬼灭"},
               "local":["排除：粤语"],"notes":["该源不支持时间区间…"]}]
}
```

### 其它端点

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/search/providers` | 源清单（能力/每页/最深/**上次运行健康度**）+ 提示 |
| GET/POST | `/api/search/presets` | 列出 / 保存一套组合（源+查询+深度+排序） |
| DELETE | `/api/search/presets/{id}` | 删除组合 |
| GET | `/api/search/history` | 最近查询 |
| POST | `/api/search/history/clear` | 清空历史 |
| POST | `/api/search/keep` | 把结果写进本地库并加星 |
| POST | `/api/search/watch` | 把结果挂进主程序关注列表（见下） |
| GET | `/api/search/watches` | 列出主程序已有的关注 |
| POST | `/api/search/watch/status` | 这批结果里哪些已经在关注里（点亮按钮用） |
| POST | `/api/search/cache/clear` | 清结果缓存与抓取缓存 |

### `POST /api/search/watch`

```jsonc
// A. 点某条结果的「关注」：带上这一条就够了
{"q": "鬼灭", "must": "1080p", "items": [ {...搜索结果那一条...} ]}

// B. 整次搜索存为关注：不用发 items，后端按同一套条件重跑（走缓存，毫秒级）
{"q": "鬼灭", "must": "1080p", "exclude": "粤语", "all": true, "pages": 3}
```

返回：

```json
{
  "ok": true, "created": true, "attached": 2, "addedToStore": 2, "skipped": 0,
  "watch": {"id":"w:3683771071","name":"鬼灭","include":"鬼灭",
            "fansub":"","sources":"","total":2,"unread":2},
  "feed": "watch:w:3683771071",
  "notes": ["2021-01-01 ~ 最新 只用于这次挂载历史条目；关注规则本身没有时间窗…"]
}
```

同一个 `feed` 直接喂主程序的 `/api/items?feed=watch:w:xxx` 就能看到这批条目。
`created=false` 表示复用了同规则的已有 关注（不会每点一次长一条）。

### 也能当库用（不绕 HTTP）

```python
mod = ctx.modules.get("search")
mod.search("鬼灭 -粤语 after:2022-01-01", src="animegarden,nyaa", limit=20)
# → 与 HTTP 完全同构的 dict
```

这正是「本项目也可能成为别人的插件」那条约束带来的便利：业务在模块里，接口只是外壳。

---

## 7. 加一个新源：三步，不碰其它文件

```python
# providers/example_site.py
from .. import net
from ..model import to_ts
from .base import Page, Provider, build


class ExampleSite(Provider):
    id = "example"
    name = "示例站"
    home = "https://example.com"
    note = "每页 40 条，可翻页"
    caps = frozenset({"keyword", "paging"})   # 声明能力 → 查询层自动决定下推什么
    page_size = 40
    max_pages = 30

    def search(self, q, page):
        kw = q.server_keyword()
        url = "%s/list?q=%s&p=%d" % (self.home, kw, page)
        items = []
        for row in parse(net.fetch(url, timeout=15)):        # 你自己的解析
            items.append(build(self.id, self.name, row["title"], row["link"],
                               magnet=row["magnet"], size=row["size"],
                               ts=to_ts(row["date"])))
        return Page(items, has_more=len(items) >= self.page_size)
```

1. 写这个文件；
2. 在 `providers/__init__.py` 的 `BUILTIN` 里加一行；
3. 完。前端 chip、API、缓存、去重、兜底过滤全都自动生效。

不想改插件本体（比如只是自己魔改），就在任意第三方插件里注册：

```python
def setup(ctx):
    ctx.extra.setdefault("search.providers", []).append(ExampleSite)
```

同 id 会**顶掉**内置实现，方便局部改行为。注册表在每次请求时读取，所以加载顺序无所谓。

---

## 8. 与主程序的关系（重要）

- **搜索不入库**：老帖量级大、命中率低，全存进去会把「全部更新」冲垮；
- **收藏才入库**：写进 `search_keep` 频道（`enabled=0`，不参与自动抓取），
  同时 `starred=1` —— 因为主库 `prune()` 会删掉 30 天前的非星标条目，
  **不加星的旧帖第二天就没了**，这是最容易踩的坑；
- **关注只挂载、不建频道**：走 `upsert_items(link=False)`，条目不进任何 feed，
  只在 `watch_items` 里出现，所以「全部更新」不会被历史帖淹掉；
- **不改主库结构**：只用 `store.add_feed / upsert_items / set_star / add_watch /
  attach_watch / watches` 这些公开方法；唯一读私有属性是 `watched_map()` 里的
  `store._db`（主程序没提供「查这批条目在哪些关注里」的方法，而改主程序又违背插件前提），
  加锁只读，拿不到就当没关注；
- **同进程完全信任**：和所有第三方插件一样，本插件拥有主程序同等权限，只装你信任的。

---

## 9. 测试

```bash
# 离线：不联网，50 项（语法 / 结构化参数 / 关注映射 / 下推兜底 / 各源解析 / 去重排序）
python -m unittest discover -s plugins/arr_search/tests -p "test_search.py" -v

# 集成：需要主程序在场（仓库根目录），18 项，用假源测编排与关注接入，不联网
python -m unittest discover -s plugins/arr_search/tests -v

# 联网冒烟：真打源站，看能回溯多旧
python plugins/arr_search/live_smoke.py 鬼灭 2018-01-01
```

夹具在 `tests/fixtures/`，是从真实响应裁剪的（nyaa 列表行、各站 RSS、两个 JSON），
所以解析层的回归测试不需要网络也能覆盖真实结构。

集成测试里有几条专门守关注链路，因为这段最容易「看起来成功、其实没挂上」：

- `test_watch_from_items`：挂完之后**用主程序的 `query_items(feed="watch:…")` 真查一遍**；
- `test_watch_idempotent`：同规则点两次不长出第二条；
- `test_watch_keeps_out_of_updates`：挂载前后「全部更新」条数必须不变（`link=False`）；
- `test_structured_params_over_http`：图形面板的参数真的能筛，不是只改了个界面。

---

## 10. 已知限制（写在这里，省得你踩）

1. **萌番组的搜索接口挂了**（`/api/torrent/search` 返回 500），只能按页翻 + 本地筛，
   所以它擅长「按时间区间浏览」，不擅长关键词；
2. **蜜柑不能翻页**，且它的种子是自己重打包的，磁力可能下不动 —— 只给直链；
3. **动漫花园的 500 条窗口是硬上限**，再旧要靠 AnimeGarden（它本来就镜像花园）；
4. **Nyaa 的 RSS 不支持 `p` 参数**（实测第 3 页与第 1 页完全相同），所以这里读 HTML；
   站点改版会导致解析失效，`tests/fixtures/nyaa_rows.html` 是回归基线；
5. **深度越大越慢**：8 个源 × 10 页 ≈ 几十个请求。默认「深（3 页）」是速度与覆盖的折中；
6. 结果缓存 90 秒、抓取缓存 120 秒，翻页时很快，但**刚发布的帖子最多晚 2 分钟**出现；
7. **关注只看得到 30 天**：挂进去的条目跟主程序其它关注一样受 `KEEP_WATCH_DAYS` 到期清理
   （`watches.last_hit` 会续期）。想长期留存某一条，用「收藏」而不是「关注」；
8. **关注规则只有四字段**，表达不了时间窗和体积（见 §4.6）。这不是没做，是主程序的
   `watches` 表就没这两列 —— 要硬加就得改主程序，那就违背了「插件不改主程序」的前提。
