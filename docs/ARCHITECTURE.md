# 架构设计：薄内核 + 双向插件契约

> 目标版本线：v1.5.0 → v2.0.0
> 状态：设计已定稿，P0 完成，P1 待启动

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
    ctx.log.info("schedule 模块启动")

    ctx.router.get("/api/schedule", list_handler)       # 自动挂到 /api/schedule/*
    ctx.router.post("/api/schedule/refresh", refresh)

    ctx.settings.define(group="schedule", items=[...])  # 设置页自动出现，无需改中央文件
    ctx.db.migrate("001_init", SQL)                     # 自己前缀的表 + 迁移记录
    ctx.jobs.every(3600, refresh_all)                   # 替代裸 thread，退出时自动收尾
    ctx.bus.on("item.new", on_item)                     # 跨模块只走事件
    ctx.ui.add_view(id="schedule", title="新番放送表",
                    script="schedule.js", style="schedule.css")
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
  registerView({ id, title, icon, mount(el, ctx), unmount }),   // 侧栏自动出现
  registerSettingPanel({ id, render(el, schema) }),             // 需要自定义控件时用
  registerCommand({ id, title, keys, run }),                    // 快捷键/命令面板
  on(event, fn), emit(event, payload),                          // 与后端 bus 同名事件
  api: { get(path), post(path, body) }                          // 自动加模块前缀
}
```

模块自带 `static/<id>/` 目录，内核挂到 `/assets/<id>/`，按需 `import()` 加载。**不引入打包器**——这是这个项目“改一行就能生效”的根基。

## 5. 方向 B：本项目作为别人的插件

提供两种接入姿态，覆盖绝大多数宿主：

| 姿态 | 入口 | 适用 |
|---|---|---|
| 进程内嵌入 | `anime_rss.create_app(config) → App`，宿主调 `app.start()`，或直接 `app.handle(method, path, body)` | 宿主是 Python 程序（Flask/FastAPI/Tkinter/另一个内核） |
| 作为宿主插件 | `anime_rss.host.export(host_ctx)` | 宿主有自己的插件系统，我们只注册路由/视图/命令 |
| 进程外 | `python -m anime_rss --api-only --port N`，走 `/api/v1` | 跨语言、跨机器，最省事 |

`export(host_ctx)` 只需要宿主提供三个方法：`route(method, path, fn)`、`static(prefix, dir)`、`config(schema)`。缺哪个就用适配器补：没有 `route` 就退化成自己起一个线程内服务；没有 `config` 就用内存配置。**宿主不需要为了接我们改架构。**

对应的，我们自己暴露 `/api/v1/*` 稳定前缀（当前 `/api/*` 保留为别名），避免以后版本升级把宿主打挂。

## 6. 信任与安全边界

- 第三方插件**进程内执行 = 完全信任**，这一点必须写在文档最显眼处，不假装有沙箱；
- 提供 `--no-third-party` 启动开关，只加载内置模块；
- 插件的 `capabilities` 声明会被校验：声明了 `desktop` 但平台不是 win32 → 拒绝加载而不是静默半残；
- 目录穿越、路径注入这类输入校验放在内核（已由 `smoke.py` 覆盖）；沙箱化（子进程 + IPC）列为 P4 之后的备选，不做。

## 7. 迁移阶段与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0** ✅ | `smoke.py` 端到端冒烟 + 打 tag `v1.5.0` | 51 项全通过 |
| **P1** | 抽 `kernel/`：路由表化、全局变量改 `ctx` 注入、静态资源挂载 | 功能零变化，`smoke.py` 仍 51/51；`app.py` 只剩组装代码 |
| **P2** | 按 `schedule` → `feeds`/`reader` → `settings` 拆模块 | 每拆一个跑一次冒烟，全绿才算完 |
| **P3** | 开放插件发现（entry_points + plugins/ 目录）+ 写示例插件验证契约 | 示例插件只靠公开 API 就能跑通 bus + jobs + 视图 |
| **P4** | `create_app()` / `export(host)` / `/api/v1` + 文档 | 用一个最小宿主程序（10 行内）成功加载并访问 |

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
