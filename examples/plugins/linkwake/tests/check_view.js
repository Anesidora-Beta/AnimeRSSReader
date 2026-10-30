/* 前端视图的断言，跑在 jsdom 里。
 *
 * 为什么值得单独跑：view.js 只有 500 多行，但它决定了「点了以后有没有回音」。
 * 后端测试再绿，前端把 r.ok 判反、把过滤参数拼错，用户看到的还是坏界面。
 *
 * 用法：
 *   node tests/check_view.js
 * jsdom 不是插件的依赖（插件要能直接丢进 exe 用，不能带 node_modules），
 * 所以按「环境变量 → 全局 → 上级目录」的顺序找，找不到就跳过并说明。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const HERE = __dirname;
const PLUGIN = path.dirname(HERE);
const VIEW = path.join(PLUGIN, "static", "view.js");

function loadJsdom() {
  const tries = [
    "jsdom",
    process.env.LW_JSDOM,
    path.join(PLUGIN, "node_modules", "jsdom"),
    path.join(PLUGIN, "..", "..", ".jsdom", "node_modules", "jsdom"),
    path.join(PLUGIN, "..", "..", "..", ".jsdom", "node_modules", "jsdom"),
  ].filter(Boolean);
  for (const t of tries) {
    try {
      return require(t);
    } catch (e) {
      /* 换下一条 */
    }
  }
  return null;
}

const jsdom = loadJsdom();
if (!jsdom) {
  console.log("跳过：没找到 jsdom。装一个再跑 ——\n" +
    "  npm i jsdom            # 全局或临时目录都行\n" +
    "  LW_JSDOM=/path/to/jsdom node tests/check_view.js");
  process.exit(0);
}

// ---------------------------------------------------------------- 夹具
const ENTRIES = [
  {
    id: "i1",
    title: "某番 01 [1080p]",
    source: "测试源",
    size: "1.2 GB",
    pubdate: "2026-10-01",
    ts: Math.floor(Date.now() / 1000) - 3600,
    page: "https://bgm.tv/subject/1",
    starred: true,
    read: false,
    count: 2,
    links: [
      { url: "magnet:?xt=urn:btih:aaa", pid: "magnet", name: "磁力链接",
        badge: "MG", kind: "bt", color: "#5aa9e6", pwd: "", from: "magnet" },
      { url: "https://pan.baidu.com/s/1abc?pwd=k9x2", pid: "baidu",
        name: "百度网盘", badge: "BD", kind: "pan", color: "#306cff",
        pwd: "k9x2", from: "summary" },
    ],
    groups: [
      { pid: "magnet", name: "磁力链接", badge: "MG", kind: "bt",
        color: "#5aa9e6", note: "", urls: ["magnet:?xt=urn:btih:aaa"], count: 1 },
      { pid: "baidu", name: "百度网盘", badge: "BD", kind: "pan",
        color: "#306cff", note: "", pwd: "k9x2",
        urls: ["https://pan.baidu.com/s/1abc?pwd=k9x2"], count: 1 },
    ],
  },
  {
    id: "i2",
    title: "某番 02 [1080p]",
    source: "测试源", size: "", pubdate: "", ts: 0, page: "", starred: false,
    read: false, count: 3,
    links: ["b1", "b2", "b3"].map((h) => ({
      url: "magnet:?xt=urn:btih:" + h, pid: "magnet", name: "磁力链接",
      badge: "MG", kind: "bt", color: "#5aa9e6", pwd: "", from: "magnet",
    })),
    groups: [
      { pid: "magnet", name: "磁力链接", badge: "MG", kind: "bt",
        color: "#5aa9e6", note: "",
        urls: ["magnet:?xt=urn:btih:b1", "magnet:?xt=urn:btih:b2",
               "magnet:?xt=urn:btih:b3"], count: 3 },
    ],
  },
];

const PROVIDERS = [
  { id: "magnet", name: "磁力链接", badge: "MG", kind: "bt", color: "#5aa9e6",
    scheme: "", note: "", default: { mode: "open", scheme: "", exe: "", args: "{url}" } },
  { id: "ed2k", name: "电驴链接", badge: "ED", kind: "bt", color: "#8e7cc3",
    scheme: "", note: "", default: { mode: "open", scheme: "", exe: "", args: "{url}" } },
  { id: "baidu", name: "百度网盘", badge: "BD", kind: "pan", color: "#306cff",
    scheme: "baiduyun", note: "", default: { mode: "web", scheme: "", exe: "", args: "{url}" } },
];

const MODES = [
  { id: "open", text: "系统默认程序" },
  { id: "web", text: "浏览器打开" },
  { id: "copy", text: "只复制不启动" },
  { id: "scheme", text: "客户端协议" },
  { id: "app", text: "指定程序" },
];

const TARGETS = [
  { id: "auto", text: "自动", note: "按「每种链接交给谁」里的设置",
    kinds: ["bt", "pan", "file", "web"] },
  { id: "qb", text: "qBittorrent", note: "直接加进下载队列",
    kinds: ["bt", "file"] },
  { id: "aria2", text: "aria2", note: "直接加进下载队列",
    kinds: ["bt", "file"] },
  { id: "open", text: "系统默认程序", note: "交给系统关联的客户端",
    kinds: ["bt", "pan", "file", "web"] },
  { id: "web", text: "浏览器打开", note: "网盘链接会带上提取码",
    kinds: ["bt", "pan", "file", "web"] },
  { id: "copy", text: "只复制链接", note: "不启动任何程序",
    kinds: ["bt", "pan", "file", "web"] },
  { id: "ask", text: "每次都问我", note: "点链接时弹出选择",
    kinds: ["bt", "pan", "file", "web"] },
];

function kindOf(url) {
  if (/^magnet:|^ed2k:|^thunder:/.test(url)) return "bt";
  if (/pan\.baidu\.com/.test(url)) return "pan";
  return "file";
}

function indexPayload(kind, q, prefs) {
  // 按 kind 过滤，模拟后端真实行为（各档数量因此各不相同）
  const keep = (ln) => kind === "all" ? true
    : kind === "dl" ? ["bt", "pan", "file"].indexOf(ln.kind) >= 0
    : ln.kind === kind;
  const entries = [];
  const counts = { items: 0, links: 0, bt: 0, pan: 0, file: 0, web: 0 };
  for (const e of ENTRIES) {
    const links = e.links.filter(keep);
    if (!links.length) continue;
    const groups = e.groups.filter((g) => keep({ kind: g.kind }));
    if (!groups.length) continue;
    entries.push(Object.assign({}, e, { links, groups, count: links.length }));
    counts.items++;
    counts.links += links.length;
    for (const ln of links) counts[ln.kind]++;
  }
  return {
    ok: true, entries, counts, scanned: 40, kind, q: q || "",
    providers: PROVIDERS, modes: MODES,
    targets: TARGETS,
    prefs: Object.assign({ bt: "auto", pan: "auto", file: "auto", web: "auto" },
      prefs || {}),
    cfg: {
      version: 2,
      apps: {
        magnet: { mode: "open", scheme: "", exe: "", args: "{url}" },
        ed2k: { mode: "open", scheme: "", exe: "", args: "{url}" },
        baidu: { mode: "web", scheme: "", exe: "", args: "{url}" },
      },
      prefs: Object.assign({ bt: "auto", pan: "auto", file: "auto", web: "auto" },
        prefs || {}),
      qb: { url: "", user: "admin", savepath: "", passwordSet: false },
      aria2: { url: "", dir: "", tokenSet: false },
    },
    opt: { allowRemote: false, autoPwd: true, onlyDownloadable: true, pageSize: 40 },
    env: { dataDir: "/tmp", home: PLUGIN, os: "nt", canWake: true },
  };
}

// ---------------------------------------------------------------- 断言器
let pass = 0;
const fails = [];

function ok(cond, name, extra) {
  if (cond) {
    pass++;
  } else {
    fails.push(name + (extra ? "  ← " + extra : ""));
  }
}

function eq(got, want, name) {
  ok(got === want, name, "得到 " + JSON.stringify(got) +
    "，期望 " + JSON.stringify(want));
}

function has(hay, needle, name) {
  ok(String(hay).indexOf(needle) >= 0, name,
    "「" + needle + "」不在 " + JSON.stringify(String(hay).slice(0, 200)));
}

// ---------------------------------------------------------------- 起环境
function boot(opts) {
  opts = opts || {};
  const dom = new jsdom.JSDOM(
    "<!doctype html><html><body><main id=\"host\"></main></body></html>",
    { runScripts: "outside-only", pretendToBeVisual: true, url: "http://127.0.0.1:8899/" });
  const win = dom.window;
  const doc = win.document;

  const calls = [];
  const toasts = [];
  let view = null;

  // 和主程序一样的极简总线：article:open 就靠它
  const listeners = {};
  const bus = {
    on: function (ev, fn) {
      (listeners[ev] = listeners[ev] || []).push(fn);
      return function () { bus.off(ev, fn); };
    },
    off: function (ev, fn) {
      const l = listeners[ev] || [];
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    emit: function (ev, payload) {
      (listeners[ev] || []).slice().forEach(function (fn) {
        try { fn(payload); } catch (e) { /* 订阅者自己炸了不该拖垮别人 */ }
      });
    },
  };

  const api = {
    get: (url) => {
      calls.push({ kind: "get", url });
      if (opts.getFails) return Promise.reject(new Error("网络断了"));
      if (/\/item\//.test(url)) {
        const id = decodeURIComponent(url.split("/item/")[1].split("?")[0]);
        if (opts.item) return Promise.resolve(opts.item(id));
        return Promise.resolve({ ok: false });
      }
      const m = /[?&]kind=([^&]*)/.exec(url);
      const qm = /[?&]q=([^&]*)/.exec(url);
      const kind = m ? decodeURIComponent(m[1]) : "dl";
      const q = qm ? decodeURIComponent(qm[1]) : "";
      return Promise.resolve(opts.payload ? opts.payload(kind, q)
        : indexPayload(kind, q, opts.prefs));
    },
    send: (method, url, body) => {
      calls.push({ kind: "send", method, url, body });
      if (opts.sendFails) return Promise.reject(new Error("接口挂了"));
      if (opts.reply) return Promise.resolve(opts.reply(url, body));
      if (/\/apps$/.test(url)) {
        const cfg = indexPayload("dl", "", opts.prefs);
        if (body && body.prefs) cfg.cfg.prefs = body.prefs;
        return Promise.resolve({ ok: true, cfg: cfg.cfg });
      }
      if (/\/star$/.test(url)) return Promise.resolve({ ok: true, item: body });
      if (/\/prepare$/.test(url)) {
        // 模拟后端：认出类型 → 查优先响应 → 决定「直接执行」还是「弹层」
        const url0 = (body && body.url) || "";
        const kind = kindOf(url0);
        const pref = (opts.prefs || {})[kind] || "auto";
        const ask = pref === "ask" || !!(body && body.ask);
        return Promise.resolve({
          ok: true, url: url0, pid: kind === "pan" ? "baidu" : "magnet",
          kind, kindText: { bt: "磁力 / 种子", pan: "网盘", file: "直链" }[kind],
          name: kind === "pan" ? "百度网盘" : "磁力链接",
          badge: kind === "pan" ? "BD" : "MG",
          color: kind === "pan" ? "#306cff" : "#5aa9e6",
          title: (body && body.title) || "",
          pref, default: ask ? "auto" : pref, ask,
          targets: TARGETS.filter((t) => t.kinds.indexOf(kind) >= 0),
          canWake: true, reason: "",
        });
      }
      return Promise.resolve({ ok: true, action: "open", detail: "已唤醒",
                               url: body && body.url });
    },
  };

  // 和主程序一样的能力登记表：插件之间就靠这个说话
  const caps = {};
  win.ARR = {
    registerView: function (v) { view = v; },
    on: bus.on, off: bus.off, emit: bus.emit,
    toast: (m) => toasts.push(String(m)),
    openItem: (id) => calls.push({ kind: "openItem", id }),
    api: api,
    provide: (name, fn) => { caps[name] = fn; return true; },
    can: (name) => typeof caps[name] === "function",
    invoke: (name, p) => {
      if (typeof caps[name] !== "function") return false;
      caps[name](p);
      return true;
    },
    openView: (id) => { calls.push({ kind: "openView", id }); return true; },
  };
  win.navigator.clipboard = { writeText: (t) => Promise.resolve(t) };

  const ctx = {
    esc: (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;"),
    toast: (m) => toasts.push(String(m)),
    openItem: (id) => calls.push({ kind: "openItem", id }),
    on: bus.on, off: bus.off, emit: bus.emit,
    api: api,
  };

  win.eval(fs.readFileSync(VIEW, "utf8"));
  const host = doc.getElementById("host");
  view.mount(host, ctx);

  return { dom, win, doc, host, view, ctx, calls, toasts, bus, caps };
}

// 等一轮微任务 + 一次宏任务，让 load() 的 Promise 落定
function tick(ms) {
  return new Promise((r) => setTimeout(r, ms || 0));
}

function click(win, node) {
  node.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true }));
}

function fire(win, node, type) {
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}

function q(scope, sel) { return scope.querySelector(sel); }
function qa(scope, sel) { return Array.prototype.slice.call(scope.querySelectorAll(sel)); }

// ---------------------------------------------------------------- 用例
async function main() {
  // 1. 注册
  {
    const env = boot();
    await tick(10);
    ok(env.view && env.view.id === "linkwake", "registerView 拿到了视图");
    eq(env.view.title, "链接唤醒", "侧栏标题");
    eq(typeof env.view.mount, "function", "有 mount");
    eq(typeof env.view.unmount, "function", "有 unmount");
  }

  // 2. 首次读取 + 一行一条目
  {
    const env = boot();
    await tick(10);
    const gets = env.calls.filter((c) => c.kind === "get");
    eq(gets.length, 1, "挂载后只读一次");
    has(gets[0].url, "kind=dl", "默认按「可下载」读");

    eq(qa(env.host, ".lw-row").length, 2, "渲染出 2 行");
    has(q(env.host, '[data-el="sub"]').textContent, "2 个条目", "统计条目数");
    has(q(env.host, '[data-el="sub"]').textContent, "5 条链接", "统计链接数");
    has(q(env.host, '[data-el="sub"]').textContent, "磁力 4", "统计磁力数");
    has(q(env.host, '[data-el="sub"]').textContent, "网盘 1", "统计网盘数");
    has(q(env.host, ".lw-row-title").textContent, "某番 01", "行标题");

    // 默认收起：一屏要能扫过十几条，链接不铺开
    eq(q(env.host, ".lw-detail").hidden, true, "默认收起明细");
    // 行上仍要知道这条有几个来源，徽章不能省
    const tags = qa(env.host, ".lw-tag");
    eq(tags.length, 3, "两个条目共 3 个来源徽章");
    has(tags.map((t) => t.textContent).join("|"), "BD", "网盘徽章在行上");
    has(tags.map((t) => t.textContent).join("|"), "3", "多条链接的徽章带数量");

    // 展开后才看得到链接与提取码
    click(env.win, q(env.host, ".lw-row-main"));
    await tick(10);
    eq(q(env.host, ".lw-detail").hidden, false, "点行展开");
    has(q(env.host, ".lw-pwd").textContent, "k9x2", "显示补到的提取码");
    eq(qa(q(env.host, ".lw-row"), ".lw-url").length, 2, "这一条有两行链接");
  }

  // 3. 筛选条：只有当前档带数字
  {
    const env = boot();
    await tick(10);
    const chips = qa(env.host, ".lw-chip");
    eq(chips.length, 6, "六个筛选档");
    const on = chips.filter((c) => c.className.indexOf("lw-chip-on") >= 0);
    eq(on.length, 1, "只有一个档是选中态");
    eq(on[0].getAttribute("data-kind"), "dl", "默认选中「可下载」");
    // 这一档是 4 磁力 + 1 网盘 = 5 条
    eq(q(on[0], "i").textContent, "5", "当前档的数字是这一档的实际条数");
    const offBt = chips.filter((c) => c.getAttribute("data-kind") === "bt")[0];
    eq(q(offBt, "i"), null, "没选中的档不带数字（不拿这一档冒充那一档）");
  }

  // 4. 切档会带新的 kind 去读
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, '[data-act="kind"][data-kind="bt"]'));
    await tick(10);
    const gets = env.calls.filter((c) => c.kind === "get");
    eq(gets.length, 2, "切档又读了一次");
    has(gets[1].url, "kind=bt", "带上新的 kind");
    const on = qa(env.host, ".lw-chip-on");
    eq(on.length, 1, "还是只有一个选中");
    eq(on[0].getAttribute("data-kind"), "bt", "选中态跟上了");
  }

  // 5. 搜索防抖
  {
    const env = boot();
    await tick(10);
    const input = q(env.host, '[data-el="q"]');
    input.value = " 某番 02 ";
    fire(env.win, input, "input");
    const before = env.calls.filter((c) => c.kind === "get").length;
    eq(before, 1, "刚敲字不发请求");
    await tick(320);
    const gets = env.calls.filter((c) => c.kind === "get");
    eq(gets.length, 2, "防抖到点才发一次");
    has(gets[1].url, "q=" + encodeURIComponent("某番 02"), "查询词去了空白");
  }

  // 6. 勾选 -> 批量条（勾的是条目，不是一个链接一个框）
  {
    const env = boot();
    await tick(10);
    const bar = q(env.host, '[data-el="selbar"]');
    eq(bar.hidden, true, "一开始没有批量条");
    const cbs = qa(env.host, 'input[type="checkbox"]');
    eq(cbs.length, 2, "两个条目两个框，不再是一条链接一个框");
    cbs[0].checked = true;
    fire(env.win, cbs[0], "change");
    eq(bar.hidden, false, "勾上后批量条出来");
    has(bar.textContent, "已选 1 个条目", "显示已选条目数");
    has(bar.textContent, "2 条链接", "同时报会提交多少条链接");

    // 再勾一个
    cbs[1].checked = true;
    fire(env.win, cbs[1], "change");
    has(bar.textContent, "已选 2 个条目", "数量累加");
    has(bar.textContent, "5 条链接", "链接数跟着累加");

    // 取消
    cbs[1].checked = false;
    fire(env.win, cbs[1], "change");
    has(bar.textContent, "已选 1 个条目", "取消勾选会减掉");
  }

  // 7. 批量复制：不发接口（复制在前端做），走剪贴板
  {
    const env = boot();
    await tick(10);
    const cbs = qa(env.host, 'input[type="checkbox"]');
    cbs.forEach((c) => { c.checked = true; fire(env.win, c, "change"); });
    const sends = env.calls.filter((c) => c.kind === "send").length;
    click(env.win, q(env.host, '[data-act="batch-copy"]'));
    await tick(10);
    eq(env.calls.filter((c) => c.kind === "send").length, sends,
      "批量复制不该打接口");
    has(env.toasts.join("|"), "已复制 5 条", "复制有回音，还说清复制了几条");
  }

  // 8. 批量交给 qB：一个条目下的链接全部带上
  {
    const env = boot();
    await tick(10);
    const cbs = qa(env.host, 'input[type="checkbox"]');
    cbs.forEach((c) => { c.checked = true; fire(env.win, c, "change"); });
    click(env.win, q(env.host, '[data-act="batch-qb"]'));
    await tick(20);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    ok(!!send, "打了提交接口");
    has(send.url, "/batch", "打到 /batch");
    eq(send.body.action, "qb", "action 是 qb");
    eq(send.body.urls.length, 5, "两个条目下的 5 条链接全带上，不用逐条勾");
    has(env.toasts.join("|"), "已提交", "提交后有回音");
  }

  // 9. 单条唤醒：成功给 toast，失败照原话说
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, ".lw-row-main"));      // 先展开，操作在明细里
    await tick(10);
    click(env.win, q(env.host, '[data-act="wake"]'));
    await tick(20);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/wake", "打到 /wake");
    has(send.body.url, "magnet:", "带上链接");
    eq(send.body.pid, "magnet", "带上提供方");
    has(env.toasts.join("|"), "已唤醒", "成功有回音");
  }
  {
    const env = boot({
      reply: () => ({ ok: false, error: "连不上，端口没人监听（客户端启动了吗？）" }),
    });
    await tick(10);
    click(env.win, q(env.host, ".lw-row-main"));
    await tick(10);
    click(env.win, q(env.host, '[data-act="wake"]'));
    await tick(20);
    has(env.toasts.join("|"), "端口没人监听", "失败先说原因，不说「已唤醒」");
  }

  // 10. 接口抛错 -> 页面上说清楚，还能重试
  {
    const env = boot({ getFails: true });
    await tick(10);
    has(q(env.host, '[data-el="body"]').textContent, "读取失败", "读取失败要有话说");
    has(q(env.host, '[data-el="body"]').textContent, "网络断了", "带上原因");
    ok(!!q(env.host, '[data-act="reload"]'), "给一个重试按钮");
  }

  // 11. 空结果分档说真话（「网盘为空」要说成网盘少，不能让用户以为插件坏了）
  {
    const emptyPayload = (kind) => ({
      ok: true, entries: [],
      counts: { items: 0, links: 0, bt: 0, pan: 0, file: 0, web: 0 },
      scanned: 40, kind: kind, q: "", providers: PROVIDERS, modes: MODES,
      cfg: indexPayload().cfg, opt: {}, env: {},
    });
    const env = boot({ payload: emptyPayload });
    await tick(10);
    has(q(env.host, '[data-el="body"]').textContent, "没找到可下载的链接",
      "默认档为空时说清扫了多少");
    has(q(env.host, '[data-el="sub"]').textContent, "扫了 40 条", "报扫描量");

    click(env.win, q(env.host, '[data-act="kind"][data-kind="pan"]'));
    await tick(10);
    has(q(env.host, '[data-el="body"]').textContent, "没有网盘链接",
      "网盘档为空时说成「网盘本来就少」");
    has(q(env.host, '[data-el="body"]').textContent, "深度搜索",
      "并给出下一步该去哪找");
  }

  // 12. 来源再多也还是一行，明细里全都有
  {
    const mkGroup = (pid, name, badge, color, url) => ({
      pid, name, badge, kind: pid === "baidu" ? "pan" : "bt",
      color, note: "", urls: [url], count: 1,
    });
    const many = Object.assign({}, ENTRIES[1], {
      id: "i9",
      groups: [
        mkGroup("magnet", "磁力链接", "MG", "#5aa9e6", "magnet:?xt=urn:btih:a"),
        mkGroup("ed2k", "电驴链接", "ED", "#8e7cc3", "ed2k://|file|a.mkv|1|H|/"),
        mkGroup("baidu", "百度网盘", "BD", "#306cff", "https://pan.baidu.com/s/1x"),
        mkGroup("aliyun", "阿里云盘", "AL", "#ff6a00", "https://www.aliyundrive.com/s/1"),
        mkGroup("quark", "夸克网盘", "QK", "#5b6cff", "https://pan.quark.cn/s/1"),
        mkGroup("xunlei", "迅雷", "XL", "#0d84ff", "thunder://QUFodHRwOi8v"),
        mkGroup("uc", "UC 网盘", "UC", "#2b6bff", "https://drive.uc.cn/s/1"),
      ],
    });
    const env = boot({ payload: (kind, qq) => Object.assign(indexPayload(kind, qq), {
      entries: [many], counts: { items: 1, links: 7, bt: 5, pan: 2, file: 0, web: 0 },
    }) });
    await tick(10);
    eq(qa(env.host, ".lw-row").length, 1, "只有一行");
    // 7 个来源压成 7 个小徽章，行高不跟着涨
    eq(qa(env.host, ".lw-tag").length, 7, "7 个来源都在行上");
    eq(q(env.host, ".lw-detail").hidden, true, "默认收起");
    click(env.win, q(env.host, ".lw-row-main"));
    await tick(10);
    eq(qa(env.host, ".lw-group").length, 7, "展开后 7 组全在，不再藏一半");
    click(env.win, q(env.host, ".lw-row-main"));
    await tick(10);
    eq(q(env.host, ".lw-detail").hidden, true, "再点一次收起");
  }

  // 13. 设置抽屉
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, '[data-act="drawer"]'));
    await tick(10);
    const d = q(env.host, '[data-el="drawer"]');
    eq(d.hidden, false, "抽屉打开了");
    const rows = qa(d, "tr[data-pid]");
    eq(rows.length, PROVIDERS.length, "每个提供方一行");
    eq(q(rows[0], ".lw-mode").value, "open", "回填了磁力的方式");
    eq(q(rows[2], ".lw-mode").value, "web", "回填了百度的方式");
    eq(q(rows[2], ".lw-exe").disabled, true, "非「指定程序」时路径框禁用");
    eq(q(rows[2], ".lw-scheme").disabled, true, "非「客户端协议」时协议名禁用");

    // 切成「客户端协议」，协议名框要能填
    const m2 = q(rows[2], ".lw-mode");
    m2.value = "scheme";
    fire(env.win, m2, "change");
    eq(q(rows[2], ".lw-scheme").disabled, false, "选「客户端协议」后协议名放开");
    eq(q(rows[2], ".lw-exe").disabled, true, "此时路径框仍然禁用");

    // 切成「指定程序」，路径框要能填
    const sel = q(rows[0], ".lw-mode");
    sel.value = "app";
    fire(env.win, sel, "change");
    eq(q(rows[0], ".lw-exe").disabled, false, "选「指定程序」后路径框放开");
    q(rows[0], ".lw-exe").value = "C:\\Tools\\qbittorrent.exe";

    click(env.win, q(d, '[data-act="save-cfg"]'));
    await tick(20);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/apps", "保存打到 /apps");
    eq(send.body.apps.magnet.mode, "app", "把选中的方式带上");
    eq(send.body.apps.magnet.exe, "C:\\Tools\\qbittorrent.exe", "把路径带上");
    has(env.toasts.join("|"), "已保存", "保存有回音");
  }

  // 14. 密码留空不下发：抽屉里密码框初始就是空的
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, '[data-act="drawer"]'));
    await tick(10);
    const pw = q(env.host, '[data-sec="qb"][data-k="password"]');
    eq(pw.value, "", "密码框不回显");
    eq(pw.getAttribute("placeholder"), "没设就留空", "没设时说清楚");
  }

  // 15. 关闭抽屉
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, '[data-act="drawer"]'));
    await tick(10);
    const d = q(env.host, '[data-el="drawer"]');
    click(env.win, q(d, ".lw-drawer-mask"));
    eq(d.hidden, true, "点遮罩关掉");
  }

  // 16. 卸载后不留状态
  {
    const env = boot();
    await tick(10);
    const cb = q(env.host, 'input[type="checkbox"]');
    cb.checked = true;
    fire(env.win, cb, "change");
    env.view.unmount();
    env.view.mount(env.host, env.ctx);
    await tick(10);
    eq(q(env.host, '[data-el="selbar"]').hidden, true, "重新挂载后勾选不残留");
  }

  // 17. 点标题跳回主界面阅读区
  {
    const env = boot();
    await tick(10);
    click(env.win, q(env.host, ".lw-row-title"));
    await tick(10);
    const op = env.calls.filter((c) => c.kind === "openItem").pop();
    ok(!!op, "点标题要给主程序发跳转");
    eq(op && op.id, "i1", "跳到这一条");
    eq(q(env.host, ".lw-detail").hidden, true, "点标题不该顺手把行展开");
  }

  // 18. 收藏走主程序自己的接口，回主界面是同一份状态
  {
    const env = boot();
    await tick(10);
    const stars = qa(env.host, ".lw-star");
    eq(stars.length, 2, "每条一颗星");
    // 第一条夹具本来就是收藏状态，用第二条测「收藏」
    click(env.win, stars[1]);
    await tick(20);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/api/star", "收藏打到主程序的 /api/star");
    eq(send.body.id, "i2", "带上条目 id");
    eq(send.body.starred, true, "第一次是收藏");
    ok(stars[1].classList.contains("on"), "星点亮起来");
    has(env.toasts.join("|"), "已收藏", "有回音");

    click(env.win, stars[1]);
    await tick(20);
    eq(env.calls.filter((c) => c.kind === "send").pop().body.starred, false,
      "再点一次是取消收藏");
  }

  // 19. 阅读区联动：article:open 补网盘按钮与角标
  {
    const env = boot({
      item: () => ({ ok: true, entry: { id: "i1", groups: [
        { pid: "baidu", name: "百度网盘", badge: "BD", kind: "pan",
          color: "#306cff", pwd: "k9x2",
          urls: ["https://pan.baidu.com/s/1abc?pwd=k9x2"], count: 1 },
        { pid: "magnet", name: "磁力链接", badge: "MG", kind: "bt",
          color: "#5aa9e6", pwd: "",
          urls: ["magnet:?xt=urn:btih:aaa"], count: 1 },
      ] } }),
    });
    await tick(10);
    // 造一个和主程序一样的阅读区
    env.doc.body.insertAdjacentHTML("beforeend",
      '<div id="artActions"><button class="line-btn">帖子页</button></div>' +
      '<div id="artLinks"><a class="link-item" href="https://pan.baidu.com/s/1abc?pwd=k9x2">' +
      '<span>pan.baidu.com/s/1abc</span></a></div>');
    env.bus.emit("article:open", { id: "i1" });
    await tick(20);

    const acts = env.doc.getElementById("artActions");
    const btn = acts.querySelector("[data-lw-wake]");
    ok(!!btn, "网盘组补了一个按钮");
    has(btn.textContent, "百度网盘", "按钮说清是哪家");
    eq(acts.querySelector("button").textContent, "帖子页", "原有按钮不动");

    const a = env.doc.querySelector("#artLinks a");
    const pin = a.querySelector(".lw-pin");
    ok(!!pin, "正文链接前加了提供商角标");
    eq(pin && pin.textContent, "BD", "角标是提供商标记");
    has(a.title, "提取码 k9x2", "悬浮能看到提取码");

    click(env.win, btn);
    await tick(30);
    const prep = env.calls.filter((c) => c.kind === "send" &&
      /\/prepare$/.test(c.url)).pop();
    ok(!!prep, "先问一句「这链接是谁的」");
    eq(prep && prep.body.pid, "baidu", "带上提供方");
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/wake", "阅读区的按钮最终打到 /wake");
    eq(send.body.target, "auto", "默认按「自动」执行");
    eq(send.body.pid, "baidu", "唤醒时仍带上提供方");
  }

  // 20. 没链接的条目：不要留一个空按钮组
  {
    const env = boot({ item: () => ({ ok: false }) });
    await tick(10);
    env.doc.body.insertAdjacentHTML("beforeend", '<div id="artActions"></div>');
    env.bus.emit("article:open", { id: "i9" });
    await tick(20);
    eq(env.doc.getElementById("artActions").children.length, 0,
      "没链接就什么都不加，别塞空壳");
  }

  // 21. 优先响应：抽屉里四种类型各一行，选项按类型过滤
  {
    const env = boot();
    await tick(20);
    click(env.win, q(env.host, '[data-act="drawer"]'));
    await tick(10);
    const sels = qa(env.host, "[data-pref]");
    eq(sels.length, 4, "磁力 / 网盘 / 直链 / 网页 四行");
    const kinds = sels.map((s) => s.getAttribute("data-pref"));
    eq(kinds.join(","), "bt,pan,file,web", "顺序固定");
    const panVals = qa(env.host, '[data-pref="pan"] option').map((o) => o.value);
    ok(panVals.indexOf("qb") < 0, "网盘那行不该出现 qB");
    ok(panVals.indexOf("ask") >= 0, "每种都能选「每次都问我」");
    const btVals = qa(env.host, '[data-pref="bt"] option').map((o) => o.value);
    ok(btVals.indexOf("qb") >= 0, "磁力那行能选 qB");
    has(q(env.host, '[data-pref="pan"] option[value="ask"]').textContent,
      "问我", "选项文案是人话");
  }

  // 22. 只给 URL：别的插件通过 ARR.invoke 递进来，直接执行
  {
    const env = boot();
    await tick(20);
    ok(env.caps["link.wake"], "对外提供了 link.wake 能力");
    env.caps["link.wake"]({ url: "magnet:?xt=urn:btih:zzz", title: "某番" });
    await tick(30);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/wake", "认出来之后就去唤醒");
    eq(send.body.target, "auto", "没人指定就按「自动」");
    eq(env.doc.querySelector(".lw-sheet"), null, "默认不弹层");
  }

  // 23. 设成「每次都问我」：点一下先弹层，选谁就是谁
  {
    const env = boot({ prefs: { bt: "ask" } });
    await tick(20);
    env.caps["link.wake"]({ url: "magnet:?xt=urn:btih:zzz", title: "某番 01" });
    await tick(30);
    const sheet = env.doc.querySelector(".lw-sheet");
    ok(!!sheet, "弹层出现了");
    has(sheet.textContent, "用什么打开", "标题说清在问什么");
    has(sheet.textContent, "qBittorrent", "列出下载器");
    has(sheet.textContent, "只复制链接", "也留着「只复制」这条路");

    const opts = qa(env.doc, ".lw-opt").map((b) => b.getAttribute("data-tid"));
    ok(opts.indexOf("ask") < 0, "弹层里不该再问「要不要问」");

    click(env.win, q(env.doc, '.lw-opt[data-tid="qb"]'));
    await tick(30);
    const send = env.calls.filter((c) => c.kind === "send").pop();
    has(send.url, "/wake", "选完就去唤醒");
    eq(send.body.target, "qb", "用的是点的那一项");
    eq(env.doc.querySelector(".lw-sheet"), null, "选完就收起");
  }

  // 24. 弹层里勾「以后都用它」：写回优先响应
  {
    const env = boot({ prefs: { bt: "ask" } });
    await tick(20);
    env.caps["link.wake"]({ url: "magnet:?xt=urn:btih:zzz" });
    await tick(30);
    const keep = q(env.doc, '[data-el="sheet-keep"]');
    ok(!!keep, "有「不再问」的勾选框");
    keep.checked = true;
    click(env.win, q(env.doc, '.lw-opt[data-tid="aria2"]'));
    await tick(30);
    const save = env.calls.filter((c) => c.kind === "send" &&
      /\/apps$/.test(c.url)).pop();
    ok(!!save, "记住要写回配置");
    eq(save && save.body.prefs.bt, "aria2", "把磁力改成 aria2");
    eq(save && save.body.prefs.pan, "auto", "别的类型不动");
  }

  // 25. 弹层能关掉，别把界面锁死
  {
    const env = boot({ prefs: { bt: "ask" } });
    await tick(20);
    env.caps["link.wake"]({ url: "magnet:?xt=urn:btih:zzz" });
    await tick(30);
    env.doc.dispatchEvent(new env.win.KeyboardEvent("keydown", { key: "Escape" }));
    await tick(10);
    eq(env.doc.querySelector(".lw-sheet"), null, "按 Esc 能收起来");
    eq(env.calls.filter((c) => c.kind === "send" && /\/wake$/.test(c.url)).length,
      0, "没选就不该偷偷唤醒");

    env.caps["link.wake"]({ url: "magnet:?xt=urn:btih:zzz" });
    await tick(30);
    click(env.win, q(env.doc, ".lw-sheet-mask"));
    await tick(10);
    eq(env.doc.querySelector(".lw-sheet"), null, "点遮罩也能收起来");
  }

  // 26. 页面里的「▾」：这次换个方式，不改默认
  {
    const env = boot();
    await tick(20);
    const rows = qa(env.host, ".lw-row");
    click(env.win, rows[0].querySelector(".lw-row-main"));
    await tick(10);
    const pick = rows[0].querySelector('[data-act="choose"]');
    ok(!!pick, "每行链接旁有「换个方式」");
    click(env.win, pick);
    await tick(30);
    ok(!!env.doc.querySelector(".lw-sheet"), "点它就是弹层");
    const save = env.calls.filter((c) => c.kind === "send" && /\/apps$/.test(c.url));
    eq(save.length, 0, "只这一次，不动配置");
  }

  // 27. 「唤醒」按钮在 ask 模式下也走弹层（设置不能被抓着绕过）
  {
    const env = boot({ prefs: { bt: "ask" } });
    await tick(20);
    const rows = qa(env.host, ".lw-row");
    click(env.win, rows[0].querySelector(".lw-row-main"));
    await tick(10);
    click(env.win, rows[0].querySelector('[data-act="wake"]'));
    await tick(30);
    ok(!!env.doc.querySelector(".lw-sheet"), "ask 模式下先问再动");
  }
}

main().then(() => {
  const total = pass + fails.length;
  if (fails.length) {
    console.log("失败 " + fails.length + "/" + total + "：");
    fails.forEach((f) => console.log("  ✗ " + f));
    process.exit(1);
  }
  console.log("check_view: " + pass + " 项全过");
}).catch((e) => {
  // 崩了也要把已跑出的失败交代清楚，否则只知道「崩了」不知道前面哪条不对
  if (fails.length) {
    console.log("崩之前已有 " + fails.length + " 条失败：");
    fails.forEach((f) => console.log("  ✗ " + f));
  }
  console.log("跑崩了：" + (e && e.stack || e));
  process.exit(1);
});
