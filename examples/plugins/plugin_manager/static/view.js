/* 插件管理 —— 插件自带的前端视图。
 *
 * 主程序的 HTML / JS 里没有为它写过一行代码：后端在 bootstrap 的 views 清单里
 * 声明这个视图，主程序按清单加载 /plug/pluginmgr/view.js，脚本自己调
 * ARR.registerView 注册，侧栏就多一个入口。
 *
 * 能用的 ctx（见 docs/ARCHITECTURE.md §4.4）：
 *   ctx.api.get(url) / ctx.api.send(method, url, body)
 *   ctx.toast(msg) / ctx.esc(str) / ctx.on/off/emit / ctx.openItem(id)
 *
 * 交互上只守一条：**危险按钮永远有第二次确认**，而且确认框里写清会发生什么
 * （「移进 .disabled，可以再启用」和「移进回收站」是两件事，不能都叫「确定」）。
 */
(function () {
  "use strict";

  var VIEW_ID = "pluginmgr";

  var S = {
    data: null,          // /api/pluginmgr 的整包
    filter: "all",       // all | on | off | bad | third
    q: "",
    open: {},            // id -> true（展开详情）
    detail: {},          // id -> detail 缓存
    file: {},            // id -> {path, text}
    busy: "",            // 正在操作的插件 id
    showIssues: false,
    loading: false,
    error: ""
  };

  var el = null;         // 视图根节点
  var C = null;          // ctx

  // ---------------------------------------------------------------- 小工具

  function $(sel, root) { return (root || el).querySelector(sel); }

  function esc(s) {
    if (C && typeof C.esc === "function") return C.esc(s == null ? "" : s);
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(msg) {
    if (C && typeof C.toast === "function") C.toast(msg);
  }

  function api(path) {
    return C.api.get(path);
  }

  function post(path, body) {
    return C.api.send("POST", path, body || {});
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : "" + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtAgo(ts) {
    if (!ts) return "";
    var diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.round(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.round(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.round(diff / 86400) + " 天前";
    return fmtTime(ts).slice(0, 10);
  }

  // ---------------------------------------------------------------- 骨架

  function skeleton() {
    return '' +
      '<div class="pm-wrap">' +
      '  <div class="pm-head">' +
      '    <div class="pm-title">' +
      '      <h2>插件管理</h2>' +
      '      <p class="pm-sub">看清楚装了哪些插件、它们现在什么状态；' +
      '停用、启用、重载、卸载和安装都在这一页完成。</p>' +
      '    </div>' +
      '    <div class="pm-acts">' +
      '      <button class="pm-btn" data-act="refresh" title="重新扫描插件目录">刷新</button>' +
      '      <button class="pm-btn" data-act="doctor" title="检查版本、依赖、目录名等问题">体检</button>' +
      '      <button class="pm-btn pm-primary" data-act="install">安装 .zip</button>' +
      '    </div>' +
      '  </div>' +

      '  <div class="pm-bar">' +
      '    <div class="pm-stats" id="pmStats"></div>' +
      '    <div class="pm-path">' +
      '      <span class="pm-pathlabel">插件目录</span>' +
      '      <code class="pm-pathv" id="pmRoot">—</code>' +
      '      <button class="pm-btn pm-mini" data-act="open" title="在文件管理器里打开">打开</button>' +
      '    </div>' +
      '  </div>' +

      '  <div class="pm-tools">' +
      '    <div class="pm-filters" id="pmFilters"></div>' +
      '    <input class="pm-search" id="pmSearch" type="search" ' +
      '           placeholder="搜索名称 / id / 目录…" autocomplete="off">' +
      '  </div>' +

      '  <div class="pm-issues" id="pmIssues" hidden></div>' +
      '  <div class="pm-list" id="pmList"></div>' +
      '  <div class="pm-trash" id="pmTrash" hidden></div>' +

      '  <input type="file" class="pm-file" id="pmFile" accept=".zip,application/zip" hidden>' +
      '  <div class="pm-drop" id="pmDrop" hidden><b>松手安装</b>' +
      '    <span>把 .zip 拖到这里，会解到 plugins/ 并立刻加载</span></div>' +
      '  <div class="pm-modal" id="pmModal" hidden></div>' +
      '</div>';
  }

  // ---------------------------------------------------------------- 拉数据

  function load(quiet) {
    if (S.loading) return;
    S.loading = true;
    if (!quiet) renderList();
    api("/api/pluginmgr").then(function (res) {
      S.loading = false;
      if (!res || !res.ok) {
        S.error = (res && res.error) || "读取插件列表失败";
        renderAll();
        return;
      }
      S.error = "";
      S.data = res;
      S.detail = {};                      // 状态变了，详情缓存作废
      renderAll();
    }).catch(function (e) {
      S.loading = false;
      S.error = "连不上本地服务：" + e.message;
      renderAll();
    });
  }

  function entryOf(id) {
    var list = (S.data && S.data.entries) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  // ---------------------------------------------------------------- 渲染

  function renderAll() {
    if (!el) return;
    renderStats();
    renderFilters();
    renderList();
    renderTrash();
    renderIssues();
  }

  function renderStats() {
    var box = $("#pmStats");
    var d = S.data;
    if (!box) return;
    if (!d) { box.innerHTML = ""; return; }
    var c = d.counts || {};
    var root = (d.env && d.env.root) || "—";
    var rootEl = $("#pmRoot");
    if (rootEl) { rootEl.textContent = root; rootEl.title = root; }

    var bits = [
      ["插件", c.total || 0, ""],
      ["运行中", c.on || 0, "ok"],
      ["已停用", c.off || 0, "mute"],
      ["待处理", c.bad || 0, c.bad ? "bad" : "mute"],
      ["内置", c.core || 0, "mute"]
    ];
    box.innerHTML = bits.map(function (b) {
      return '<span class="pm-stat ' + b[2] + '"><b>' + b[1] + "</b>" + esc(b[0]) + "</span>";
    }).join("");
    if (d.env && d.env.writable === false) {
      box.innerHTML += '<span class="pm-stat bad" ' +
        'title="目录不可写，停用只能作用到本次运行">只读目录</span>';
    }
  }

  function filtered() {
    var list = (S.data && S.data.entries) || [];
    var q = S.q.trim().toLowerCase();
    return list.filter(function (e) {
      if (S.filter === "on" && !(e.state === "normal" || e.state === "external")) return false;
      if (S.filter === "off" && !(e.state === "disabled" || e.state === "removed")) return false;
      if (S.filter === "bad" && !(e.state === "broken" || e.state === "partial" ||
        e.state === "stale" || (e.warnings || []).length)) return false;
      if (S.filter === "third" && e.builtin) return false;
      if (!q) return true;
      return (e.name + " " + e.id + " " + e.dirname + " " + (e.summary || ""))
        .toLowerCase().indexOf(q) >= 0;
    });
  }

  function renderFilters() {
    var box = $("#pmFilters");
    if (!box || !S.data) return;
    var defs = [
      ["all", "全部"], ["on", "运行中"], ["off", "停用/卸载"],
      ["bad", "要处理"], ["third", "第三方"]
    ];
    box.innerHTML = defs.map(function (d) {
      return '<button class="pm-chip' + (S.filter === d[0] ? " on" : "") +
        '" data-act="filter" data-v="' + d[0] + '">' + esc(d[1]) + "</button>";
    }).join("");
  }

  function renderList() {
    var box = $("#pmList");
    if (!box) return;
    if (S.error) {
      box.innerHTML = '<div class="pm-empty"><b>读不到插件列表</b>' + esc(S.error) + "</div>";
      return;
    }
    if (!S.data) {
      box.innerHTML = '<div class="pm-empty"><b>正在扫描插件目录…</b></div>';
      return;
    }
    var list = filtered();
    if (!list.length) {
      var hasAny = (S.data.entries || []).length > 0;
      box.innerHTML = '<div class="pm-empty"><b>' +
        (hasAny ? "没有符合条件的插件" : "plugins/ 还是空的") + "</b>" +
        (hasAny ? "换个筛选条件试试。"
          : "把插件包（.zip）拖进来，或者把插件目录整个复制到这里：<br>" +
            '<code class="pm-empty-path">' + esc((S.data.env && S.data.env.root) || "") +
            "</code>") + "</div>";
      return;
    }
    box.innerHTML = list.map(card).join("");
  }

  function stateClass(e) {
    return e.builtin ? "core" : (e.state || "normal");
  }

  function card(e) {
    var open = !!S.open[e.id];
    var busy = S.busy === e.id;
    var initials = (e.name || e.id || "?").slice(0, 1);
    var pill = e.builtin ? "内置" : (e.stateText || e.state);

    var metrics = [];
    if (e.routes && e.routes.length) metrics.push("路由 " + e.routes.length);
    if (e.views && e.views.length) metrics.push("视图 " + e.views.length);
    if (e.settings && e.settings.length) metrics.push("设置 " + e.settings.length);
    if (e.depends && e.depends.length) metrics.push("依赖 " + e.depends.join("+"));
    if (!e.builtin && e.sizeText) metrics.push(e.sizeText);

    var btns = ['<button class="pm-btn pm-mini" data-act="toggle" data-id="' +
      esc(e.id) + '">' + (open ? "收起" : "详情") + "</button>"];

    if (e.canReload && !e.isSelf) {
      btns.push('<button class="pm-btn pm-mini" data-act="reload" data-id="' +
        esc(e.id) + '"' + (busy ? " disabled" : "") + ">重载</button>");
    }
    if (e.canDisable && !e.isSelf) {
      btns.push('<button class="pm-btn pm-mini" data-act="disable" data-id="' +
        esc(e.id) + '"' + (busy ? " disabled" : "") + ">停用</button>");
    }
    if (e.canEnable && !e.isSelf) {
      var label = e.state === "removed" ? "恢复"
        : (e.state === "disabled" ? "启用" : "重新加载");
      btns.push('<button class="pm-btn pm-mini pm-primary" data-act="enable" data-id="' +
        esc(e.id) + '"' + (busy ? " disabled" : "") + ">" + label + "</button>");
    }
    if (e.canRemove && !e.isSelf) {
      btns.push('<button class="pm-btn pm-mini pm-danger" data-act="remove" data-id="' +
        esc(e.id) + '"' + (busy ? " disabled" : "") + ">卸载</button>");
    }

    var warn = "";
    var warns = e.warnings || [];
    if (warns.length) {
      warn = '<div class="pm-hint ' + (isErr(e) ? "err" : "warn") + '">' +
        esc(warns[0]) + (warns.length > 1
          ? '<span class="pm-more">还有 ' + (warns.length - 1) + " 条</span>" : "") +
        "</div>";
    }

    return '<div class="pm-card pm-st-' + stateClass(e) + (open ? " open" : "") + '"' +
      ' data-id="' + esc(e.id) + '">' +
      '<div class="pm-row">' +
      '  <div class="pm-ico">' + esc(initials) + "</div>" +
      '  <div class="pm-main">' +
      '    <div class="pm-l1">' +
      '      <span class="pm-name">' + esc(e.name || e.id) + "</span>" +
      (e.version ? '<span class="pm-ver">v' + esc(e.version) + "</span>" : "") +
      '      <span class="pm-pill">' + esc(pill) + "</span>" +
      (e.isSelf ? '<span class="pm-tag">本插件</span>' : "") +
      (e.soft ? '<span class="pm-tag">仅本次运行</span>' : "") +
      "    </div>" +
      '    <div class="pm-l2">' + esc(e.id) +
      (e.dirname && e.dirname !== e.id
        ? ' <span class="pm-dim">·</span> ' + esc(e.dirname) : "") +
      (e.apiVersion && !e.builtin
        ? ' <span class="pm-dim">·</span> 契约 v' + esc(e.apiVersion) : "") +
      "    </div>" +
      (e.summary ? '    <div class="pm-sum">' + esc(e.summary) + "</div>" : "") +
      (metrics.length
        ? '    <div class="pm-metrics">' + metrics.map(function (m) {
            return "<span>" + esc(m) + "</span>";
          }).join("") + "</div>"
        : "") +
      warn +
      "  </div>" +
      '  <div class="pm-btns">' + btns.join("") + "</div>" +
      "</div>" +
      (open ? detailHtml(e) : "") +
      "</div>";
  }

  function isErr(e) {
    return e.state === "broken" || e.state === "partial";
  }

  // ---------------------------------------------------------------- 详情

  function detailHtml(e) {
    var d = S.detail[e.id];
    if (!d) {
      if (!S.busy) setTimeout(function () { fetchDetail(e.id); }, 0);
      return '<div class="pm-detail"><div class="pm-block">' +
        '<div class="pm-dim">正在读取详情…</div></div></div>';
    }

    var kv = [
      ["目录", d.relPath || d.dirname || "—"],
      ["路径", d.path || "—"],
      ["模块类", d.moduleClass || "—"],
      ["体积", d.sizeText ? d.sizeText + " / " + d.files + " 个文件" : "—"],
      ["修改时间", d.mtime ? fmtTime(d.mtime) + "（" + fmtAgo(d.mtime) + "）" : "—"]
    ];
    var kvHtml = kv.map(function (p) {
      return '<div class="pm-kv"><span>' + esc(p[0]) + "</span><code>" +
        esc(p[1]) + "</code></div>";
    }).join("");

    var warnHtml = (d.warnings || []).map(function (w) {
      return '<div class="pm-warnline">' + esc(w) + "</div>";
    }).join("");

    var routes = d.routes || [];
    var routeHtml = routes.length
      ? '<div class="pm-routes">' + routes.slice(0, 30).map(function (r) {
          var i = r.indexOf(" ");
          return '<div class="pm-route"><em>' + esc(r.slice(0, i)) + "</em>" +
            "<code>" + esc(r.slice(i + 1)) + "</code></div>";
        }).join("") +
        (routes.length > 30
          ? '<div class="pm-dim">另有 ' + (routes.length - 30) + " 条…</div>" : "") +
        "</div>"
      : '<div class="pm-dim">没有注册 HTTP 路由</div>';

    function tokens(list) {
      if (!list || !list.length) return '<span class="pm-dim">—</span>';
      return list.map(function (v) {
        return '<code class="pm-token">' + esc(v) + "</code>";
      }).join(" ");
    }

    var files = d.fileList || [];
    var fileHtml = files.length
      ? '<div class="pm-files">' + files.slice(0, 60).map(function (f) {
          var active = S.file[e.id] && S.file[e.id].path === f.path;
          return '<button class="pm-file' + (active ? " on" : "") +
            '" data-act="view-file" data-id="' + esc(e.id) + '" data-path="' +
            esc(f.path) + '">' + esc(f.path) + "<i>" + f.size + "B</i></button>";
        }).join("") + "</div>"
      : '<div class="pm-dim">没有可读文件</div>';

    var src = S.file[e.id];
    var srcHtml = src
      ? '<div class="pm-src"><div class="pm-srchead"><code>' + esc(src.path) +
        '</code><button class="pm-btn pm-mini" data-act="close-file" data-id="' +
        esc(e.id) + '">关闭</button></div><pre>' + esc(src.text || "") + "</pre></div>"
      : "";

    var note = ((S.data && S.data.notes) || {})[e.id] || d.notes || "";

    return '<div class="pm-detail">' +
      (warnHtml ? '<div class="pm-warnbox">' + warnHtml + "</div>" : "") +
      '<div class="pm-dgrid">' +
      '  <div class="pm-block"><h4>基本信息</h4>' + kvHtml + "</div>" +
      '  <div class="pm-block"><h4>路由 <i>' + routes.length + "</i></h4>" +
      routeHtml + "</div>" +
      '  <div class="pm-block"><h4>视图 / 设置 / 依赖</h4>' +
      '    <div class="pm-kv"><span>视图</span><div>' + tokens(d.views) + "</div></div>" +
      '    <div class="pm-kv"><span>设置</span><div>' + tokens(d.settings) + "</div></div>" +
      '    <div class="pm-kv"><span>依赖</span><div>' +
      ((d.depends || []).length ? tokens(d.depends)
        : '<span class="pm-dim">无</span>') + "</div></div>" +
      "  </div>" +
      '  <div class="pm-block"><h4>文件</h4>' + fileHtml + "</div>" +
      '  <div class="pm-block pm-block-wide"><h4>备注</h4>' +
      '    <div class="pm-notebox">' +
      '      <input class="pm-note" data-note="' + esc(e.id) + '" ' +
      '             placeholder="给自己留一句话，比如「等上游修了 bug 再开」" ' +
      '             value="' + esc(note) + '">' +
      '      <button class="pm-btn pm-mini" data-act="save-note" data-id="' +
      esc(e.id) + '">保存</button>' +
      "    </div>" +
      "  </div>" +
      "</div>" + srcHtml + "</div>";
  }

  function fetchDetail(id) {
    api("/api/pluginmgr/detail?id=" + encodeURIComponent(id)).then(function (res) {
      if (!res || !res.ok) { toast((res && res.error) || "读取详情失败"); return; }
      if (!S.open[id]) return;           // 期间被收起了，别硬画
      S.detail[id] = res;
      renderList();
    }).catch(function (e) { toast("读取详情失败：" + e.message); });
  }

  // ---------------------------------------------------------------- 回收站

  function renderTrash() {
    var box = $("#pmTrash");
    if (!box) return;
    var list = (S.data && S.data.trash) || [];
    if (!list.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = '<div class="pm-trashhead">' +
      '<h4>停用区 / 回收站 <i>' + list.length + "</i></h4>" +
      '<span class="pm-dim">停用只是移进 .disabled；卸载才是移进 .removed</span>' +
      '<span class="pm-grow"></span>' +
      '<button class="pm-btn pm-mini" data-act="open-removed">打开目录</button>' +
      "</div>" +
      '<div class="pm-trashlist">' + list.map(function (t) {
        var canRestore = t.state === "removed";
        return '<div class="pm-trashrow">' +
          "<code>" + esc(t.dirname) + "</code>" +
          '<span class="pm-dim">' + esc(t.sizeText || "") + "</span>" +
          '<span class="pm-dim">' + (t.state === "removed" ? "回收站" : "停用区") + "</span>" +
          '<span class="pm-grow"></span>' +
          (canRestore
            ? '<button class="pm-btn pm-mini pm-primary" data-act="restore" ' +
              'data-token="' + esc(t.token) + '">恢复</button>' : "") +
          '<button class="pm-btn pm-mini pm-danger" data-act="purge" data-token="' +
          esc(t.token) + '">彻底删除</button>' +
          "</div>";
      }).join("") + "</div>";
  }

  // ---------------------------------------------------------------- 体检

  function renderIssues() {
    var box = $("#pmIssues");
    if (!box) return;
    var issues = (S.data && S.data.issues) || [];
    if (!S.showIssues || !issues.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = '<div class="pm-issueshead">' +
      "<b>体检结果</b>" + '<span class="pm-dim">' + issues.length + " 条</span>" +
      '<span class="pm-grow"></span>' +
      '<button class="pm-btn pm-mini" data-act="doctor">收起</button></div>' +
      issues.map(function (i) {
        return '<div class="pm-issuerow ' + (i.level === "error" ? "err" : "warn") + '">' +
          "<b>" + esc(i.name) + "</b><span>" + esc(i.text) + "</span>" +
          '<button class="pm-btn pm-mini" data-act="jump" data-id="' + esc(i.id) +
          '">定位</button></div>';
      }).join("");
  }

  // ---------------------------------------------------------------- 动作

  function act(action, id, extra) {
    var body = { action: action, id: id };
    if (extra) {
      for (var k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) body[k] = extra[k];
    }
    S.busy = id;
    renderList();
    post("/api/pluginmgr/action", body).then(function (res) {
      S.busy = "";
      if (!res || !res.ok) {
        toast((res && res.error) || "操作失败");
        renderList();
        return;
      }
      var steps = res.steps || [];
      toast(labelOf(action) + "「" + id + "」" + (steps.length ? "：" + steps[0] : ""));
      load(true);
    }).catch(function (e) {
      S.busy = "";
      toast("操作失败：" + e.message);
      renderList();
    });
  }

  function labelOf(a) {
    return { disable: "已停用", enable: "已启用", reload: "已重载",
      remove: "已卸载", restore: "已恢复", purge: "已删除" }[a] || a;
  }

  function confirmThen(opts, fn) {
    var box = $("#pmModal");
    if (!box) { fn(); return; }
    box.hidden = false;
    box.innerHTML = '<div class="pm-modalbox">' +
      "<h3>" + esc(opts.title || "确认操作") + "</h3>" +
      '<div class="pm-modalbody">' + opts.body + "</div>" +
      '<div class="pm-modalacts">' +
      '<button class="pm-btn" data-modal="no">取消</button>' +
      '<button class="pm-btn ' + (opts.danger ? "pm-danger" : "pm-primary") +
      '" data-modal="yes">' + esc(opts.ok || "确定") + "</button></div></div>";
    box.querySelector('[data-modal="no"]').onclick = closeModal;
    box.querySelector('[data-modal="yes"]').onclick = function () {
      closeModal();
      fn();
    };
  }

  function closeModal() {
    var box = $("#pmModal");
    if (box) { box.hidden = true; box.innerHTML = ""; }
  }

  function onAction(action, target) {
    var id = target.getAttribute("data-id") || "";
    var e = id ? entryOf(id) : null;

    if (action === "refresh") { load(); return; }
    if (action === "doctor") {
      S.showIssues = !S.showIssues;
      if (S.showIssues && !((S.data && S.data.issues) || []).length) toast("体检通过，没发现问题");
      renderIssues();
      return;
    }
    if (action === "filter") {
      S.filter = target.getAttribute("data-v") || "all";
      renderFilters();
      renderList();
      return;
    }
    if (action === "install") { $("#pmFile").click(); return; }
    if (action === "open") { openFolder(""); return; }
    if (action === "open-disabled") { openFolder("disabled"); return; }
    if (action === "open-removed") { openFolder("removed"); return; }

    if (action === "toggle") {
      S.open[id] = !S.open[id];
      if (S.open[id] && !S.detail[id]) fetchDetail(id);
      renderList();
      return;
    }
    if (action === "jump") {
      S.open[id] = true;
      S.filter = "all";
      renderFilters();
      renderList();
      if (!S.detail[id]) fetchDetail(id);
      var card = el.querySelector('.pm-card[data-id="' + id + '"]');
      if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
      return;
    }

    if (action === "disable") {
      if (!e) return;
      confirmThen({
        title: "停用「" + e.name + "」",
        body: "<p>会把它移进 <code>plugins/.disabled/</code>，并<b>立刻</b>从当前进程" +
          "摘掉它注册的路由、视图和定时任务。</p>" +
          '<p class="pm-dim">随时可以再启用，不需要重启程序。</p>',
        ok: "停用"
      }, function () { act("disable", id); });
      return;
    }
    if (action === "enable") { act("enable", id); return; }
    if (action === "reload") { act("reload", id); return; }

    if (action === "remove") {
      if (!e) return;
      confirmThen({
        title: "卸载「" + e.name + "」",
        body: "<p>会把 <code>plugins/" + esc(e.dirname) + "</code> 移进回收站" +
          '（<code>.removed/</code>），并立刻停掉它的所有接口。</p>' +
          '<p class="pm-dim">这不是删除，还能在页面底部「恢复」。</p>',
        ok: "卸载",
        danger: true
      }, function () { act("remove", id); });
      return;
    }
    if (action === "restore") {
      act("restore", target.getAttribute("data-token"));
      return;
    }
    if (action === "purge") {
      var token = target.getAttribute("data-token");
      confirmThen({
        title: "彻底删除",
        body: "<p>会把 <code>.removed/" + esc(token) + "</code> 从磁盘上删掉，" +
          "<b>无法再恢复</b>。</p><p class=\"pm-dim\">只想关掉它的话，用「停用」就够了。</p>",
        ok: "永久删除",
        danger: true
      }, function () {
        post("/api/pluginmgr/action", { action: "purge", id: token }).then(function (res) {
          toast(res && res.ok ? "已删除" : ((res && res.error) || "删除失败"));
          load(true);
        });
      });
      return;
    }
    if (action === "view-file") {
      var path = target.getAttribute("data-path");
      if (S.file[id] && S.file[id].path === path) { delete S.file[id]; renderList(); return; }
      api("/api/pluginmgr/file?id=" + encodeURIComponent(id) +
        "&path=" + encodeURIComponent(path)).then(function (res) {
        if (!res || !res.ok) { toast((res && res.error) || "读不到文件"); return; }
        S.file[id] = { path: path, text: res.text };
        renderList();
      });
      return;
    }
    if (action === "close-file") { delete S.file[id]; renderList(); return; }
    if (action === "save-note") {
      var input = el.querySelector('.pm-note[data-note="' + id + '"]');
      var text = input ? input.value : "";
      post("/api/pluginmgr/note", { id: id, text: text }).then(function (res) {
        if (!res || !res.ok) { toast((res && res.error) || "备注没存上"); return; }
        if (!S.data.notes) S.data.notes = {};
        S.data.notes[id] = text;
        if (S.detail[id]) S.detail[id].notes = text;
        toast("备注已保存");
      });
      return;
    }
  }

  function openFolder(which) {
    post("/api/pluginmgr/open", { which: which }).then(function (res) {
      if (!res || !res.ok) {
        toast("打不开文件管理器" + (res && res.path ? "，路径：" + res.path : ""));
        return;
      }
      toast("已打开：" + res.path);
    });
  }

  // ---------------------------------------------------------------- 安装

  function installFile(file) {
    if (!file) return;
    if (!/\.zip$/i.test(file.name)) { toast("只支持 .zip 插件包"); return; }
    toast("正在安装 " + file.name + "…");
    var reader = new FileReader();
    reader.onload = function () {
      fetch("/api/pluginmgr/install?name=" + encodeURIComponent(file.name), {
        method: "POST",
        body: reader.result
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (!res || !res.ok) { toast((res && res.error) || "安装失败"); return; }
        var steps = res.steps || [];
        toast("装好了：" + (res.id || res.dirname) +
          (steps.length ? "（" + steps[steps.length - 1] + "）" : ""));
        load(true);
      }).catch(function (e) { toast("安装失败：" + e.message); });
    };
    reader.onerror = function () { toast("读不了这个文件"); };
    reader.readAsArrayBuffer(file);
  }

  // ---------------------------------------------------------------- 挂载

  function mount(host, ctx) {
    C = ctx;
    el = host;
    el.innerHTML = skeleton();
    if (!C.api) {
      $("#pmList").innerHTML = '<div class="pm-empty"><b>缺 ctx.api</b>' +
        "这个视图需要主程序提供的 ARR.api。</div>";
      return;
    }

    var fileInput = $("#pmFile");
    fileInput.onchange = function () {
      if (fileInput.files && fileInput.files[0]) installFile(fileInput.files[0]);
      fileInput.value = "";
    };

    // 一个委托搞定全部按钮：卡片是整块重绘的，逐个绑定会漏掉新建的节点
    el.addEventListener("click", function (ev) {
      var t = ev.target;
      while (t && t !== el) {
        var act = t.getAttribute && t.getAttribute("data-act");
        if (act) { ev.preventDefault(); onAction(act, t); return; }
        if (t.getAttribute && t.getAttribute("data-modal") === "no") {
          closeModal();
          return;
        }
        t = t.parentNode;
      }
    });

    var search = $("#pmSearch");
    search.value = S.q;
    search.addEventListener("input", function () {
      S.q = search.value;
      renderList();
    });

    // 拖 zip 进来：比「点按钮 → 找文件」快一步
    var drop = $("#pmDrop");
    var depth = 0;
    el.addEventListener("dragenter", function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      depth++;
      drop.hidden = false;
    });
    el.addEventListener("dragover", function (ev) {
      if (hasFiles(ev)) { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; }
    });
    el.addEventListener("dragleave", function () {
      depth = Math.max(0, depth - 1);
      if (!depth) drop.hidden = true;
    });
    el.addEventListener("drop", function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      depth = 0;
      drop.hidden = true;
      var files = ev.dataTransfer.files;
      if (files && files[0]) installFile(files[0]);
    });

    load();
  }

  function hasFiles(ev) {
    var dt = ev.dataTransfer;
    if (!dt) return false;
    if (dt.types && Array.prototype.indexOf.call(dt.types, "Files") >= 0) return true;
    return !!(dt.files && dt.files.length);
  }

  function unmount() {
    closeModal();
    el = null;
    C = null;
  }

  if (window.ARR && typeof window.ARR.registerView === "function") {
    window.ARR.registerView({
      id: VIEW_ID,
      title: "插件管理",
      icon: "⧉",
      order: 95,
      description: "看清楚装了哪些插件、它们的状态，停用/启用/重载/安装",
      mount: mount,
      unmount: unmount
    });
  }
})();
