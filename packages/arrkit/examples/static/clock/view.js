/* clock 视图：自带脚本与样式，内核不知道这里画什么。 */
(function () {
  var timer = null;

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  window.KIT.registerView({
    id: "clock",
    title: "时钟",
    mount: function (el, ctx) {
      el.innerHTML =
        '<div class="clock-card">' +
        '  <div class="clock-time" id="ck-time">--:--:--</div>' +
        '  <div class="clock-sub" id="ck-sub">等待第一秒…</div>' +
        '  <dl class="clock-stats">' +
        '    <div><dt>跳了</dt><dd id="ck-ticks">0</dd></div>' +
        '    <div><dt>运行</dt><dd id="ck-up">0s</dd></div>' +
        '    <div><dt>订阅数</dt><dd id="ck-subs">0</dd></div>' +
        '  </dl>' +
        '  <button class="clock-btn" id="ck-ping">发一条 clock.ping</button>' +
        "</div>";

      var showSeconds = true;
      try {
        var s = (ctx.data && ctx.data.settings && ctx.data.settings.values) || {};
        showSeconds = s["clock.showSeconds"] !== false;
      } catch (e) { /* 没配置就按默认 */ }

      function paint() {
        ctx.api("/api/clock").then(function (d) {
          var t = d.time;
          el.querySelector("#ck-time").textContent = showSeconds ? t : t.slice(0, 5);
          el.querySelector("#ck-ticks").textContent = d.ticks;
          el.querySelector("#ck-up").textContent = d.uptime + "s";
          el.querySelector("#ck-sub").textContent = "服务端时间 · 每秒自动刷新";
        }).catch(function (e) {
          el.querySelector("#ck-sub").textContent = "取时间失败：" + e.message;
        });
        ctx.api("/api/clock/stats").then(function (d) {
          el.querySelector("#ck-subs").textContent =
            (d.subscribers && d.subscribers.subscriptions) || 0;
        }).catch(function () {});
      }

      el.querySelector("#ck-ping").addEventListener("click", function () {
        // 事件是后端到后端；这里只是给个提示，证明按钮活着
        ctx.toast("clock.ping 是后端事件，见 /_kit/jobs 与启动日志");
      });

      paint();
      timer = setInterval(paint, 2000);
    },
    unmount: function () {
      if (timer) clearInterval(timer);
      timer = null;
    }
  });
})();
