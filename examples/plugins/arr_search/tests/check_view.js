/* 前端视图的断言，跑在 jsdom 里。
 *
 * 这个文件只盯三件「后端测不到、但用户一按就露馅」的事：
 *
 *   1. 「磁力」按钮到底干了什么 —— 装了链接唤醒就该递 URL 过去，没装就该
 *      老老实实复制。以前它只会复制，用户点了没反应，就是坏在这。
 *   2. 侧栏快捷搜索认领了没有、按回车有没有真的带着词跳进搜索页。
 *   3. 空结果 / 没链接的条目别渲染出一堆空壳按钮。
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
    process.env.AS_JSDOM,
    path.join(PLUGIN, "node_modules", "jsdom"),
    path.join(PLUGIN, "..", "..", ".jsdom", "node_modules", "jsdom"),
    path.join(PLUGIN, "..", "..", "..", ".jsdom", "node_modules", "jsdom"),
  ].filter(Boolean);
  for (const t of tries) {
    try { return require(t); } catch (e) { /* 换下一个 */ }
  }
  return null;
}

const jsdom = loadJsdom();
if (!jsdom) {
  console.log("跳过：没找到 jsdom。装一个再跑 ——\n" +
    "  npm i jsdom            # 全局或临时目录都行\n" +
    "  AS_JSDOM=/path/to/jsdom node tests/check_view.js");
  process.exit(0);
}

// ---------------------------------------------------------------- 夹具
const PROVIDERS = [
  { id: "local", name: "本地库", kind: "local", note: "主程序已经抓到的" },
  { id: "mikan", name: "Mikan", kind: "rss", note: "蜜柑计划" },
];

const ITEMS = [
  {
    id: "m1", title: "[喵萌奶茶屋] 某番 01 [1080p]",
    magnet: "magnet:?xt=urn:btih:aaa", torrent: "",
    link: "https://mikanani.me/Home/Episode/abc",
    source: "mikan", sourceName: "Mikan", size: "1.2GB", seeders: 42,
    ts: Math.floor(Date.now() / 1000) - 7200, pubdate: "2026-10-01",
    local: false, alsoIn: [],
  },
  {
    id: "m2", title: "没有磁力的条目",
    magnet: "", torrent: "https://example.com/a.torrent",
    link: "https://example.com/post/2",
    source: "mikan", sourceName: "Mikan", size: "", seeders: null,
    ts: 0, pubdate: "", local: true, alsoIn: ["local"],
  },
];

// ---------------------------------------------------------------- 断言器
let pass = 0;
const fails = [];

function ok(cond, name, extra) {
  if (cond) pass++;
  else fails.push(name + (extra ? "  ← " + extra : ""));
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
    '<!doctype html><html><body><main id="host"></main></body></html>',
    { runScripts: "outside-only", pretendToBeVisual: true,
      url: "http://127.0.0.1:8899/" });
  const win = dom.window;
  const doc = win.document;

  const calls = [];
  const toasts = [];
  const quick = [];
  const views = {};
  let view = null;

  const api = {
    get: (url) => {
      calls.push({ kind: "get", url });
      if (/\/providers$/.test(url)) {
        return Promise.resolve({
          ok: true, providers: PROVIDERS, tips: ["+必含 -排除"],
          defaults: ["local"], pages: 3,
        });
      }
      if (/\/presets$/.test(url)) return Promise.resolve({ ok: true, presets: [] });
      if (/\/history$/.test(url)) return Promise.resolve({ ok: true, items: [] });
      if (/^\/api\/search\?/.test(url)) {
        return Promise.resolve({
          ok: true, items: ITEMS, hasMore: false, took: 12, raw: 2,
          query: {}, sources: [],
        });
      }
      return Promise.resolve({ ok: true });
    },
    send: (method, url, body) => {
      calls.push({ kind: "send", method, url, body });
      return Promise.resolve({ ok: true });
    },
  };

  const caps = {};
  win.ARR = {
    registerView: (v) => { view = v; views[v.id] = v; },
    on: () => function () {}, off: () => {}, emit: () => {},
    toast: (m) => toasts.push(String(m)),
    openItem: () => {},
    api: api,
    provide: (n, fn) => { caps[n] = fn; return true; },
    can: (n) => typeof caps[n] === "function",
    invoke: (n, p) => {
      if (typeof caps[n] !== "function") return false;
      caps[n](p);
      return true;
    },
    quickSearch: (spec) => { quick.push(spec); return true; },
    openView: (id) => { calls.push({ kind: "openView", id }); return true; },
  };
  // 测试里可选的「链接唤醒」替身
  if (opts.withWake) {
    caps["link.wake"] = (p) => { calls.push({ kind: "wake", body: p }); };
  }
  win.navigator.clipboard = {
    writeText: (t) => { calls.push({ kind: "clipboard", text: t });
                        return Promise.resolve(t); },
  };

  win.eval(fs.readFileSync(VIEW, "utf8"));

  return {
    dom, win, doc, calls, toasts, quick, caps, views,
    get view() { return view; },
    mount() {
      const host = doc.getElementById("host");
      view.mount(host, {
        esc: (s) => String(s == null ? "" : s)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;"),
        toast: (m) => toasts.push(String(m)),
        api: api,
        on: () => function () {}, off: () => {}, emit: () => {},
      });
      return host;
    },
  };
}

function tick(ms) { return new Promise((r) => setTimeout(r, ms || 0)); }
function click(win, node) {
  node.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true }));
}
async function search(env, word) {
  const box = q(env.doc, "#sqQ");
  box.value = word;
  click(env.win, q(env.doc, "#sqGo"));
  await tick(30);
}

function q(scope, sel) { return scope.querySelector(sel); }
function qa(scope, sel) {
  return Array.prototype.slice.call(scope.querySelectorAll(sel));
}

// ---------------------------------------------------------------- 用例
async function main() {
  // 1. 注册
  {
    const env = boot();
    eq(env.view && env.view.id, "search", "registerView 拿到了视图");
    eq(env.view && env.view.title, "深度搜索", "标题");
  }

  // 2. 认领侧栏快捷搜索（脚本加载时就认领，不必先 mount）
  {
    const env = boot();
    eq(env.quick.length, 1, "认领了一次快捷搜索");
    const spec = env.quick[0];
    eq(spec.id, "search", "认领者 id");
    ok(typeof spec.submit === "function", "有 submit");
    has(spec.placeholder, "搜", "有提示词");
  }

  // 3. 从快捷搜索进来：必须带着词跳过去，并且真的发起检索
  {
    const env = boot();
    env.quick[0].submit("芙莉莲");
    await tick(10);
    const jump = env.calls.filter((c) => c.kind === "openView").pop();
    ok(!!jump, "要跳到搜索页");
    eq(jump && jump.id, "search", "跳到自己这一页");

    env.mount();
    await tick(30);
    eq(q(env.doc, "#sqQ").value, "芙莉莲", "输入框被填上了");
    const search = env.calls.filter((c) => c.kind === "get" &&
      /^\/api\/search\?/.test(c.url)).pop();
    ok(!!search, "自动发起了检索");
    has(search && search.url, "q=%E8%8A%99%E8%8E%89%E8%8E%B2", "词带进请求里");
  }

  // 4. 装了链接唤醒：按钮叫「下载」，点了把 URL 递过去
  {
    const env = boot({ withWake: true });
    env.mount();
    await tick(30);
    await search(env, "某番");
    const rows = qa(env.doc, ".sq-item");
    eq(rows.length, 2, "两条结果");
    const btn = q(rows[0], ".sq-mag");
    eq(btn.textContent, "下载", "装了唤醒就叫「下载」");
    ok(!!q(rows[0], ".sq-mag-pick"), "旁边有「换一个」的箭头");

    click(env.win, btn);
    await tick(20);
    const w = env.calls.filter((c) => c.kind === "wake").pop();
    ok(!!w, "点下去要递唤醒请求");
    eq(w && w.body.url, "magnet:?xt=urn:btih:aaa", "递的是磁力链");
    has(w && w.body.title, "某番 01", "带上标题，弹层里好认");
    eq(w && w.body.ask, false, "默认按设置走，不强制弹层");

    click(env.win, q(rows[0], ".sq-mag-pick"));
    await tick(20);
    const w2 = env.calls.filter((c) => c.kind === "wake").pop();
    eq(w2 && w2.body.ask, true, "点箭头就是「这次让我选」");
  }

  // 5. 只有种子链接的条目：走同一套（后端认得出 .torrent）
  {
    const env = boot({ withWake: true });
    env.mount();
    await tick(30);
    await search(env, "某番");
    const rows = qa(env.doc, ".sq-item");
    const btn = q(rows[1], ".sq-mag");
    ok(!!btn, "只有种子也给出按钮");
    click(env.win, btn);
    await tick(20);
    const w = env.calls.filter((c) => c.kind === "wake").pop();
    eq(w && w.body.url, "https://example.com/a.torrent", "递种子地址");
  }

  // 6. 没装链接唤醒：退回复制，跟以前一样，不能点了没反应
  {
    const env = boot();
    env.mount();
    await tick(30);
    await search(env, "某番");
    const rows = qa(env.doc, ".sq-item");
    eq(q(rows[0], ".sq-mag").textContent, "磁力", "没装唤醒就叫「磁力」");
    eq(q(rows[0], ".sq-mag-pick"), null, "没装就别给一个点不动的箭头");
    click(env.win, q(rows[0], ".sq-mag"));
    await tick(20);
    const clip = env.calls.filter((c) => c.kind === "clipboard").pop();
    ok(!!clip, "复制到剪贴板");
    eq(clip && clip.text, "magnet:?xt=urn:btih:aaa", "复制的是磁力链");
    has(env.toasts.join("|"), "已复制", "有回音");
    has(env.toasts.join("|"), "链接唤醒", "顺便告诉他能装个插件");
  }

  // 7. 空着搜：先要个关键词，别白跑一趟
  {
    const env = boot();
    env.mount();
    await tick(30);
    click(env.win, q(env.doc, "#sqGo"));
    await tick(20);
    const search = env.calls.filter((c) => c.kind === "get" &&
      /^\/api\/search\?/.test(c.url));
    eq(search.length, 0, "没给词就不发请求");
    has(env.toasts.join("|"), "关键词", "告诉用户缺什么");
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
  console.log("崩了：" + (e && e.stack ? e.stack : e));
  if (fails.length) {
    console.log("已跑到的失败：");
    fails.forEach((f) => console.log("  ✗ " + f));
  }
  process.exit(1);
});
