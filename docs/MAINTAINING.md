# 开发与维护

给改代码、打发布、排障的人看。**用户向的说明在 [README](../README.md)**，
模块化与插件契约的设计取舍在 [ARCHITECTURE](ARCHITECTURE.md)。

- [文件结构](#文件结构)
- [架构一句话](#架构一句话)
- [测试与回归](#测试与回归)
- [打包发布](#打包发布)
- [API](#api)
- [插件](#插件)
- [嵌入到别的项目](#嵌入到别的项目)
- [独立底座 arrkit](#独立底座-arrkit)
- [环境变量](#环境变量)
- [排障](#排障)
- [发版清单](#发版清单)

## 文件结构

```
anime-rss-reader/
├─ app.py              组装层：拼上下文、加载模块、挂路由与静态资源、窗口监督
├─ anime_rss/          应用包（内核 / 模块 / API 三层分开）
│  ├─ app.py           可嵌入组装：create_app / App.start / handle / wsgi / serve
│  ├─ host.py          作为别人插件的入口：export(host) / wsgi_app()
│  ├─ kernel/          薄内核：http 原语、静态资源、服务适配、上下文、事件/任务/视图
│  ├─ module.py        模块契约：Module / 加载顺序 / api_version 校验
│  ├─ common.py        模块共用的视图工具（来源名、设置快照、生效回调）
│  ├─ discovery.py     插件发现：entry_points + 本地 plugins/ 目录
│  ├─ runtime.py       打包后输出兜底：无控制台时把 stdout/stderr 接到 data/logs/app.log
│  ├─ userdata.py      用户数据：一致性快照、导出 zip、校验、恢复、老数据搬迁
│  ├─ modules/         内置模块：feeds / reader / settings / schedule / userdata
│  └─ api/             外壳：静态页、健康检查、/api/bootstrap 聚合、/api/jobs
├─ feeds.py            聚合引擎：多站抓取、镜像轮换、通用 RSS 解析、导出
├─ sources.py          各站适配层（源自原仓库，新增 AnimeGarden）
├─ schedule.py         新番放送表：四平台抓取、跨平台合并、周历缓存
├─ settings.py         用户设置：默认值、校验、持久化、后台定时刷新线程
├─ textutil.py         清洗：纯文本、外链、磁力规整、时间解析
├─ store.py            SQLite 存储：去重写入、查询、已读收藏、关注匹配、放送表
├─ static/             index.html + style.css + app.js（原生 JS，无框架）
├─ tests/              单测与模块契约（tests/test_*.py）
├─ smoke.py            端到端冒烟，起真服务打真接口
├─ examples/plugins/   示例插件；packages/arrkit/ 独立内核包
├─ build_exe.py        单文件打包；build_icon.py 生成图标
├─ tools/check_subsystem.py  检查 exe 是窗口程序（2）还是控制台程序（3）
├─ userdata/           用户数据（运行时生成，已 gitignore）
└─ data/               运行残留：日志、浏览器配置、自动备份、导出落点
```

`DATA_DIR`（用户数据，可迁移）与 `RUNTIME_DIR`（本机残留）严格分开：模块要日志、浏览器配置、
备份落点一律用 `ctx.runtime_dir`，只有库和 `hosts.json` 在 `ctx.data_dir`。

## 架构一句话

`app.py` 拼 `Context` → 按 `anime_rss.modules.BUILTIN_MODULES` 顺序 `setup(ctx)` → 每个模块自己挂
`/api/<id>/...` 路由与设置项 → 内核统一管路由、静态资源、事件总线、定时任务、前端视图。
加功能优先写模块，不改主程序。

## 测试与回归

```bash
python -m unittest discover -s tests        # 单测 + 模块契约
python smoke.py                             # 端到端冒烟（自己起服务、自己收尾）
node --check static/app.js                  # 前端语法
cd packages/arrkit && python -m unittest discover -s tests   # 独立底座
```

基线（v2.1.5）：**单测 142、冒烟 93、arrkit 全过**；插件侧 linkwake 134 项
（含 jsdom 前端 131 条）、arr_search 68 项。

改了接口就同步 `tests/test_module.py` 里的
模块清单与路由断言，冒烟里补一条对应检查 —— 这两处是重构的安全网，别让它们比代码旧。

## 打包发布

```bash
pip install pyinstaller
python build_exe.py            # 单文件，dist/ 下，窗口程序（无黑框）
python build_exe.py --onedir   # 文件夹版，启动更快、便于排查
python build_exe.py --console  # 保留黑框，调试时能看实时日志
```

PyInstaller 不支持交叉编译，要哪个平台就在哪个平台跑一次。注意：

- **必须用装了 PyInstaller 的解释器**。若 `python build_exe.py` 报「需要先安装 PyInstaller」，
  换成有它的 venv（本机是 `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`）。
- **新增模块要加 hidden-import**：`build_exe.py` 里静态列了内核与内置模块，漏了打包后就是
  `ModuleNotFoundError`（2.1.0 补过 `anime_rss.userdata` / `anime_rss.modules.userdata`）。
- **黑框**：默认打成 `WINDOWS_GUI`（子系统 2）。代价是 `sys.stdout/stderr` 为 `None`，
  所以启动横幅与访问日志写到 `data/logs/app.log`（超 1 MB 重开），启动失败弹窗而不是静默退出。
  确认产物类型：`python tools/check_subsystem.py dist/AnimeRSSReader.exe`。
- 图标由 `build_icon.py` 用纯标准库光栅化生成，换配色改文件顶部常量。

## API

`/api/...` 都有 `/api/v1/...` 别名（同一批 handler），新脚本用版本化前缀。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/bootstrap` | 频道、计数、站点、局域网地址 |
| GET | `/api/items?feed=&filter=&q=&limit=&offset=` | 条目列表（`filter` = `all`/`unread`/`starred`/`magnet`） |
| GET | `/api/item/<id>` | 单条详情 |
| POST | `/api/read` | `{ids:[...], read:true}` |
| POST | `/api/star` | `{id, starred:true}` |
| POST | `/api/read-all` | `{feed}` 整个频道标为已读 |
| POST / GET | `/api/refresh` | 触发抓取（后台跑）/ 查进度 |
| POST | `/api/feeds` | `{title, url}` 添加自定义订阅 |
| PATCH / DELETE | `/api/feeds/<id>` | 改名 / 启停 / 改筛选参数 · 删除 |
| GET | `/api/watches` | 关注列表（命中数、未读数、剩余保留天数） |
| POST | `/api/watches` | `{name, include, exclude, fansub}` 新建并立即匹配 |
| PATCH / DELETE | `/api/watches/<id>` | 改名 / 改关键词 / 启停（重新匹配）· 删除 |
| POST | `/api/watches/<id>/scan` | 手动重跑一次匹配 |
| GET | `/api/schedule` | 放送表（`?refresh=1` 顺带触发重抓） |
| GET / POST | `/api/schedule/status` · `/api/schedule/refresh` | 抓取进度 · 强制重抓 |
| GET | `/api/mirrors` | 各站点入口状态（可用 / 冷却中 / 未检测） |
| GET / POST | `/api/settings` | 当前值 + 类型定义 + 运行信息 · `{values:{...}}` 改传进来的几项 |
| POST | `/api/settings/reset` | 恢复默认值 |
| POST | `/api/maintenance` | `{action:"clean"}` 清理旧条目 / `{action:"vacuum"}` 压缩库 |
| POST | `/api/open-window` | 让已有实例把界面窗口重新打开 |
| GET | `/api/userdata` | 用户数据目录、统计、最近备份、库版本 |
| GET | `/api/userdata/export` | 下载用户数据 zip（一致性快照） |
| POST | `/api/userdata/import` | 请求体即 zip 字节：校验 → 自动备份 → 整份替换 → 重载 |
| POST | `/api/userdata/reveal` | 在文件管理器里打开用户数据文件夹 |
| GET | `/api/views` · `/api/jobs` | 前端视图清单 · 定时任务状态 |
| GET | `/plug/<模块 id>/<文件>` | 模块自带前端资源 |
| GET | `/feed.xml`、`/feed/<id>.xml`、`/opml.xml` | RSS / OPML 导出 |

`/api/items` 的 `feed` 也接受 `watch:<关注id>`，取该关注命中的条目。

## 插件

加功能不用改主程序：写一个模块丢进 `plugins/`，或打成带入口点的包 `pip install`。

```bash
mkdir -p plugins && cp -r examples/plugins/* plugins/ && python app.py
```

日志多一行 `模块 hello v0.2.0 示例插件`，侧栏多一个入口，`GET /api/hello` 可用。

```toml
[project.entry-points."anime_rss.plugins"]
hello = "my_hello_plugin:HelloModule"
```

| 钩子 | 用途 |
|---|---|
| `setup(ctx)` | 挂路由（`/api/<id>/...`）、登记设置项、登记定时任务、订阅事件 |
| `bootstrap(ctx)` | 往首屏数据加一段；抛异常会被跳过，不影响别的模块 |
| `apply_settings(values)` | 设置保存后回调，让新配置立刻生效 |
| `teardown()` | 退出前收尾（任务表已由内核统一停） |
| `views` / `assets` | 声明前端视图与静态目录，侧栏入口自动出现 |

`ctx` 上有 `store` / `settings` / `refresher` / `router` / `assets` / `bus` / `jobs` / `views` /
`data_dir` / `runtime_dir` / `port` / `module(id)`。不想自己起线程就用
`ctx.every(60, fn, name="hello.tick")`，内核统一调度，`GET /api/jobs` 能看到次数与下次时间。
加界面就声明视图 + `ARR.registerView({id, mount})`，工具入口、脚本加载、切走卸载都由主程序管。
要和别的插件协作（比如「把手上的链接交给下载器」）就用 `ARR.provide / can / invoke`
登记一项能力，**不要**去 import 另一个插件的代码或判断它的 id。

**几句实话**：插件与主程序同进程、权限完全一样，没有沙箱，只装信得过的；插件不能顶掉内置模块的
id（重名跳过）；插件写坏只会被跳过并打印原因，不会让程序起不来；排障用 `--no-third-party`。

## 嵌入到别的项目

反方向也支持：把阅读器当成别人项目里的模块，**不起进程、不用端口**。

```python
from anime_rss.host import create_app

app = create_app(data_dir="./userdata/anime")   # 组装，不联网、不起服务
resp = app.handle("GET", "/api/v1/items")       # 拿内核的 Response
print(resp.status, resp.body[:80])
app.stop()                                      # 停任务表、收尾模块、关库
```

宿主已有 Web 框架时用 WSGI 或交出路由表：

```python
from anime_rss.host import wsgi_app, export

site = wsgi_app(data_dir="./userdata/anime")        # 标准 WSGI callable
app = export(my_host, data_dir="./userdata/anime")  # 宿主有 route/add_route/mount 即可
```

`data_dir` 是用户数据目录（库 + 镜像状态）；要另外指定日志 / 浏览器配置的位置，加
`runtime_dir="./data/anime"`。`create_app()` 只组装，`start()` 才启动任务表，嵌入时默认不主动
抓取网络，需要就传 `fetch_on_start=True`。

## 独立底座 arrkit

内核（路由 / 事件 / 定时任务 / 前端视图 / 模块契约 / 端口适配器）抽成零依赖独立包，
在 `packages/arrkit/`，可脱离本项目使用：`pip install -e packages/arrkit`。

```python
from arrkit import Module, create_kernel

class Hello(Module):
    id, name, version = "hello", "问候", "0.1.0"
    def setup(self, ctx):
        ctx.router.get("/api/hello", lambda req: {"hi": "world"})

k = create_kernel(modules=[Hello], static_dir="static", port=8899)
k.serve()
```

两边**互不影响**：主版本用 `anime_rss/kernel/` 里自带那份，不 import arrkit；arrkit 也不 import
任何业务代码。各有各的版本号、测试与运行路径（见 `packages/arrkit/README.md`、`examples/mini_app.py`）。
让主版本改用它是一次显式迁移，不是隐式耦合。

## 环境变量

| 变量 | 作用 |
|---|---|
| `PORT` / `HOST` | 同 `--port` / `--host` |
| `ANIME_RSS_USERDATA` | 用户数据目录（同 `--user-data`） |
| `ANIME_RSS_DATA` | 运行目录（日志 / 备份 / 浏览器配置）；**设了它以后用户数据也放这里**，兼容 2.1 之前的用法 |
| `ANIME_RSS_ON_CLOSE` | 同 `--on-close`：`ask` / `exit` / `background` |
| `ANIME_RSS_KEEP_ALIVE=1` | 等价于 `--on-close background` |
| `ANIME_RSS_QUIET=1` | 不打印访问日志 |
| `ANIME_RSS_PLUGIN_DEBUG=1` | 插件加载失败时打印完整堆栈 |
| `ANIME_RSS_DMHY_HOST` | 自己的动漫花园反代，排在最前面优先使用 |
| `HTTPS_PROXY` | 走代理抓取 |

## 排障

| 现象 | 先看哪里 |
|---|---|
| 双击没反应 / 起来就退出 | `data/logs/app.log`（窗口程序没有控制台，异常弹窗 + 进日志） |
| 打包后 `ModuleNotFoundError` | `build_exe.py` 漏了对应 `--hidden-import` |
| 插件没生效 | 启动日志里的跳过原因；`ANIME_RSS_PLUGIN_DEBUG=1` 看堆栈；`--no-third-party` 对照 |
| 抓不到内容 | 左下角「镜像」状态；`ANIME_RSS_DMHY_HOST`；必要时 `HTTPS_PROXY` |
| 关窗行为不对 | `--on-close` 与设置里的「程序行为」谁优先：命令行 > 环境变量 > 设置 |
| 数据看着不对 | `GET /api/userdata` 看目录、统计、库版本；`data/backups/` 里有恢复前的自动备份 |

窗口相关的坑（历史上踩过，别再踩）：

- 独立窗口是 Chrome/Edge 的 `--app`，配置目录固定在 `data/.window/`；最小化窗口 `GetWindowRect`
  返回 -32000，判断窗口存在要看 `EnumWindows` 列表，不能只看坐标。
- 关窗询问框**不要**为了「垫在后面」去重开窗口：会白闪一帧 + 按 `--window-size` 重落位。
  `MessageBoxW` 也不按父窗位置摆，得弹出来后再 `SetWindowPos`（10ms 轮询，偏移 ≤2px 不动）。

## 发版清单

1. 版本号三处同步：`app.py` 的 `VERSION`、`anime_rss/__init__.py` 的 `__version__`、`build_exe.py`。
2. 新增模块 / 包 → 补 `build_exe.py` 的 `--hidden-import`。
3. `python -m unittest discover -s tests`、`python smoke.py`、`node --check static/app.js` 全过。
4. 重打 `dist/`，`tools/check_subsystem.py` 确认子系统为 2，属性面板版本正确。
5. README 里会过期的数字：冒烟检查条数、exe 体积、测试基线（本文「测试与回归」一节）。
