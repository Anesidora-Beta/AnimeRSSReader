/* 示例插件的前端视图。
 *
 * 这个文件由主程序按 bootstrap 里的清单自动加载，加载后自己调 ARR.registerView，
 * 侧栏就会多一个入口 —— 主程序的 HTML / JS 里都没有提到「hello」。
 *
 * 拿到的 ctx：
 *   ctx.api.get(url) / ctx.api.send(method, url, body)   与主程序同一套请求
 *   ctx.toast(msg) / ctx.esc(str)
 *   ctx.on(event, fn) / ctx.emit(event, payload)         前端事件总线
 *   ctx.openItem(id)                                     打开某条更新
 *   ctx.theme / ctx.boot / ctx.baseUrl
 */
(function () {
  "use strict";

  var timer = null;

  function card(el, ctx, data) {
    var box = el.querySelector(".hello-card");
    if (!box) return;
    if (!data || !data.ok) {
      box.innerHTML = '<p class="hello-err">接口暂时不可用</p>';
      return;
    }
    var job = data.job || {};
    box.innerHTML =
      '<p class="hello-greet">' + ctx.esc(data.text || "") + "</p>" +
      '<p class="hello-meta">模块 <b>' + ctx.esc(data.module || "") + "</b> v" +
      ctx.esc(data.version || "") + " · 每 15 秒自动刷新</p>" +
      '<dl class="hello-stats">' +
      "<div><dt>抓取完成次数</dt><dd>" + (data.refreshes || 0) + "</dd></div>" +
      "<div><dt>定时任务跑了</dt><dd>" + (data.ticks || 0) + " 次</dd></div>" +
      "<div><dt>任务下次在</dt><dd>" + (job.nextIn != null ? job.nextIn + " 秒后" : "—") + "</dd></div>" +
      "<div><dt>事件订阅数</dt><dd>" + ((data.bus && data.bus.subscriptions) || 0) + "</dd></div>" +
      "</dl>";
  }

  function pull(el, ctx) {
    ctx.api.get("/api/hello").then(function (hello) {
      ctx.api.get("/api/hello/stats").then(function (stats) {
        var merged = stats || {};
        merged.text = (hello && hello.text) || (stats && stats.text) || "";
        merged.module = (hello && hello.module) || "hello";
        merged.version = (hello && hello.version) || "";
        card(el, ctx, merged);
      });
    }).catch(function (e) {
      card(el, ctx, null);
      if (window.console) console.warn("[hello] 读取失败", e);
    });
  }

  ARR.registerView({
    id: "hello",
    title: "示例插件",
    icon: "◆",
    mount: function (el, ctx) {
      el.innerHTML =
        '<div class="hello-wrap">' +
        '  <h2>示例插件</h2>' +
        '  <p class="hello-sub">这段界面来自 <code>examples/plugins/static/view.js</code>，' +
        "主程序没有为它写过一行 HTML。</p>" +
        '  <div class="hello-card"><p class="hello-greet">正在读取…</p></div>' +
        '  <p class="hello-actions"><button class="primary-btn" id="helloAgain">再问一次</button></p>' +
        "</div>";

      el.querySelector("#helloAgain").addEventListener("click", function () {
        pull(el, ctx);
        ctx.toast("已重新读取");
      });

      pull(el, ctx);
      clearInterval(timer);
      timer = setInterval(function () { pull(el, ctx); }, 15000);
      ctx.on("hello.ping", function () { ctx.toast("前端事件总线可用"); });
    },
    unmount: function () {
      clearInterval(timer);
      timer = null;
    }
  });
})();
