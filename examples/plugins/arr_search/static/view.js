/* 深度搜索 —— 插件自带的前端视图。
 *
 * 主程序的 HTML / JS 里没有为它写过一行代码：后端在 bootstrap 的 views 清单里
 * 声明这个视图，主程序按清单加载 /plug/search/view.js，脚本自己调
 * ARR.registerView 注册，侧栏就多一个入口。
 *
 * 能用的 ctx（见 docs/ARCHITECTURE.md §4.4）：
 *   ctx.api.get(url) / ctx.api.send(method, url, body)
 *   ctx.toast(msg) / ctx.esc(str) / ctx.on/off/emit / ctx.openItem(id)
 */
(function () {
  "use strict";

  var STORE_KEY = "arr.search.form";

  var S = {
    q: "",
    must: "",          // 必含词（全部命中），等价语法里的 +词
    exclude: "",       // 排除词（命中任一即去掉），等价语法里的 -词
    fansub: "",        // 字幕组，等价语法里的 字幕组:
    sizeMin: "",       // 体积下限，等价语法里的 size>1G
    sources: [],
    after: "",
    before: "",
    sort: "date",
    pages: 3,
    page: 1,
    limit: 30,
    items: [],
    providers: [],
    tips: [],
    presets: [],
    history: [],
    watched: {},       // item.id -> watch_id：这批结果里哪些已经在关注列表
    hasMore: false,
    loading: false,
    took: 0,
    raw: 0,
    lastQuery: null
  };

  var el = null;      // 视图根节点
  var ctxRef = null;
  // 从侧栏快捷搜索带进来的词。视图还没挂载时先存这儿，boot 完再搜。
  var pendingQ = "";

  // ---------------------------------------------------------------- 小工具

  function $(sel, root) { return (root || el).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || el).querySelectorAll(sel));
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function fmtAgo(ts) {
    if (!ts) return "";
    var diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 0) diff = 0;
    if (diff < 3600) return Math.max(1, Math.round(diff / 60)) + " 分钟前";
    if (diff < 86400) return Math.round(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.round(diff / 86400) + " 天前";
    var y = Math.floor(diff / (86400 * 365));
    if (y >= 1) return y + " 年前";
    return Math.round(diff / (86400 * 30)) + " 个月前";
  }

  function saveForm() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        sources: S.sources, sort: S.sort, pages: S.pages, limit: S.limit,
        must: S.must, exclude: S.exclude, fansub: S.fansub, sizeMin: S.sizeMin
      }));
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function loadForm() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); resolve(); }
      catch (e) { reject(e); }
      document.body.removeChild(ta);
    });
  }

  // ---------------------------------------------------------------- 骨架

  function skeleton() {
    el.innerHTML =
      '<div class="sq-wrap">' +
      '  <header class="sq-head">' +
      '    <div class="sq-headinfo">' +
      '      <h2>深度搜索</h2>' +
      '      <p class="sq-sub">跨源站检索历史帖子 —— 主程序只保留最新 200 条，这里可以往回翻。</p>' +
      '    </div>' +
      '    <button class="sq-btn sq-btn-ghost" id="sqHelp" title="也可以直接写查询语法">高级语法</button>' +
      '  </header>' +
      '  <div class="sq-help" id="sqHelpBox" hidden></div>' +
      '  <div class="sq-bar">' +
      '    <input class="sq-input" id="sqQ" type="search" spellcheck="false" ' +
      '      placeholder="番名或关键词，空格分隔（命中任一个就算）  ·  回车搜索">' +
      '    <button class="sq-btn sq-btn-primary" id="sqGo">搜索</button>' +
      '  </div>' +
      // 这一排是给不想记语法的人准备的：每个框对应语法里的一个符号
      '  <div class="sq-filters sq-filters-main">' +
      '    <label class="sq-field sq-field-grow"><span>必含</span>' +
      '      <input class="sq-input" id="sqMust" type="text" spellcheck="false" ' +
      '        placeholder="全部都要有，空格分隔"></label>' +
      '    <label class="sq-field"><span>排除</span>' +
      '      <input class="sq-input" id="sqExclude" type="text" spellcheck="false" ' +
      '        placeholder="命中就丢掉"></label>' +
      '    <label class="sq-field"><span>字幕组</span>' +
      '      <input class="sq-input" id="sqFansub" type="text" spellcheck="false" ' +
      '        list="sqFansubList" placeholder="不限">' +
      '      <datalist id="sqFansubList"></datalist></label>' +
      '    <label class="sq-field"><span>体积不小于</span>' +
      '      <select id="sqSize">' +
      '        <option value="">不限</option>' +
      '        <option value="300M">300 MB</option>' +
      '        <option value="700M">700 MB</option>' +
      '        <option value="1G">1 GB</option>' +
      '        <option value="3G">3 GB</option>' +
      '        <option value="10G">10 GB</option>' +
      '      </select></label>' +
      '  </div>' +
      '  <div class="sq-filters">' +
      '    <label class="sq-field"><span>时间</span>' +
      '      <select id="sqRange">' +
      '        <option value="">不限</option>' +
      '        <option value="1y">近一年</option>' +
      '        <option value="3y">近三年</option>' +
      '        <option value="5y">近五年</option>' +
      '        <option value="older">更早（三年前）</option>' +
      '        <option value="custom">自定义…</option>' +
      '      </select>' +
      '    </label>' +
      '    <label class="sq-field sq-field-date" hidden><span>从</span>' +
      '      <input id="sqAfter" type="date"></label>' +
      '    <label class="sq-field sq-field-date" hidden><span>到</span>' +
      '      <input id="sqBefore" type="date"></label>' +
      '    <label class="sq-field"><span>排序</span>' +
      '      <select id="sqSort">' +
      '        <option value="date">最新</option>' +
      '        <option value="seeders">做种</option>' +
      '        <option value="size">体积</option>' +
      '        <option value="relevance">相关度</option>' +
      '      </select>' +
      '    </label>' +
      '    <label class="sq-field"><span>深度</span>' +
      '      <select id="sqDepth">' +
      '        <option value="1">快（1 页）</option>' +
      '        <option value="3">深（3 页）</option>' +
      '        <option value="10">归档（10 页）</option>' +
      '      </select>' +
      '    </label>' +
      '    <label class="sq-field"><span>每页</span>' +
      '      <select id="sqLimit">' +
      '        <option value="20">20</option>' +
      '        <option value="30">30</option>' +
      '        <option value="60">60</option>' +
      '      </select>' +
      '    </label>' +
      '    <button class="sq-btn sq-btn-ghost" id="sqReset" title="清空必含 / 排除 / 字幕组 / 体积">清空条件</button>' +
      '  </div>' +
      '  <div class="sq-chips" id="sqChips"></div>' +
      '  <div class="sq-combo">' +
      '    <input class="sq-input sq-input-sm" id="sqPresetName" ' +
      '      placeholder="把这套组合存个名字…">' +
      '    <button class="sq-btn" id="sqSavePreset">保存组合</button>' +
      '    <button class="sq-btn sq-btn-watch" id="sqWatchAll" ' +
      '      title="按当前条件再搜一次，把命中的全部挂进关注列表">整次搜索存为关注</button>' +
      '    <div class="sq-presets" id="sqPresets"></div>' +
      '  </div>' +
      '  <div class="sq-status" id="sqStatus"></div>' +
      '  <div class="sq-list" id="sqList"></div>' +
      '  <div class="sq-more" id="sqMore"></div>' +
      '</div>';

    // 输入框回车即搜
    $("#sqQ").addEventListener("keydown", function (e) {
      if (e.key === "Enter") run(1);
    });
    $("#sqGo").addEventListener("click", function () { run(1); });

    // 四个图形条件：改完按「搜索」或直接回车生效（不自动重搜，避免打字打一半就发请求）
    ["sqMust", "sqExclude", "sqFansub"].forEach(function (id) {
      $("#" + id).addEventListener("keydown", function (e) {
        if (e.key === "Enter") { syncFilters(); run(1); }
      });
    });
    $("#sqSize").addEventListener("change", function () {
      S.sizeMin = this.value; saveForm();
      if (S.items.length) run(1);
    });
    $("#sqReset").addEventListener("click", function () {
      S.must = ""; S.exclude = ""; S.fansub = ""; S.sizeMin = "";
      $("#sqMust").value = ""; $("#sqExclude").value = "";
      $("#sqFansub").value = ""; $("#sqSize").value = "";
      saveForm();
      if (S.items.length) run(1);
    });
    $("#sqWatchAll").addEventListener("click", function () { watchAll(this); });

    $("#sqSort").addEventListener("change", function () {
      S.sort = this.value; saveForm(); if (S.items.length) run(1);
    });
    $("#sqDepth").addEventListener("change", function () {
      S.pages = parseInt(this.value, 10) || 3; saveForm();
    });
    $("#sqLimit").addEventListener("change", function () {
      S.limit = parseInt(this.value, 10) || 30; saveForm();
    });
    $("#sqRange").addEventListener("change", onRange);
    $("#sqAfter").addEventListener("change", function () {
      S.after = this.value; run(1);
    });
    $("#sqBefore").addEventListener("change", function () {
      S.before = this.value; run(1);
    });
    $("#sqSavePreset").addEventListener("click", savePreset);
    $("#sqHelp").addEventListener("click", function () {
      var box = $("#sqHelpBox");
      box.hidden = !box.hidden;
    });
  }

  /** 把四个图形输入框的值读回 S（输入框才是唯一真相）。 */
  function syncFilters() {
    S.must = $("#sqMust").value.trim();
    S.exclude = $("#sqExclude").value.trim();
    S.fansub = $("#sqFansub").value.trim();
    S.sizeMin = $("#sqSize").value;
    saveForm();
  }

  /** 把 S 里的条件写回输入框（恢复表单 / 应用组合时用）。 */
  function fillFilters() {
    $("#sqMust").value = S.must || "";
    $("#sqExclude").value = S.exclude || "";
    $("#sqFansub").value = S.fansub || "";
    $("#sqSize").value = S.sizeMin || "";
  }

  // ---------------------------------------------------------------- 源 chips

  function renderChips() {
    var box = $("#sqChips");
    if (!S.providers.length) {
      box.innerHTML = '<span class="sq-hint">正在读取数据源…</span>';
      return;
    }
    box.innerHTML = S.providers.map(function (p) {
      var on = S.sources.indexOf(p.id) >= 0;
      var h = p.health || {};
      var state = h.ok === false ? " sq-chip-bad" : (h.ok ? " sq-chip-ok" : "");
      var tip = p.note || "";
      if (h.ok !== undefined) {
        tip += " ｜ 上次 " + (h.ms || 0) + "ms · " + (h.count || 0) + " 条" +
          (h.error ? " · " + h.error : "");
      }
      return '<button class="sq-chip' + (on ? " sq-chip-on" : "") + state + '" ' +
        'data-id="' + p.id + '" title="' + ctxRef.esc(tip) + '">' +
        ctxRef.esc(p.name) + (p.kind === "local" ? " ·" : "") + "</button>";
    }).join("");
    $$(".sq-chip", box).forEach(function (b) {
      b.addEventListener("click", function () {
        var id = this.getAttribute("data-id");
        var i = S.sources.indexOf(id);
        if (i >= 0) S.sources.splice(i, 1);
        else S.sources.push(id);
        saveForm();
        renderChips();
      });
    });
  }

  function renderTips() {
    var box = $("#sqHelpBox");
    if (!box) return;
    box.innerHTML = "<b>查询语法</b><ul>" + (S.tips || []).map(function (t) {
      return "<li>" + ctxRef.esc(t) + "</li>";
    }).join("") + "</ul>" +
      '<p class="sq-hint">组合是可以叠加的：源 × 关键词 × 时间 × 排序，' +
      "任意一层都能改，结果语义不变。</p>";
  }

  // ---------------------------------------------------------------- 时间

  function onRange() {
    var v = this.value;
    var showCustom = v === "custom";
    $$(".sq-field-date").forEach(function (n) { n.hidden = !showCustom; });
    if (showCustom) return;

    S.after = "";
    S.before = "";
    if (v === "1y") S.after = relDate(365);
    else if (v === "3y") S.after = relDate(365 * 3);
    else if (v === "5y") S.after = relDate(365 * 5);
    else if (v === "older") S.before = relDate(365 * 3);
    $("#sqAfter").value = S.after;
    $("#sqBefore").value = S.before;
    if (S.items.length) run(1);
  }

  function relDate(days) {
    var d = new Date(Date.now() - days * 86400000);
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  // ---------------------------------------------------------------- 搜索

  function run(page) {
    if (S.loading) return;
    syncFilters();                       // 输入框才是真相，先收回 S
    var q = $("#sqQ").value.trim();
    if (!q && !S.must && !S.fansub && !S.after && !S.before) {
      ctxRef.toast("给个关键词，或者填「必含 / 字幕组」、选一个时间范围");
      $("#sqQ").focus();
      return;
    }
    S.q = q;
    S.page = page || 1;
    S.loading = true;
    if (S.page === 1) { S.items = []; S.watched = {}; renderList(); }
    setStatus('<span class="sq-spin"></span> 正在检索 ' + S.sources.length + " 个源…");

    var qs = [
      "q=" + encodeURIComponent(q),
      "src=" + encodeURIComponent(S.sources.join(",")),
      "sort=" + encodeURIComponent(S.sort),
      "pages=" + S.pages,
      "page=" + S.page,
      "limit=" + S.limit
    ];
    if (S.after) qs.push("after=" + encodeURIComponent(S.after));
    if (S.before) qs.push("before=" + encodeURIComponent(S.before));
    // 图形条件作为独立参数走，后端和语法解析落到同一个 Query 上
    if (S.must) qs.push("must=" + encodeURIComponent(S.must));
    if (S.exclude) qs.push("exclude=" + encodeURIComponent(S.exclude));
    if (S.fansub) qs.push("fansub=" + encodeURIComponent(S.fansub));
    if (S.sizeMin) qs.push("sizeMin=" + encodeURIComponent(S.sizeMin));

    ctxRef.api.get("/api/search?" + qs.join("&")).then(function (res) {
      S.loading = false;
      if (!res || !res.ok) {
        setStatus('<span class="sq-err">' + ctxRef.esc((res && res.error) || "检索失败") + "</span>");
        return;
      }
      S.items = S.page === 1 ? res.items : S.items.concat(res.items);
      S.hasMore = !!res.hasMore;
      S.took = res.took;
      S.raw = res.raw;
      S.lastQuery = res.query;
      (res.sources || []).forEach(function (s) {
        var p = S.providers.filter(function (x) { return x.id === s.id; })[0];
        if (p) p.health = s;
      });
      renderChips();
      renderFansubHints(S.items);
      renderList();
      renderStatus(res);
      refreshWatchStatus(S.items);
      if (res.took > 6000) ctxRef.toast("有点慢，试试减少源或降低深度");
    }).catch(function (e) {
      S.loading = false;
      setStatus('<span class="sq-err">请求失败：' + ctxRef.esc(e.message) + "</span>");
    });
  }

  /** 从结果标题的 [..] 里收集字幕组，给「字幕组」输入框做补全。 */
  function renderFansubHints(items) {
    var box = $("#sqFansubList");
    if (!box) return;
    var seen = {}, out = [];
    (items || []).forEach(function (it) {
      if (it.author && !seen[it.author]) {
        seen[it.author] = 1; out.push(it.author);
        return;
      }
      var m = /\[([^\[\]]{2,20})\]/.exec(it.title || "");
      if (m && !seen[m[1]]) { seen[m[1]] = 1; out.push(m[1]); }
    });
    box.innerHTML = out.slice(0, 40).map(function (n) {
      return '<option value="' + ctxRef.esc(n) + '"></option>';
    }).join("");
  }

  function renderStatus(res) {
    var ok = (res.sources || []).filter(function (s) { return s.ok; }).length;
    var bad = (res.sources || []).filter(function (s) { return !s.ok; });
    var parts = [
      "命中 <b>" + res.count + "</b> 条",
      "合并去重后 " + res.matched + " / 原始 " + res.raw,
      res.took + " ms",
      ok + " 个源可用" + (res.cached ? "（缓存）" : "")
    ];
    var html = parts.join(" · ");
    if (bad.length) {
      html += '<div class="sq-err-line">失败：' + bad.map(function (b) {
        return ctxRef.esc(b.name + "（" + (b.error || "未知") + "）");
      }).join("；") + "</div>";
    }
    setStatus(html);
  }

  function setStatus(html) {
    var box = $("#sqStatus");
    if (box) box.innerHTML = html;
  }

  // ---------------------------------------------------------------- 列表

  function renderList() {
    var box = $("#sqList");
    if (!box) return;
    if (!S.items.length) {
      box.innerHTML = S.loading ? "" :
        '<div class="sq-empty"><b>没有结果</b>' +
        "<p>换个更短的关键词，或者把深度调到 10 页 / 圈一个更早的时间区间。</p></div>";
      $("#sqMore").innerHTML = "";
      return;
    }
    box.innerHTML = S.items.map(rowHtml).join("");
    $$(".sq-item", box).forEach(function (node, i) {
      var it = S.items[i];
      var magBtn = node.querySelector(".sq-mag");
      if (magBtn) {
        magBtn.addEventListener("click", function () { openLink(it, false); });
      }
      var pickBtn = node.querySelector(".sq-mag-pick");
      if (pickBtn) {
        pickBtn.addEventListener("click", function () { openLink(it, true); });
      }
      var keepBtn = node.querySelector(".sq-keep");
      if (keepBtn) {
        keepBtn.addEventListener("click", function () { keep(it, this); });
      }
      var watchBtn = node.querySelector(".sq-watch");
      if (watchBtn) {
        watchBtn.addEventListener("click", function () { watchItem(it, this); });
      }
      if (it.link) {
        node.querySelector(".sq-title").addEventListener("click", function () {
          window.open(it.link, "_blank", "noopener");
        });
      }
    });
    var more = $("#sqMore");
    more.innerHTML = S.hasMore
      ? '<button class="sq-btn sq-btn-wide" id="sqMoreBtn">加载更旧的（第 ' +
        (S.page + 1) + " 页）</button>"
      : '<span class="sq-hint">到底了。想更深就调「深度」或指定更早的时间。</span>';
    if (S.hasMore) {
      $("#sqMoreBtn").addEventListener("click", function () {
        S.page += 1;
        run(S.page);
      });
    }
  }

  function rowHtml(it) {
    var meta = [
      '<span class="sq-src">' + ctxRef.esc(it.sourceName || it.source) + "</span>",
      '<span title="' + ctxRef.esc(it.pubdate || "") + '">' + fmtDate(it.ts) +
        (it.ts ? " · " + fmtAgo(it.ts) : "") + "</span>"
    ];
    if (it.size) meta.push("<span>" + ctxRef.esc(it.size) + "</span>");
    if (it.seeders !== null && it.seeders !== undefined) {
      meta.push("<span>做种 " + it.seeders + "</span>");
    }
    if (it.local) meta.push('<span class="sq-tag">本地</span>');
    if (it.alsoIn && it.alsoIn.length) {
      meta.push('<span class="sq-tag">也在 ' + it.alsoIn.length + " 个源</span>");
    }
    var acts = [];
    if (it.magnet || it.torrent) acts.push(magButtonHtml());
    if (it.link) acts.push('<button class="sq-act sq-keep" title="存进本地库并加星">收藏</button>');
    var on = S.watched[it.id];
    acts.push('<button class="sq-act sq-watch' + (on ? " sq-watch-on" : "") +
      '" title="' + (on ? "已在关注列表里" : "把这条挂进主程序的关注列表") + '">' +
      (on ? "已关注" : "关注") + "</button>");
    return '<article class="sq-item">' +
      '<h3 class="sq-title" title="' + ctxRef.esc(it.title) + '">' +
      ctxRef.esc(it.title) + "</h3>" +
      '<div class="sq-meta">' + meta.join('<i>·</i>') + "</div>" +
      '<div class="sq-acts">' + acts.join("") + "</div>" +
      "</article>";
  }

  // ------------------------------------------------------------ 磁力 → 唤醒
  //
  // 「下载」按钮不自己搞下载：把 URL 交给链接唤醒插件（如果装了），由它
  // 认出这是磁力 / 种子 / 网盘，再按用户设的「优先响应」交给 qB、aria2 或
  // 系统关联程序。没装那个插件就退回复制 —— 这是老行为，不能更差。
  //
  // 两个插件互不认识 id，只通过主程序的 ARR.can / ARR.invoke 这一层说话。
  function wakeReady() {
    return !!(window.ARR && typeof ARR.can === "function" && ARR.can("link.wake"));
  }

  function magButtonHtml() {
    if (!wakeReady()) {
      return '<button class="sq-act sq-mag" title="复制磁力链接">磁力</button>';
    }
    return '<span class="sq-split">' +
      '<button class="sq-act sq-mag" title="交给下载器（在「链接唤醒」里改用哪个）">下载</button>' +
      '<button class="sq-act sq-mag-pick" title="这次换个方式打开">▾</button>' +
      "</span>";
  }

  function openLink(it, ask) {
    var url = it.magnet || it.torrent || "";
    if (!url) { ctxRef.toast("这条没有磁力或种子"); return; }
    if (wakeReady() &&
        ARR.invoke("link.wake", { url: url, title: it.title, ask: !!ask })) {
      return;
    }
    // 没有链接唤醒：磁力只能复制，种子链接就开页面
    if (it.magnet) {
      copyText(it.magnet).then(function () {
        ctxRef.toast("磁力已复制（装个「链接唤醒」就能一键交给下载器）");
      }).catch(function () { ctxRef.toast("复制失败，请手动选择"); });
    } else {
      window.open(it.torrent, "_blank", "noopener");
    }
  }

  function keep(it, btn) {
    btn.disabled = true;
    ctxRef.api.send("POST", "/api/search/keep", { items: [it] }).then(function (res) {
      btn.disabled = false;
      if (res && res.ok) {
        btn.textContent = "已收藏";
        ctxRef.toast("已存入「搜索收藏」并加星（不会被 30 天清理带走）");
      } else {
        ctxRef.toast((res && res.error) || "收藏失败");
      }
    }).catch(function (e) {
      btn.disabled = false;
      ctxRef.toast("收藏失败：" + e.message);
    });
  }

  // ---------------------------------------------------------------- 关注

  /** 拼关注接口的请求体：当前这套条件 + 本次要挂的条目。 */
  function watchBody(extra) {
    var b = {
      q: $("#sqQ").value.trim(),
      must: S.must, exclude: S.exclude, fansub: S.fansub,
      after: S.after, before: S.before, src: S.sources
    };
    for (var k in (extra || {})) b[k] = extra[k];
    return b;
  }

  function watchItem(it, btn) {
    if (btn.getAttribute("data-busy") === "1") return;
    btn.setAttribute("data-busy", "1");
    btn.disabled = true;
    ctxRef.api.send("POST", "/api/search/watch", watchBody({ items: [it] }))
      .then(function (res) {
        btn.disabled = false;
        btn.setAttribute("data-busy", "");
        if (res && res.ok) {
          S.watched[it.id] = res.watch.id;
          btn.textContent = "已关注";
          btn.classList.add("sq-watch-on");
          btn.title = "已在关注「" + res.watch.name + "」里";
          ctxRef.toast("已加入关注「" + res.watch.name + "」· 现在共 " +
            res.watch.total + " 条");
          showNotes(res.notes);
        } else {
          ctxRef.toast((res && res.error) || "加入关注失败");
        }
      }).catch(function (e) {
        btn.disabled = false;
        btn.setAttribute("data-busy", "");
        ctxRef.toast("加入关注失败：" + e.message);
      });
  }

  /** 整次搜索存为关注：只发条件，让后端按条件重跑再挂（走缓存，很快）。 */
  function watchAll(btn) {
    syncFilters();
    var q = $("#sqQ").value.trim();
    if (!q && !S.must && !S.fansub) {
      ctxRef.toast("先给个关键词或字幕组，才好在关注里长期跟");
      $("#sqQ").focus();
      return;
    }
    if (btn.disabled) return;
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "正在挂载…";
    ctxRef.api.send("POST", "/api/search/watch", watchBody({ all: true, pages: S.pages }))
      .then(function (res) {
        btn.disabled = false;
        btn.textContent = old;
        if (res && res.ok) {
          ctxRef.toast("已把 " + res.attached + " 条挂进关注「" + res.watch.name +
            "」· 现在共 " + res.watch.total + " 条");
          showNotes(res.notes);
          refreshWatchStatus(S.items);
        } else {
          ctxRef.toast((res && res.error) || "加入关注失败");
        }
      }).catch(function (e) {
        btn.disabled = false;
        btn.textContent = old;
        ctxRef.toast("加入关注失败：" + e.message);
      });
  }

  /** 问一次后端：这批结果里哪些已经在关注里，把按钮点亮。 */
  function refreshWatchStatus(items) {
    var payload = (items || []).filter(function (x) { return x.id; })
      .map(function (x) {
        return { id: x.id, infohash: x.infohash, link: x.link, title: x.title };
      });
    if (!payload.length) return;
    ctxRef.api.send("POST", "/api/search/watch/status", { items: payload })
      .then(function (res) {
        if (res && res.ok) { S.watched = res.watched || {}; markWatched(); }
      }).catch(function () { /* 状态查不到就当没关注，不影响搜索 */ });
  }

  function markWatched() {
    $$(".sq-item").forEach(function (node, i) {
      var it = S.items[i];
      if (!it) return;
      var btn = node.querySelector(".sq-watch");
      if (!btn || !S.watched[it.id]) return;
      btn.textContent = "已关注";
      btn.classList.add("sq-watch-on");
      btn.title = "已在关注列表里";
    });
  }

  /** 关注规则和检索条件不等价的地方要说出来，别让用户以为时间窗也跟着走。 */
  function showNotes(notes) {
    var box = $("#sqStatus");
    if (!box) return;
    // 先清掉上一条：连点两次不该叠出两行一样的说明
    $$(".sq-note", box).forEach(function (n) { n.parentNode.removeChild(n); });
    if (!notes || !notes.length) return;
    var div = document.createElement("div");
    div.className = "sq-note";
    div.textContent = notes.join("；");
    box.appendChild(div);
  }

  // ---------------------------------------------------------------- 组合

  function savePreset() {
    var name = $("#sqPresetName").value.trim();
    if (!name) { ctxRef.toast("给这套组合起个名字"); return; }
    ctxRef.api.send("POST", "/api/search/presets", {
      name: name, query: composeQuery(), sources: S.sources,
      mode: String(S.pages), sort: S.sort
    }).then(function (res) {
      if (res && res.ok) {
        S.presets = res.presets || [];
        renderPresets();
        $("#sqPresetName").value = "";
        ctxRef.toast("组合已保存");
      } else {
        ctxRef.toast((res && res.error) || "保存失败");
      }
    });
  }

  function renderPresets() {
    var box = $("#sqPresets");
    if (!box) return;
    if (!S.presets.length) {
      box.innerHTML = '<span class="sq-hint">还没有保存的组合。' +
        "源 + 条件 存下来，下次一点就跑。</span>";
      return;
    }
    box.innerHTML = S.presets.map(function (p) {
      return '<span class="sq-preset" data-id="' + p.id + '">' +
        ctxRef.esc(p.name) +
        '<i class="sq-preset-x" data-id="' + p.id + '" title="删除">×</i></span>';
    }).join("");
    $$(".sq-preset", box).forEach(function (n) {
      n.addEventListener("click", function (e) {
        if (e.target.classList.contains("sq-preset-x")) return;
        applyPreset(parseInt(this.getAttribute("data-id"), 10));
      });
    });
    $$(".sq-preset-x", box).forEach(function (n) {
      n.addEventListener("click", function (e) {
        e.stopPropagation();
        var pid = this.getAttribute("data-id");
        ctxRef.api.send("DELETE", "/api/search/presets/" + pid).then(function (res) {
          if (res && res.ok) { S.presets = res.presets || []; renderPresets(); }
        });
      });
    });
  }

  function applyPreset(pid) {
    var p = S.presets.filter(function (x) { return x.id === pid; })[0];
    if (!p) return;
    // 组合里存的是语法串，拆回图形框，用户看到的就是他当初填的那几格
    var d = decomposeQuery(p.query || "");
    $("#sqQ").value = d.q;
    S.must = d.must.join(" ");
    S.exclude = d.exclude.join(" ");
    S.fansub = d.fansub;
    S.sizeMin = d.sizeMin;
    setSizeSelect(d.sizeMin);
    fillFilters();
    S.sources = (p.sources && p.sources.length) ? p.sources.slice() : S.sources;
    S.sort = p.sort || "date";
    S.pages = parseInt(p.mode, 10) || 3;
    $("#sqSort").value = S.sort;
    $("#sqDepth").value = String(S.pages > 5 ? 10 : (S.pages > 1 ? 3 : 1));
    renderChips();
    run(1);
  }

  /** 当前这套条件 -> 语法串。存组合时用它，组合才是完整快照。 */
  function composeQuery() {
    syncFilters();
    var parts = [];
    var q = $("#sqQ").value.trim();
    if (q) parts.push(q);
    (S.must || "").split(/\s+/).forEach(function (w) { if (w) parts.push("+" + w); });
    (S.exclude || "").split(/\s+/).forEach(function (w) { if (w) parts.push("-" + w); });
    if (S.fansub) parts.push("字幕组:" + S.fansub);
    if (S.sizeMin) parts.push("size>" + S.sizeMin);
    return parts.join(" ");
  }

  /** 语法串 -> 图形框。只认这里能表达的那几种符号，其余当关键词。 */
  function decomposeQuery(raw) {
    var out = { q: "", must: [], exclude: [], fansub: "", sizeMin: "" };
    (raw || "").split(/\s+/).forEach(function (tok) {
      if (!tok) return;
      if (tok.length > 1 && tok[0] === "+") { out.must.push(tok.slice(1)); return; }
      if (tok.length > 1 && tok[0] === "-") { out.exclude.push(tok.slice(1)); return; }
      var m = /^(?:字幕组|fansub|team):(.+)$/i.exec(tok);
      if (m) { out.fansub = m[1]; return; }
      m = /^size\s*>\s*([\d.]+\s*[KMGT]?)$/i.exec(tok);
      if (m) { out.sizeMin = m[1].replace(/\s+/g, "").toUpperCase(); return; }
      out.q += (out.q ? " " : "") + tok;
    });
    return out;
  }

  /** 体积下拉是固定几档，组合里带来别的值时临时补一个选项。 */
  function setSizeSelect(v) {
    var sel = $("#sqSize");
    var found = Array.prototype.some.call(sel.options, function (o) {
      return o.value === (v || "");
    });
    if (!found && v) {
      var o = document.createElement("option");
      o.value = v;
      o.textContent = "不小于 " + v;
      sel.appendChild(o);
    }
    sel.value = v || "";
  }

  // ---------------------------------------------------------------- 装配

  function boot() {
    var saved = loadForm();
    if (saved) {
      S.sources = saved.sources || [];
      S.sort = saved.sort || "date";
      S.pages = saved.pages || 3;
      S.limit = saved.limit || 30;
      S.must = saved.must || "";
      S.exclude = saved.exclude || "";
      S.fansub = saved.fansub || "";
      S.sizeMin = saved.sizeMin || "";
      setSizeSelect(S.sizeMin);
      fillFilters();
    }
    $("#sqSort").value = S.sort;
    $("#sqDepth").value = S.pages > 5 ? "10" : (S.pages > 1 ? "3" : "1");
    $("#sqLimit").value = String(S.limit);

    ctxRef.api.get("/api/search/providers").then(function (res) {
      if (!res || !res.ok) throw new Error((res && res.error) || "源列表读取失败");
      S.providers = res.providers || [];
      S.tips = res.tips || [];
      if (!S.sources.length) S.sources = res.defaults || [];
      if (!saved) S.pages = res.pages || S.pages;
      renderChips();
      renderTips();
      var id = S.providers.filter(function (p) { return p.kind === "local"; })[0];
      if (id && !S.sources.length) S.sources.push(id.id);
      applyPendingQuery();
    }).catch(function (e) {
      S.providers = [];
      renderChips();
      ctxRef.toast("读取数据源失败：" + e.message);
      applyPendingQuery();          // 源读不到也要把用户带过来的词搜出去
    });

    ctxRef.api.get("/api/search/presets").then(function (res) {
      if (res && res.ok) {
        S.presets = res.presets || [];
        renderPresets();
      }
    });
  }

  /** 从侧栏快捷搜索带进来的词：等源列表就绪再搜，否则会搜出 0 条。 */
  function applyPendingQuery() {
    if (!pendingQ) return;
    var q = pendingQ;
    pendingQ = "";
    var box = $("#sqQ");
    if (!box) return;
    box.value = q;
    S.q = q;
    run(1);
  }

  ARR.registerView({
    id: "search",
    title: "深度搜索",
    icon: "⌕",
    mount: function (host, ctx) {
      el = host;
      ctxRef = ctx;
      skeleton();
      boot();
    },
    unmount: function () {
      el = null;
      ctxRef = null;
    }
  });

  /* ------------------------------------------------------------ 快捷搜索
   *
   * 认领侧栏顶部那个搜索框。认领之后不用先进插件页：在主界面任何地方敲
   * 番名回车，就直接跳进来搜。主程序不知道「深度搜索」是什么，它只负责
   * 把词交给认领者 —— 换成别的插件认领也一样能用。
   *
   * 认领发生在脚本加载时（不是 mount）：这个框要在视图没打开时就在。
   */
  if (ARR.quickSearch) {
    ARR.quickSearch({
      id: "search",
      label: "深度搜索",
      hint: "跨源站检索历史帖子，回车跳到「深度搜索」",
      placeholder: "跨站搜番名，回车检索…",
      submit: function (q) {
        pendingQ = q;
        if (ARR.openView && ARR.openView("search")) return;
        // 视图已经挂着（openView 返回 false 说明切换没发生）：直接就地搜
        if (ctxRef) applyPendingQuery();
      }
    });
  }
})();
