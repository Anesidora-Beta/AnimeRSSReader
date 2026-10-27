/* arrkit 示例前端：一个命名空间 + 视图注册，没有构建步骤。 */
(function () {
  "use strict";

  var KIT = window.KIT = {
    version: "0.1",
    views: {},
    state: { id: "", data: null },

    registerView: function (view) {
      if (!view || !view.id || typeof view.mount !== "function") return;
      this.views[view.id] = view;
      if (this.state.id === view.id) this.render();
    },

    api: function (path, opt) {
      opt = opt || {};
      var init = { method: opt.method || "GET", headers: {} };
      if (opt.body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opt.body);
      }
      return fetch(path, init).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
          return data;
        });
      });
    },

    esc: function (s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    },

    toast: function (msg) {
      var el = document.createElement("div");
      el.className = "toast";
      el.textContent = msg;
      document.body.appendChild(el);
      setTimeout(function () { el.remove(); }, 2600);
    }
  };

  function $(sel) { return document.querySelector(sel); }

  function navItem(view) {
    var btn = document.createElement("button");
    btn.className = "nav";
    btn.dataset.id = view.id;
    btn.innerHTML = '<span class="ico">' + KIT.esc(view.icon || "•") +
      "</span>" + KIT.esc(view.title || view.id);
    btn.addEventListener("click", function () { go(view.id); });
    return btn;
  }

  function go(id) {
    KIT.state.id = id;
    document.querySelectorAll(".nav").forEach(function (el) {
      el.classList.toggle("active", el.dataset.id === id);
    });
    var view = KIT.views[id];
    $("#title").textContent = view ? (view.title || id) : id;
    KIT.render();
    if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
  }

  KIT.render = function () {
    var view = KIT.views[KIT.state.id];
    var box = $("#view");
    if (!view) {
      box.innerHTML = '<p class="empty">这个视图没有注册脚本。</p>';
      return;
    }
    box.innerHTML = "";
    try {
      view.mount(box, {
        api: KIT.api, esc: KIT.esc, toast: KIT.toast,
        data: KIT.state.data, state: KIT.state
      });
    } catch (e) {
      box.innerHTML = '<p class="empty">渲染失败：' + KIT.esc(e.message) + "</p>";
    }
  };

  function loadScript(src) {
    return new Promise(function (resolve) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = resolve;          // 脚本坏了只少一个视图，不阻塞整页
      document.head.appendChild(s);
    });
  }

  function loadStyle(href) {
    if (!href) return;
    var l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    document.head.appendChild(l);
  }

  fetch("/_kit/bootstrap").then(function (r) { return r.json(); }).then(function (boot) {
    KIT.state.data = boot;
    $("#meta").textContent = boot.app + " v" + boot.version +
      " · " + (boot.modules || []).length + " 个模块";
    var nav = $("#nav");
    var views = boot.views || [];
    views.forEach(function (v) { nav.appendChild(navItem(v)); });

    var chain = Promise.resolve();
    views.forEach(function (v) {
      chain = chain.then(function () {
        loadStyle(v.style);
        if (!v.script) return;
        return loadScript(v.script);
      });
    });

    return chain.then(function () {
      var want = (location.hash || "").replace("#", "");
      go(want && views.some(function (v) { return v.id === want; })
        ? want : (views[0] && views[0].id) || "");
    });
  }).catch(function (e) {
    $("#title").textContent = "加载失败";
    $("#view").innerHTML = '<p class="empty">' + KIT.esc(e.message) + "</p>";
  });
})();
