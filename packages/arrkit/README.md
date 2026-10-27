# arrkit

零依赖的模块化微内核。给「一个进程、一堆功能模块、可能被别的程序嵌进去」的项目用。

```python
from arrkit import Module, create_kernel

class Hello(Module):
    id, name, version = "hello", "问候", "0.1.0"

    def setup(self, ctx):
        ctx.router.get("/api/hello", lambda req: {"ok": True, "hi": "world"})
        ctx.every(60, self.tick, name="hello.tick")

    def tick(self):
        self.ctx.emit("hello.tick", {})

k = create_kernel(modules=[Hello], static_dir="static", port=8899)
k.serve()
```

打开 `http://127.0.0.1:8899/api/hello`，同时白拿了 `/_kit/health`、`/_kit/modules`、
`/_kit/jobs`、`/_kit/bootstrap` 这一套诊断接口。

---

## 和主版本的关系

这个包是**从 AnimeRSSReader 里抽出来的独立底座**，放在 `packages/arrkit/`：

| | 主版本 `anime_rss` | 本包 `arrkit` |
|---|---|---|
| 依赖 | 动漫业务（RSS、放送表、数据库） | **零第三方依赖** |
| 内核位置 | `anime_rss/kernel/` 自带一份 | `packages/arrkit/arrkit/` 自成一包 |
| 管理前缀 | `/api/*` | `/_kit/*`（不抢业务路径） |
| 版本 | 跟主程序走 | 自己的 `0.1.0` 与 `API_VERSION=1` |
| 安装 | 跑仓库就行 | `pip install -e packages/arrkit` |

**两边互不影响**：主版本一行没改，也不 import 这里；这里的改动不会进入主版本的
运行路径。主版本仍然可以用 `smoke.py` 与 `tests/` 独立验证。将来如果要让主版本
也用这个底座，那是一次**显式的迁移**，而不是隐式的耦合。

---

## 五个能力、一条契约

| 能力 | 入口 | 解决什么 |
|---|---|---|
| 路由 | `ctx.router` / `Router` | handler 只认 `Request`/`Response`，不认服务器 |
| 事件 | `ctx.on` / `ctx.emit` / `Bus` | 模块之间不互相 import |
| 任务 | `ctx.every` / `ctx.later` / `Jobs` | 代替裸线程，退出统一收尾、失败可查 |
| 视图 | `Module.views` / `ViewRegistry` | 模块自带前端入口，内核不知道界面长什么样 |
| 配置 | `ctx.settings` / `Settings` | 模块自己声明 schema，内核负责归一化与持久化 |

一条契约是 `Module`：

```python
class Module:
    id = ""            # 唯一，同时作为路由前缀与表前缀
    name = ""
    version = "0.0.0"
    api_version = "1"  # 与内核不匹配会被拒绝加载，不静默降级
    depends = ()       # 依赖的其它模块，内核做拓扑排序
    views = ()         # 前端视图声明
    assets = ""        # 自带静态目录（相对模块文件）

    def setup(self, ctx): ...          # 挂路由、声明配置、起任务
    def bootstrap(self, ctx): ...      # 往 /_kit/bootstrap 里加一段（可选）
    def apply_settings(self, values): ...  # 配置保存后立刻生效（可选）
    def teardown(self): ...            # 退出收尾（可选）
```

模块拿能力的唯一通道是 `ctx`：

```python
ctx.router / ctx.settings / ctx.bus / ctx.jobs / ctx.views
ctx.data_dir / ctx.static_dir / ctx.app_name / ctx.version / ctx.port
ctx.module(id)        # 取别的模块（谨慎用；跨模块说话优先走事件）
ctx.service(name)     # 取宿主注入的服务对象（数据库、邮件…）
ctx.emit / ctx.on / ctx.every / ctx.later / ctx.say
```

---

## 端口与适配器

内核只认接口，不认实现：

```
adapters ──实现──▶ ports ◀──依赖── kernel
```

| 端口 | 作用 | 自带实现 |
|---|---|---|
| `TransportPort` | 把 `dispatch` 挂到 socket 上 | `StdlibTransport`（ThreadingHTTPServer） |
| `AssetPort` | 按「目录 + 文件名」取字节 | `FilesystemAssets`、`MemoryAssets` |
| `ClockPort` | 任务调度的时间来源 | `SystemClock`、`FakeClock`（测试用） |

换掉它们不用改内核一行：

```python
k = create_kernel(transport=MyUvicornTransport(), asset_port=MemoryAssets({...}))
```

`FakeClock` 让定时任务测试不用真等：

```python
clock = FakeClock()
k = create_kernel(clock=clock, modules=[Poll])
clock.advance(300)
k.jobs.run_pending()      # 立刻跑一拍
```

---

## 内置管理接口

全部在 `/_kit/` 下（可改 `admin_prefix`），**故意不占** `/api/`：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/_kit/health` | 存活、版本、模块数 |
| GET | `/_kit/status` | 内核状态（数据目录、视图、任务数） |
| GET | `/_kit/modules` | 已加载模块清单 |
| GET | `/_kit/views` | 前端视图清单（脚本/样式 URL） |
| GET | `/_kit/jobs` | 任务表：间隔、次数、失败数、下次时间 |
| POST | `/_kit/jobs/{name}/run` | 立刻跑一次 |
| GET | `/_kit/bootstrap` | 首屏聚合：内核字段 + 各模块 `bootstrap()` |
| GET/POST | `/_kit/settings` | 读 / 保存配置（保存会回调 `apply_settings` 并发事件） |
| POST | `/_kit/settings/reset` | 恢复默认值 |

模块自带的前端资源挂在 `/plug/<模块 id>/<文件>`，目录穿越会被拒绝。

---

## 模块自带界面

后端只做三件事：收声明、挂静态目录、把清单塞进 `/_kit/bootstrap`。
界面由模块自己的 `view.js` 画出来：

```python
class Clock(Module):
    id = "clock"
    assets = "static/clock"
    views = ({"id": "clock", "title": "时钟",
              "script": "view.js", "style": "view.css", "order": 10},)
```

```js
// static/clock/view.js
KIT.registerView({
  id: "clock",
  mount: function (el, ctx) {
    el.innerHTML = "<h2>时钟</h2>";
    ctx.api("/api/clock").then(d => { el.innerHTML = d.time; });
  },
  unmount: function () { /* 清定时器 */ }
});
```

`examples/static/app.js` 里有一份约 120 行的 `KIT` 最小实现（`registerView` /
`api` / `esc` / `toast`），可以直接抄。**不引入打包器**——这是「改一行刷新就生效」的根基。

---

## 装进别人的程序

```python
# ① 宿主有自己的路由表
from arrkit.hosting import export
k = export(my_host, app_name="我的平台", modules=[Clock])
...
k.stop()

# ② 宿主是 Flask / Django / gunicorn（任何 WSGI 容器）
from arrkit.hosting import wsgi_app
site = wsgi_app(app_name="我的平台", modules=[Clock])
```

宿主要实现 `mount(router)` / `add_route(method, path, fn)` / `route(method, path, fn)`
任意一个。都没有就明确抛 `TypeError` —— 与其猜，不如让调用方写清楚。

不想走 HTTP 也行：

```python
app = create_kernel(modules=[Clock])
resp = app.handle("GET", "/api/clock")     # 直接拿 Response
resp.status, resp.body
```

---

## 插件发现

两个来源，都不用改内核：

**① 丢进目录**（默认 `data_dir/modules/`，可配）

```bash
echo 'from arrkit import Module' > data/modules/my_plug.py
```

**② 打成 Python 包**，在 `pyproject.toml` 声明入口点：

```toml
[project.entry-points."arrkit.modules"]
my_plug = "my_plug:MyModule"
```

规则：**内置优先**（插件不能顶掉已装模块的 id），**插件出错不拖垮启动**
（加载失败只记录原因）。排障时用 `no_plugins=True` 一次关掉全部第三方。

---

## 目录

```
packages/arrkit/
├─ arrkit/
│  ├─ __init__.py      公开 API（一层导出，别处只 from arrkit import …）
│  ├─ version.py       __version__ 与 API_VERSION
│  ├─ http.py          Request / Response / Router
│  ├─ dispatcher.py    先静态、再路由、最后 404
│  ├─ bus.py           事件总线
│  ├─ jobs.py          定时任务（单调度线程 + 每任务工作线程）
│  ├─ views.py         前端视图注册与安全读取
│  ├─ settings.py      schema 驱动的配置
│  ├─ module.py        模块契约、拓扑加载、收尾
│  ├─ discovery.py     entry_points + 目录扫描
│  ├─ admin.py         /_kit/* 管理接口
│  ├─ kernel.py        组装：KernelConfig / Kernel / create_kernel
│  ├─ hosting.py       export(host) / wsgi_app()
│  ├─ testing.py       Client（不走网络打内核）
│  ├─ ports/           抽象接口（不许 import 实现）
│  └─ adapters/        具体实现（stdlib 传输、文件/内存资源、时钟）
├─ examples/
│  ├─ mini_app.py      三个模块的完整示例
│  └─ static/          示例前端（含 120 行 KIT 最小实现）
└─ tests/              76 项，只依赖标准库
```

---

## 跑起来 / 跑测试

```bash
cd packages/arrkit
python examples/mini_app.py                    # http://127.0.0.1:8899
python -m unittest discover -s tests           # 76 项
pip install -e .                               # 装成包，别处 from arrkit import …
```

要求 Python ≥ 3.9，没有别的依赖。
