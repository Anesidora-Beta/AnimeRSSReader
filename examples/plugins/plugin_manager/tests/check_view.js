// 用 jsdom 真的把 preview.html 跑起来，检查渲染与交互（不联网、不开浏览器）
const path = require("path");
const { JSDOM } = require("jsdom");

const file = path.resolve(process.argv[2]);
const errors = [];

JSDOM.fromFile(file, {
  runScripts: "dangerously",
  resources: "usable",
  pretendToBeVisual: true,
  beforeParse(win) {
    win.addEventListener("error", e => errors.push("window.error: " + e.message));
    win.console.error = (...a) => errors.push("console.error: " + a.join(" "));
    win.console.warn = (...a) => errors.push("console.warn: " + a.join(" "));
  }
}).then(dom => new Promise(r => setTimeout(() => r(dom), 700))).then(async dom => {
  const { window } = dom;
  const doc = window.document;
  const $ = s => doc.querySelector(s);
  const $$ = s => Array.from(doc.querySelectorAll(s));
  const out = [];
  const ok = (name, cond, extra) => out.push((cond ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : ""));

  ok("视图已注册", !!window.__SPEC && window.__SPEC.id === "pluginmgr",
     window.__SPEC ? window.__SPEC.id : "none");
  ok("根节点存在", !!$(".pm-wrap"));
  ok("插件目录已显示", ($("#pmRoot") || {}).textContent === "F:\\AnimeRSSReader\\plugins",
     ($("#pmRoot") || {}).textContent);
  ok("统计条 5 项", $$(".pm-stat").length === 5, $$(".pm-stat").length + " 项");
  ok("筛选 chip 5 个", $$(".pm-chip").length === 5, $$(".pm-chip").length + " 个");

  const cards = $$(".pm-card");
  ok("卡片 9 张（6 插件 + 3 内置）", cards.length === 9, cards.length + " 张");

  const states = cards.map(c => (c.className.match(/pm-st-(\w+)/) || [])[1]);
  ok("状态类齐全", ["normal", "broken", "disabled", "removed", "core"].every(s => states.includes(s)),
     states.join(","));

  ok("每张卡都有状态胶囊", $$(".pm-pill").length === 9, $$(".pm-pill").length + " 个");
  ok("坏插件显示错误色提示", !!$(".pm-st-broken .pm-hint.err"));
  ok("本插件有标记", $$(".pm-tag").some(t => t.textContent === "本插件"));

  // 内置模块不给停用按钮
  const coreBtns = $$(".pm-st-core .pm-btns .pm-btn").map(b => b.textContent.trim());
  ok("内置模块只有「详情」", coreBtns.every(t => t === "详情"), coreBtns.join(","));

  // 点「详情」应展开并异步拉详情
  const search = $('.pm-card[data-id="search"]');
  const toggle = search.querySelector('[data-act="toggle"]');
  toggle.click();
  await new Promise(r => setTimeout(r, 200));
  ok("详情已展开", !!$('.pm-card[data-id="search"] .pm-detail'));
  ok("详情有路由列表", $$('.pm-card[data-id="search"] .pm-route').length >= 10,
     $$('.pm-card[data-id="search"] .pm-route').length + " 条");
  ok("详情有文件列表", $$('.pm-card[data-id="search"] .pm-file').length >= 10,
     $$('.pm-card[data-id="search"] .pm-file').length + " 个");
  ok("详情有备注输入", !!$('.pm-note[data-note="search"]'));

  // 点一个文件 -> 走假接口读内容
  const f = $('.pm-card[data-id="search"] .pm-file');
  f.click();
  await new Promise(r => setTimeout(r, 150));
  ok("源码面板出现", !!$('.pm-card[data-id="search"] .pm-src pre'),
     ($('.pm-card[data-id="search"] .pm-src pre') || {}).textContent ?
       "有内容" : "空");

  // 停用要二次确认
  const dis = $('.pm-card[data-id="hello"] [data-act="disable"]');
  ok("有停用按钮", !!dis);
  dis.click();
  await new Promise(r => setTimeout(r, 60));
  ok("弹出确认框", !$("#pmModal").hidden && !!$(".pm-modalbox"));
  ok("确认框写清了会移进 .disabled",
     ($("#pmModal").textContent || "").indexOf(".disabled") > 0);
  $("#pmModal").querySelector('[data-modal="no"]').click();
  await new Promise(r => setTimeout(r, 60));
  ok("取消后弹窗关闭", $("#pmModal").hidden === true);

  // 体检
  $('[data-act="doctor"]').click();
  await new Promise(r => setTimeout(r, 80));
  ok("体检结果展开", !$("#pmIssues").hidden &&
     $$(".pm-issuerow").length === 3, $$(".pm-issuerow").length + " 条");

  // 回收站
  ok("回收站有 2 条", $$(".pm-trashrow").length === 2, $$(".pm-trashrow").length + " 条");
  ok("回收站可恢复/可删除",
     $$(".pm-trashrow [data-act='restore']").length === 1 &&
     $$(".pm-trashrow [data-act='purge']").length === 2);

  // 搜索过滤
  const searchBox = $("#pmSearch");
  searchBox.value = "示例";
  searchBox.dispatchEvent(new window.Event("input"));
  await new Promise(r => setTimeout(r, 60));
  ok("搜索过滤生效", $$(".pm-card").length === 1, $$(".pm-card").length + " 张");
  searchBox.value = "";
  searchBox.dispatchEvent(new window.Event("input"));
  await new Promise(r => setTimeout(r, 60));

  // 筛选 chip
  $('.pm-chip[data-v="third"]').click();
  await new Promise(r => setTimeout(r, 60));
  ok("第三方筛选去掉了内置", $$(".pm-card").length === 6, $$(".pm-card").length + " 张");
  $('.pm-chip[data-v="all"]').click();
  await new Promise(r => setTimeout(r, 60));

  ok("无 JS 错误", errors.length === 0, errors.slice(0, 3).join(" | "));

  console.log(out.join("\n"));
  const fail = out.filter(l => l.startsWith("FAIL")).length;
  console.log("\n" + (fail ? "✗ " + fail + " 项失败" : "✓ 全部通过") + "（共 " + out.length + " 项）");
  dom.window.close();
  process.exit(fail ? 1 : 0);
}).catch(e => {
  console.error("跑不起来：", e);
  process.exit(2);
});
