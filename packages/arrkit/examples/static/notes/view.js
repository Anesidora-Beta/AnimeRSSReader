/* notes 视图：演示「模块自己管 UI、只跟自己的 /api/notes 打交道」。 */
(function () {
  window.KIT.registerView({
    id: "notes",
    title: "便签",
    mount: function (el, ctx) {
      el.innerHTML =
        '<div class="notes">' +
        '  <form class="notes-form" id="nt-form">' +
        '    <input id="nt-input" maxlength="80" placeholder="写点什么，回车添加">' +
        '    <button type="submit">添加</button>' +
        '  </form>' +
        '  <p class="notes-seen" id="nt-seen"></p>' +
        '  <ul class="notes-list" id="nt-list"></ul>' +
        "</div>";

      var list = el.querySelector("#nt-list");
      var seen = el.querySelector("#nt-seen");
      var input = el.querySelector("#nt-input");

      function paint(items, ticks) {
        seen.textContent = "clock 事件已收到 " + ticks + " 次";
        if (!items.length) {
          list.innerHTML = '<li class="notes-empty">还没有便签</li>';
          return;
        }
        list.innerHTML = items.map(function (it) {
          return '<li><span>' + ctx.esc(it.text) + "</span>" +
            '<em>' + ctx.esc(it.at) + "</em>" +
            '<button data-id="' + it.id + '" title="删除">×</button></li>';
        }).join("");
        list.querySelectorAll("button[data-id]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            ctx.api("/api/notes/" + btn.dataset.id, { method: "DELETE" })
              .then(load).catch(function (e) { ctx.toast(e.message); });
          });
        });
      }

      function load() {
        return ctx.api("/api/notes").then(function (d) {
          paint(d.items || [], d.seenTicks || 0);
        }).catch(function (e) {
          list.innerHTML = '<li class="notes-empty">读取失败：' +
            ctx.esc(e.message) + "</li>";
        });
      }

      el.querySelector("#nt-form").addEventListener("submit", function (ev) {
        ev.preventDefault();
        var text = input.value.trim();
        if (!text) return;
        ctx.api("/api/notes", { method: "POST", body: { text: text } })
          .then(function () { input.value = ""; return load(); })
          .catch(function (e) { ctx.toast(e.message); });
      });

      load();
    }
  });
})();
