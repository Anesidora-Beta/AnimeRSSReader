# 架构设计：薄内核 + 双向插件契约

> 目标版本线：v1.5.0 → v2.0.0
> 状态：设计已定稿，P0–P4 全部完成（薄内核 / 模块化 / 插件 / 可嵌入组装）

## 1. 三条硬约束

来自实际需求，决定了整体形态：

1. **插件既要能自己写，也要能接第三方** → 必须有稳定契约 + 版本协商 + 明确的信任边界，不能靠“约定俗成”。
2. **插件形态是 Python 包** → 用 `importlib.metadata` 入口点发现，`setup(ctx)` 注册；不做 JSON 声明式 DSL。
3. **本项目本身可能成为别的项目的插件** → 这是最关键的一条，它要求运行时（HTTP 服务、数据库、窗口、定时器）**必须可替换**，不能写死在 `app.py` 里。

第 3 条把“抽个路由表”升级成了**端口 / 适配器（hexagonal）**：同一套业务逻辑，既要能跑在自己的 stdlib HTTP 服务里，也要能跑在别人的 Flask / FastAPI / 自定义宿主里。

## 2. 两个方向，一套契约

```
                    ┌──────────────────────────┐
   方向 A（宿主）    │  AnimeRSSReader 作为宿主   │  接受第三方插件
                    │  registry ← entry_points │
                    └────────────┬─────────────┘
                                 │  同一套 ctx / manifest
                    ┌────────────┴─────────────┐
   方向 B（插件）    │  AnimeRSSReader 作为插件   │  被别的项目加载
                    │  export(host) → 注册进去  │
                    └──────────────────────────┘
```

两个方向共用同一份 `ctx`（能力上下文）定义，只是**提供者不同**：方向 A 里 `ctx` 由我们自己的内核提供，方向 B 里 `ctx` 由宿主提供、我们做一层适配。这样模块代码完全不需要感知自己跑在哪种模式里。

## 3. 分层

```
anime_rss/
  domain/            纯逻辑：标题归一化、跨源合并、查询参数解析、放送表排序
    ├── merge.py         无 IO，无第三方依赖，可单测
    ├── query.py
    └── models.py
  ports/             抽象接口（Protocol），只有签名没有实现
    ├── http.py          route(method, path, fn) / static(prefix, dir) / serve()
    ├── store.py         feeds / items / watches / schedule / meta / tx()
    ├── bus.py           on(event, fn) / emit(event, payload)
    ├── clock.py         now()
    └── log.py
  adapters/
    ├── standalone/      stdlib ThreadingHTTPServer + sqlite3（当前形态）
    ├── host/            Flask / FastAPI / 自定义宿主适配（方向 B）
    └── null/            测试替身：内存 store、假时钟、记录型 bus
  kernel/            内核：把 ports 组装起来，提供 register 能力
    ├── app.py          App：setup → start → serve → stop
    ├── router.py       路由表 + 前缀归属 + 统一 JSON/错误
    ├── registry.py     插件发现、依赖排序、版本校验
    ├── config.py       合并所有模块的设置 schema
    ├── jobs.py         定时任务（替代裸 thread）
    └── assets.py       静态资源聚合（/assets/<mod>/）
  modules/           内置模块（也被当作插件对待，接口完全一致）
    ├── feeds/         抓取、镜像状态、RSS 导出
    ├── reader/        已读、收藏、监视规则
    ├── schedule/      新番放送表
    ├── settings/      设置中心（schema 驱动 UI）
    └── desktop/       独立窗口、关窗询问（仅 win32，可禁用）
  plugin_api.py      对外发布的契约：Manifest / Context / 装饰器
  host.py            方向 B 入口：export(host) / create_app(config)
  cli.py             命令行入口（console_scripts）
```

依赖方向严格单向：`modules → kernel → ports ← adapters`，`domain` 谁都不依赖。**模块之间不许互相 import**，只能通过 `ctx.bus` 通信。

## 4. 契约定义

### 4.1 插件清单

```python
# anime_rss/plugin_api.py
class Manifest(NamedTuple):
    id: str                 # 唯一标识，也是表前缀与 API 前缀
    name: str               # 显示名
    version: str            # 语义化版本
    api_version: str        # 依赖的宿主契约版本，如 "1"
    requires: tuple         # 依赖的其他插件 id
    capabilities: frozenset # {"http", "db", "jobs", "ui", "desktop"}
    entry: str              # 模块内 setup 函数路径，默认 "<pkg>:setup"
```

版本规则：内核暴露 `API_VERSION = "1"`；`api_version` 不匹配 → **拒绝加载并给出明确错误**，不做静默降级。缺 `capabilities` 声明的能力 → 加载时直接报错，而不是运行到一半 500。

### 4.2 上下文（插件能拿到的全部）

```python
def setup(ctx):
    ctx.say("schedule 模块启动")

    ctx.router.get("/api/schedule", list_handler)       # 自动挂到 /api/schedule/*
    ctx.router.post("/api/schedule/refresh", refresh)

    ctx.settings.define(group="schedule", items=[...])  # 设置页自动出现，无需改中央文件
    ctx.every(3600, refresh_all, name="schedule.refresh")  # 替代裸 thread，退出时自动收尾
    ctx.on("refresh.finished", on_refresh_done)         # 跨模块只走事件
```

前端视图不在这里写代码，而是**声明**（见 4.5）：

```python
class HelloModule(Module):
    views = ({"id": "hello", "title": "示例插件", "script": "view.js",
              "style": "view.css", "order": 50},)
    assets = "static"          # → /plug/hello/view.js
```

三条边界规则（守住这三条，加十个模块也不会重新腐化）：

- 跨模块只走 `bus` 事件，不 import；
- 只碰自己 `mod_<id>_` 前缀的表（走 `ctx.db`，不直接拿连接）；
- 只注册 `/api/<id>/` 与 `/assets/<id>/` 前缀，内核负责冲突检测。

### 4.3 数据隔离

单个 SQLite 文件，表名加前缀 `mod_schedule_entry`、`mod_feeds_item`，迁移记录在 `mod_<id>_migration`。**不拆成多个库**——拆了就没有跨模块事务，一次抓取要写三张表时会很难看。

### 4.4 前端契约（无构建，单一命名空间）

```js
window.ARR = {
  version: 1,
  registerView({ id, title, icon, mount(el, ctx), unmount }),  // 侧栏自动多一个入口
  getView(id), views(),                                        // 查已注册视图
  on(event, fn) / off(event, fn) / emit(event, payload),        // 与后端 bus 同名事件
  toast(msg), esc(str), openItem(id),                           // 常用小工具
  api: { get(path), send(method, path, body) }                  // 同一套请求封装
}
```

`view.js` 里能拿到的 `ctx`（**只增不改**，废弃字段保留两个版本）：

| 字段 | 说明 |
|---|---|
| `ctx.id` | 视图 id |
| `ctx.api.get/send` | 与主程序同一套 fetch 封装 |
| `ctx.toast/esc` | 提示条、转义 |
| `ctx.on/off/emit` | 前端事件总线（与后端 bus 同名，但各走各的进程） |
| `ctx.openItem(id)` | 跳到阅读区打开某条更新 |
| `ctx.theme/boot/baseUrl` | 主题、首屏数据、服务地址 |

加载方式：后端 `bootstrap.views` 给出清单（含脚本、样式 URL），前端按清单 `<script>` 顺序加载；
模块静态目录由内核挂到 `/plug/<模块 id>/`，**不打包、不 import()、不做哈希**。
改动 `view.js` 刷新页面即生效，这是“改一行就能看到”的根基。

### 4.5 三个内核能力（P3 落地）

| 能力 | 文件 | 用法 | 语义 |
|---|---|---|---|
| 事件总线 | `kernel/bus.py` | `ctx.on(event, fn)` / `ctx.emit(event, payload)` | 同步分发；handler 抛异常只记日志，不影响发布方与其它订阅方；支持 `"*"` 通配与 `once` |
| 定时任务 | `kernel/jobs.py` | `ctx.every(秒, fn, name=)` / `ctx.later(延迟, fn)` | 单调度线程 + 每次执行一个工作线程；上次没跑完就跳过这一拍；`jobs.stop()` 在退出时统一收尾 |
| 前端视图 | `kernel/views.py` | 模块声明 `views` + `assets` | 视图 id 重名/脚本缺失 → 只跳过该视图并记日志，模块照常加载；静态文件读取做真实路径校验，`..`/绝对路径/符号链接逃逸一律 404 |

约定事件名 `命名空间.动作`：`app.started`、`app.stopping`、`refresh.started`、`refresh.finished`、
`feed.updated`。第三方请用自己的模块 id 作前缀，不要占用别人的命名空间。

运行状态可观测：`GET /api/jobs` 看每个任务的 `runs / errors / nextIn / lastError`，
`GET /api/views` 看当前注册的所有视图。模块不需要自己写监控接口。

## 5. 方向 B：本项目作为别人的插件

提供三种接入姿态，覆盖绝大多数宿主：

| 姿态 | 入口 | 适用 |
|---|---|---|
| 进程内嵌入 | `anime_rss.host.create_app(...)` → `App`，宿主调 `app.handle(method, path, body)` 拿 `Response` | 宿主是 Python 程序（Flask/FastAPI/Tkinter/另一个内核） |
| WSGI | `anime_rss.host.wsgi_app(...)` → 标准 WSGI callable | 宿主已有 Web 框架（Flask/Django/gunicorn） |
| 注册进宿主路由表 | `anime_rss.host.export(host, ...)` | 宿主有自己的插件系统，我们只把路由交出去 |

```python
# 最小宿主：不引入任何框架，10 行内就能访问
from anime_rss.host import create_app

app = create_app(data_dir="./data/anime")      # 组装，不联网、不起服务
resp = app.handle("GET", "/api/v1/items")      # 拿内核的 Response
print(resp.status, resp.body[:80])
app.stop()                                     # 停任务表、收尾模块、关库
```

宿主对象只要实现 `mount(router)` / `add_route(method, path, fn)` / `route(method, path, fn)`
任意一个，`export(host)` 就能把整张路由表交出去；三个都没有会**明确抛 TypeError**，
不猜也不静默降级。纯 WSGI 宿主用 `wsgi_app()`，返回的函数带 `.app` 与 `.stop()`。

`App` 的生命周期很短：`create_app()` 只组装，`start()` 才启动任务表（自动刷新、启动抓取），
`stop()` 收尾。**嵌入时默认 `fetch_on_start=False`** —— 宿主还没准备好时不该先打一轮网络。

对应的，我们自己暴露 `/api/v1/*` 稳定前缀（`/api/*` 保留为别名，一套 handler 两份路由），
避免以后版本升级把宿主打挂。`api_v1=False` 可以关掉别名。

## 6. 信任与安全边界

- 第三方插件**进程内执行 = 完全信任**，这一点必须写在文档最显眼处，不假装有沙箱；
- 提供 `--no-third-party` 启动开关，只加载内置模块；
- 插件的 `capabilities` 声明会被校验：声明了 `desktop` 但平台不是 win32 → 拒绝加载而不是静默半残；
- 目录穿越、路径注入这类输入校验放在内核（已由 `smoke.py` 覆盖）；沙箱化（子进程 + IPC）列为 P4 之后的备选，不做。

## 7. 迁移阶段与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0** ✅ | `smoke.py` 端到端冒烟 + 打 tag `v1.5.0` | 51 项全通过 |
| **P1** ✅ | 抽 `kernel/`：路由表化、全局变量改 `ctx` 注入、静态资源挂载 | 功能零变化，`smoke.py` 68/68；`app.py` 1295 → 744 行，只剩组装与桌面代码 |
| **P2** ✅ | 按 `schedule` → `feeds` → `reader` → `settings` 拆模块 | 四个模块迁完；`api/core.py` 447 → 84 行；路由 38 条逐条比对不变；27 项单测 + `smoke.py` 68/68 |
| **P3** ✅ | 开放插件发现（entry_points + plugins/ 目录）+ `bus` / `jobs` / 前端视图注册 | 发现机制 + 示例插件（`discovery.py` + `examples/plugins/hello.py`，含自带界面）；`bus` / `jobs` / `views` 三个内核件落地，`auto-refresh` 与启动预热改由任务表管；67 → 69 项单测、`smoke.py` 77/77；无头浏览器实测插件视图可渲染 |
| **P4** ✅ | `create_app()` / `export(host)` / `wsgi_app()` / `/api/v1` + 文档 | `anime_rss/app.py` 一处组装，桌面 `app.py` 的 `main()` 改为调它（不再自己拼部件）；`/api/v1` 别名与原路径同源；92 项单测 + `smoke.py` 80/80；最小宿主示例见 `docs/ARCHITECTURE.md` §5 |

原则：**每一阶段结束时应用都要能跑**。不出现“改到一半不能用”的状态，任何一步不对就停下，不硬推。

## 8. 明确不做的事

- 不引入前端构建链（webpack/vite），保住“改一行刷新即生效”；
- 不做微服务拆分、不做进程外 RPC 作为默认形态；
- 不把 store 拆成每模块一个数据库；
- 不为第三方插件做沙箱（先文档声明信任边界，真有需求再评估）；
- 不做插件市场 / 版本协商矩阵（只做 `api_version` 主版本匹配）。

## 9. 已知风险

| 风险 | 应对 |
|---|---|
| PyInstaller 单文件打包时插件动态发现会漏模块 | 内置模块用**静态清单**注册，不靠目录扫描；第三方插件在打包版里默认不启用 |
| 前端契约变更会悄悄破坏已有视图 | `ARR.registerView` 的 `ctx` 对象只增不改，废弃字段保留两个版本 |
| 端口抽象过度导致代码变啰嗦 | 只抽 http / store / bus / clock / log 五个，别的直接 import；抽象层只允许有一层实现时先不抽 |
| 拆模块期间新旧代码并存导致重复实现 | 一次只搬一个模块，搬完立刻删旧代码，不留“兼容分支” |

## 10. 落地现状（代码与上面的差距）

上面的目录是**目标形态**；下面是**今天仓库里真实存在的东西**，不写清楚容易照着上面的结构去找不存在的文件。

| 目标 | 现状 | 说明 |
|---|---|---|
| `kernel/{router,assets,registry,config,jobs}.py` | `kernel/{http,assets,server,context,bus,jobs,views}.py` + `module.py` + `common.py` | Router 放在 `http.py`；模块注册与版本校验在 `module.py`；`bus` / `jobs` / `views` 已补齐；`config` 由 `Settings.define` 代替，不做独立文件 |
| `ports/` + `adapters/` | ✅ 在独立包 `packages/arrkit/arrkit/{ports,adapters}/` | 主版本**没有**改用它，仍是 `kernel/` 里那份；独立包把 `TransportPort` / `AssetPort` / `ClockPort` 抽成接口，stdlib 实现放 `adapters/`，可单独 `pip install -e packages/arrkit` |
| `domain/` | 逻辑仍在 `schedule.py` / `feeds.py` 里 | 等模块搬完再拆纯函数层，避免一边搬一边改语义 |
| `modules/{feeds,reader,schedule}` | `modules/{feeds,reader,settings,schedule}.py` | 四个模块全部迁完；`api/core.py` 只剩静态页、健康检查与 bootstrap 聚合 |
| 设置 schema 由模块自带 | ✅ `Settings.define(group, after=)` | 每个 `Settings` 实例一份 schema，模块登记、越界照旧夹紧、位置可控 |
| `registry.py` 插件发现 | ✅ `discovery.py` | entry_points（组 `anime_rss.plugins`）+ 本地 `plugins/` 目录；内置优先、坏插件只跳过不拖垮启动；`--no-third-party` 可整体关闭 |
| `bus.py` / `jobs.py` | ✅ `kernel/bus.py` / `kernel/jobs.py` | 跨模块只走 `ctx.on/emit`；定时任务统一进 `ctx.jobs`，退出时 `jobs.stop()` + `unload_modules()` 收尾，`/api/jobs` 可看运行状态 |
| 前端视图注册 | ✅ `kernel/views.py` + `ARR.registerView` | 模块声明 `views`/`assets`，脚本从 `/plug/<id>/` 加载；侧栏入口自动生成，坏视图只跳过自己 |
| `create_app()` / `export(host)` / `/api/v1` | ✅ `anime_rss/app.py` + `anime_rss/host.py` | 桌面与嵌入共用一份组装；`App.handle()` 直接返回 `Response`，`App.wsgi` 是标准 WSGI；`/api/v1` 是同一批 handler 的别名 |
| `plugin_api.py` / `cli.py` | 未单独建文件 | 契约落在 `module.py`（`Module` / `API_VERSION`），命令行入口仍是根目录 `app.py` |

### 独立底座：`packages/arrkit/`

上面第 3 节画的分层（`modules → kernel → ports ← adapters`）在仓库里有两份实现，
**刻意不合并**：

| | 主版本 `anime_rss` | 独立包 `arrkit` |
|---|---|---|
| 位置 | `anime_rss/{kernel,module,discovery,app,host}.py` | `packages/arrkit/arrkit/` |
| 依赖 | 业务（RSS / 放送表 / SQLite） | 零依赖 |
| 管理接口 | `/api/*`（与业务同前缀） | `/_kit/*`（不占业务路径） |
| 设置存储 | SQLite（与业务同库） | 独立 JSON 文件 |
| 测试 | `tests/` 92 项 + `smoke.py` 80 项 | `packages/arrkit/tests/` 76 项 |

两边**互不引用**：`grep arrkit` 在主版本代码里为空，`grep anime_rss` 在 arrkit 里也为空。
这样改任何一边都不会把另一边打挂；将来要让主版本迁到 arrkit 上，是一次显式替换
（把 `anime_rss/kernel/*` 换成 `from arrkit import ...`），可单独评估、单独回滚。

独立包比主版本多出来的东西，正好是「抽出来」才看得清的部分：

- **端口真的分成目录**：`ports/` 只有抽象类，`adapters/` 只有实现，`kernel` 只 import 前者；
- **时钟也是端口**：`FakeClock` 让定时任务测试不用真等 300 秒；
- **`MemoryAssets`**：单文件打包或测试时，把前端资源塞进 dict 就能跑；
- **`/_kit/settings`**：配置读写、归一化、`apply_settings` 回调是框架能力，不再由业务重复实现。

### 现在写一个模块长什么样

```python
from anime_rss.module import Module
from anime_rss.kernel.http import Response


class HelloModule(Module):
    id = "hello"                 # 路由前缀 /api/hello/，表前缀 mod_hello_
    name = "打招呼"
    version = "1.0.0"
    depends = ("feeds",)         # 需要别的模块时写在这里，由内核排顺序

    def setup(self, ctx):
        ctx.settings.define({    # 设置项自带，设置页自动多一个分组
            "group": "hello",
            "title": "打招呼",
            "items": [{"k": "helloName", "type": "text",
                       "label": "称呼", "default": "世界"}],
        }, after="read")
        ctx.router.get("/api/hello", self.api_hello)

    def bootstrap(self, ctx):            # 贡献给首屏的那部分，可选
        return {"hello": {"name": ctx.settings.get().get("helloName")}}

    def apply_settings(self, values):    # 改设置时内核回调，不用自己监听
        self._name = values.get("helloName") or "世界"

    def api_hello(self, req):
        name = getattr(self, "_name", None) or self.ctx.settings.get()["helloName"]
        return Response.json({"ok": True, "text": "你好，%s" % name})
```

丢进 `anime_rss/modules/__init__.py` 的 `BUILTIN_MODULES` 就生效 —— 不用改 `app.py`，也不用改 `core.py`。

内核在 `setup()` 之前会把 `ctx` 挂到模块上，所以 handler 里可以直接写 `self.ctx`。

模块可用hook一览：

| 钩子 | 何时调用 | 用途 |
|---|---|---|
| `setup(ctx)` | 启动时一次 | 建服务、挂路由、登记设置、登记任务/订阅事件 |
| `bootstrap(ctx) -> dict` | 每次 `/api/bootstrap` | 贡献首屏数据；抛异常会被跳过，不影响别人 |
| `apply_settings(values)` | 保存/重置设置后 | 让新配置立刻作用到自己身上 |
| `teardown()` | 进程退出 | 停线程、关连接（任务表已由内核统一停） |
| `views` / `assets`（类属性） | 加载时（`setup` 之后） | 声明前端视图与静态目录；声明式，不用写注册代码 |

### 路由归属（P3 之后的实际情况）

| 路径 | 归属 | 备注 |
|---|---|---|
| `/`、`/index.html`、`/favicon.ico`、`/healthz`、`/api/bootstrap` | core | 外壳与首屏聚合 |
| `/api/views`、`/api/jobs` | core | 已注册视图、任务运行状态（可观测性，模块不用自己写） |
| `/plug/<模块 id>/*` | core | 转发到 `ViewRegistry`，只读各模块声明的静态目录，穿越一律 404 |
| `/api/feeds*`、`/api/mirrors`、`/api/refresh` | feeds | 订阅管理与抓取 |
| `/feed.xml`、`/rss`、`/rss.xml`、`/feed/{id}`、`/opml.xml`、`/feeds.opml` | feeds | **留根路径**：这是用户填进阅读器的订阅地址，改前缀会让已有订阅全部失效 |
| `/api/items`、`/api/item/{id}`、`/api/read*`、`/api/star` | reader | 条目与已读收藏 |
| `/api/watches*` | reader | 关注规则 |
| `/api/settings*`、`/api/maintenance` | settings | 设置与数据库维护 |
| `/api/schedule*` | schedule | 新番放送表 |

新模块请遵守 `/api/<id>/` 前缀；上面那些不带模块名的老路径是为了兼容保留的，不要再新增同类例外。
