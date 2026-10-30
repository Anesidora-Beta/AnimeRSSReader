/* 链接唤醒 —— 插件自带的前端视图。
 *
 * 主程序的 HTML / JS 里没有为它写过一行：后端在 bootstrap 的 views 清单里
 * 声明视图，主程序按清单加载 /plug/linkwake/view.js，脚本自己调
 * ARR.registerView 注册，侧栏就多一个入口。
 *
 * 能用的 ctx：
 *   ctx.api.get(url) / ctx.api.send(method, url, body)
 *   ctx.toast(msg) / ctx.esc(str) / ctx.on/off/emit / ctx.openItem(id)
 *
 * 交互上只守两条原则：
 *   1. **点了要有回音**。唤醒失败必须说清为什么（协议不支持 / 程序不存在 /
 *      只允许本机），不能像很多「一键打开」那样点了没反应。
 *   2. **别做成孤岛**。这个页面里的条目就是主界面那条，所以点标题能跳回去、
 *      点星是同一颗星；反过来主界面打开条目时，网盘链接也能交给客户端
 *      （靠 file 末尾订阅的 article:open）。
 *
 * 1.2.0 起它还对外提供一项能力：``ARR.provide("link.wake", …)``。别的插件
 * （深度搜索的「下载」按钮）只要把 URL 递进来，这里负责认出是哪家网盘、
 * 按用户的「优先响应」决定交给谁，需要时弹出选择。对方不需要知道本插件
 * 的 id，也不需要抄一份网盘域名表。
 */
(function () {
  "use strict";

  var API = "/api/linkwake";
  var KIND_KEY = "arr.linkwake.kind";

  var S = {
    entries: [],
    counts: {},
    providers: [],
    modes: [],
    targets: [],
    prefs: {},
    cfg: null,
    opt: {},
    kind: "dl",
    q: "",
    scanned: 0,
    loading: false,
    err: "",
    sel: {},          // 条目 id -> true，勾的是条目不是单条链接
    drawer: false,
    sheet: null,      // 正在选择的那次唤醒 {url, pid, kind, targets, ...}
    openDrawerOnMount: false
  };

  var el = null;
  var ctxRef = null;

  function $(sel, root) { return (root || el).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || el).querySelectorAll(sel));
  }
  function esc(s) { return ctxRef ? ctxRef.esc(s) : String(s == null ? "" : s); }

  function toast(msg) {
    if (ctxRef) { ctxRef.toast(msg); return; }
    if (ARR.toast) ARR.toast(msg);
  }

  // 阅读区联动发生在视图没挂载的时候（ctx 还不存在），所以这里要能退到
  // ARR 上那套同样的接口。不这么做，从主界面点进来的唤醒按钮会静默失效。
  function apiGet(url) { return (ctxRef ? ctxRef.api : ARR.api).get(url); }
  function apiSend(method, url, body) {
    return (ctxRef ? ctxRef.api : ARR.api).send(method, url, body);
  }

  function fmtAgo(ts) {
    if (!ts) return "";
    var diff = Math.floor(Date.now() / 1000) - ts;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
      "-" + String(d.getDate()).padStart(2, "0");
  }

  // 长链接只留头尾：磁力链全长都是哈希，展示整条没有意义
  function shortUrl(u, n) {
    var s = String(u || "");
    n = n || 46;
    if (s.length <= n) return s;
    return s.slice(0, Math.max(12, n - 14)) + " … " + s.slice(-10);
  }

  function kindText(k) {
    return { bt: "磁力 / 种子", pan: "网盘", file: "直链", web: "网页" }[k] || k;
  }

  function selCount() { return Object.keys(S.sel).length; }
  // ---------------------------------------------------------------- 骨架
  function skeleton() {
    el.innerHTML =
      '<div class="lw-wrap">' +
        '<div class="lw-head">' +
          '<div class="lw-title">' +
            '<b>链接唤醒</b>' +
            '<span class="lw-sub" data-el="sub">正在读取条目…</span>' +
          '</div>' +
          '<div class="lw-tools">' +
            '<input class="lw-search" data-el="q" type="search" ' +
              'placeholder="在标题与摘要里找…" autocomplete="off">' +
            '<button class="lw-btn" data-act="reload" title="重新读取">刷新</button>' +
            '<button class="lw-btn" data-act="drawer" title="配置客户端">设置</button>' +
          '</div>' +
        '</div>' +
        '<div class="lw-chips" data-el="chips"></div>' +
        '<div class="lw-body" data-el="body"></div>' +
        '<div class="lw-selbar" data-el="selbar" hidden></div>' +
        '<div class="lw-drawer" data-el="drawer" hidden></div>' +
      '</div>';
    el.addEventListener("click", onHit);
    el.addEventListener("change", onChange);
    var input = $('[data-el="q"]');
    var timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        S.q = input.value.trim();
        S.sel = {};
        load();
      }, 260);
    });
  }

  function onHit(ev) {
    var t = ev.target.closest("[data-act]");
    if (!t || !el.contains(t)) return;
    var act = t.getAttribute("data-act");
    var url = t.getAttribute("data-url") || "";
    var pid = t.getAttribute("data-pid") || "";
    var id = t.getAttribute("data-id") || "";

    if (act === "reload") { load(); return; }
    if (act === "drawer") { S.drawer = !S.drawer; renderDrawer(); return; }
    if (act === "close-drawer") { S.drawer = false; renderDrawer(); return; }
    if (act === "kind") {
      S.kind = t.getAttribute("data-kind") || "dl";
      S.sel = {};                      // 换一档就别带着看不见的选择去批量
      try { localStorage.setItem(KIND_KEY, S.kind); } catch (e) {}
      load();
      return;
    }
    if (act === "wake") {
      // 设成「每次都问我」时，这里的「唤醒」也走弹层 —— 否则用户会觉得
      // 设置没生效（页内按钮偷偷绕过它是最常见的那种「不听话」）。
      if (prefOf(t.getAttribute("data-kind") || "") === "ask") {
        requestWake({ url: url, pid: pid });
        return;
      }
      wake(url, pid, t.getAttribute("data-mode") || "");
      return;
    }
    if (act === "choose") { requestWake({ url: url, pid: pid, ask: true }); return; }
    if (act === "copy") { copyText(url); return; }
    if (act === "open-item") {
      var opener = (ctxRef && ctxRef.openItem) || ARR.openItem;
      if (opener && id) opener(id);
      return;
    }
    if (act === "star") { star(id, t); return; }
    if (act === "toggle") {
      // 只切这一行，不重渲染整个列表——重渲染会把滚动位置抹掉
      var row = t.closest(".lw-row");
      var det = row && row.querySelector(".lw-detail");
      if (!det) return;
      det.hidden = !det.hidden;
      row.classList.toggle("open", !det.hidden);
      return;
    }
    if (act === "clear-sel") { S.sel = {}; renderList(); renderSelBar(); return; }
    if (act === "batch-copy") { batch("copy"); return; }
    if (act === "batch-qb") { batch("qb"); return; }
    if (act === "batch-aria2") { batch("aria2"); return; }
    if (act === "batch-open") { batch("open"); return; }
    if (act === "probe") { probe(t.getAttribute("data-which")); return; }
    if (act === "save-cfg") { saveCfg(); return; }
  }

  function onChange(ev) {
    var t = ev.target;
    if (!t || t.type !== "checkbox") return;
    // 勾的是**条目**，不是单条链接：批量交给下载器时，一个条目下的
    // 磁力、种子、网盘本来就该一起进队列，让用户逐条勾是白费力气。
    var eid = t.getAttribute("data-eid");
    if (!eid) return;
    if (t.checked) S.sel[eid] = true;
    else delete S.sel[eid];
    renderSelBar();
  }

  // ---------------------------------------------------------------- 读取
  function load() {
    if (S.loading) return;
    S.loading = true;
    S.err = "";
    var url = API + "?kind=" + encodeURIComponent(S.kind);
    if (S.q) url += "&q=" + encodeURIComponent(S.q);
    apiGet(url).then(function (d) {
      S.loading = false;
      S.entries = d.entries || [];
      S.counts = d.counts || {};
      S.providers = d.providers || S.providers;
      S.modes = d.modes || S.modes;
      S.targets = d.targets || S.targets;
      S.prefs = d.prefs || S.prefs;
      S.cfg = d.cfg || S.cfg;
      S.opt = d.opt || S.opt;
      S.scanned = d.scanned || 0;
      renderAll();
    }).catch(function (e) {
      S.loading = false;
      S.err = "读取失败：" + (e && e.message ? e.message : e);
      renderAll();
    });
  }

  function renderAll() {
    renderStats();
    renderChips();
    renderList();
    renderSelBar();
    if (S.drawer) renderDrawer();
  }

  function renderStats() {
    var c = S.counts || {};
    var parts = [];
    if (c.items) parts.push(c.items + " 个条目");
    if (c.links) parts.push(c.links + " 条链接");
    var bits = [];
    if (c.bt) bits.push("磁力 " + c.bt);
    if (c.pan) bits.push("网盘 " + c.pan);
    if (c.file) bits.push("直链 " + c.file);
    if (c.web) bits.push("网页 " + c.web);
    if (bits.length) parts.push(bits.join(" · "));
    var sub = $('[data-el="sub"]');
    if (!parts.length) {
      sub.textContent = S.scanned ? "扫了 " + S.scanned + " 条，没找到链接" : "暂无条目";
    } else {
      sub.textContent = parts.join(" ｜ ");
    }
  }

  function renderChips() {
    var box = $('[data-el="chips"]');
    var c = S.counts || {};
    var defs = [
      ["dl", "可下载", (c.bt || 0) + (c.pan || 0) + (c.file || 0)],
      ["all", "全部", c.links || 0],
      ["bt", "磁力 / 种子", c.bt || 0],
      ["pan", "网盘", c.pan || 0],
      ["file", "直链", c.file || 0],
      ["web", "网页", c.web || 0]
    ];
    // 只在**当前**这一档显示数字：接口每次按 kind 过滤后再统计，把别的档也
    // 标上数字等于拿这一档的数量冒充那一档的，属于撒谎。
    box.innerHTML = defs.map(function (d) {
      var on = S.kind === d[0];
      var n = on && d[2] ? '<i>' + d[2] + '</i>' : "";
      return '<button class="lw-chip' + (on ? " lw-chip-on" : "") +
        '" data-act="kind" data-kind="' + d[0] + '">' + esc(d[1]) + n + '</button>';
    }).join("");
  }

  // ---------------------------------------------------------------- 列表
  function renderList() {
    var body = $('[data-el="body"]');
    if (S.err) {
      body.innerHTML = '<div class="lw-empty"><b>读取失败</b>' + esc(S.err) +
        '<br><br><button class="lw-btn" data-act="reload">重试</button></div>';
      return;
    }
    if (!S.entries.length) {
      body.innerHTML = emptyHtml();
      return;
    }
    body.innerHTML = S.entries.map(card).join("");
  }

  function emptyHtml() {
    if (S.q) {
      return '<div class="lw-empty"><b>没搜到「' + esc(S.q) + '」</b>' +
        '换个词，或者点上面的「全部」看看其它类型。</div>';
    }
    if (S.kind === "pan") {
      return '<div class="lw-empty"><b>这条时间线上没有网盘链接</b>' +
        '网盘分享本来就少。去「深度搜索」按番名找找，那个能翻更旧的帖子。</div>';
    }
    if (S.kind === "web") {
      return '<div class="lw-empty"><b>没有纯网页链接</b>' +
        '切到「全部」会看到详情页与资料站。</div>';
    }
    if (S.kind === "dl") {
      return '<div class="lw-empty"><b>没找到可下载的链接</b>' +
        '扫了 ' + S.scanned + ' 条。可以切到「全部」看看全部链接，' +
        '或者让主程序再抓一轮。</div>';
    }
    return '<div class="lw-empty"><b>什么都没有</b>换个筛选条件试试。</div>';
  }

  function card(e) {
    var groups = e.groups || [];
    var tags = groups.map(function (g) {
      var n = (g.urls || []).length;
      return '<span class="lw-tag" data-kind="' + esc(g.kind) +
        '" style="--lw-c:' + esc(g.color || "#8a8f98") + '" title="' +
        esc(g.name + " · " + n + " 条") + '">' +
        '<b>' + esc(g.badge || "?") + '</b>' +
        (n > 1 ? '<i>' + n + '</i>' : '') + '</span>';
    }).join("");
    var meta = [];
    if (e.source) meta.push(esc(e.source));
    if (e.size) meta.push(esc(e.size));
    if (e.ts) meta.push(fmtAgo(e.ts));
    if (e.starred) meta.push("★");

    return '<article class="lw-row" data-id="' + esc(e.id) + '">' +
      '<label class="lw-pick" title="选中这一条的全部下载链接">' +
        '<input type="checkbox" data-eid="' + esc(e.id) + '"' +
          (S.sel[e.id] ? " checked" : "") + '>' +
      '</label>' +
      '<div class="lw-row-main" data-act="toggle" data-id="' + esc(e.id) + '">' +
        '<div class="lw-row-text">' +
          '<a class="lw-row-title" data-act="open-item" data-id="' + esc(e.id) +
            '" title="在主界面里打开这个条目">' +
            '<i class="lw-unread"' + (e.read ? " hidden" : "") + '></i>' +
            esc(e.title || "(无标题)") + '</a>' +
          '<div class="lw-row-meta">' + meta.join('<i>·</i>') + '</div>' +
        '</div>' +
        '<div class="lw-tags">' + tags + '</div>' +
        '<button class="lw-star' + (e.starred ? " on" : "") + '" data-act="star" ' +
          'data-id="' + esc(e.id) + '" title="' +
          (e.starred ? "取消收藏" : "收藏") + '">★</button>' +
        '<span class="lw-caret"></span>' +
      '</div>' +
      '<div class="lw-detail" hidden>' +
        '<div class="lw-groups">' + groups.map(groupRow).join("") + '</div>' +
      '</div>' +
    '</article>';
  }

  function groupRow(g) {
    var urls = g.urls || [];
    var pwd = g.pwd ? '<span class="lw-pwd" title="提取码已自动补进链接">码 ' +
      esc(g.pwd) + '</span>' : "";
    var multi = urls.length > 1
      ? '<span class="lw-count">' + urls.length + ' 条</span>' : "";

    var rows = urls.map(function (u) {
      return '<div class="lw-url">' +
        '<code class="lw-url-text" title="' + esc(u) + '">' + esc(shortUrl(u, 58)) +
        '</code>' +
        '<button class="lw-mini" data-act="wake" data-url="' + esc(u) +
          '" data-pid="' + esc(g.pid) + '" data-kind="' + esc(g.kind) +
          '">唤醒</button>' +
        '<button class="lw-mini lw-mini-ghost" data-act="choose" data-url="' +
          esc(u) + '" data-pid="' + esc(g.pid) +
          '" title="这次换个方式打开">▾</button>' +
        '<button class="lw-mini lw-mini-ghost" data-act="copy" data-url="' +
          esc(u) + '">复制</button>' +
        '</div>';
    }).join("");

    return '<div class="lw-group" style="--lw-c:' + esc(g.color || "#8a8f98") +
      '" title="' + esc(kindText(g.kind) + (g.note ? " · " + g.note : "")) + '">' +
      '<div class="lw-group-head">' +
        '<span class="lw-badge">' + esc(g.badge || "?") + '</span>' +
        '<span class="lw-name">' + esc(g.name) + '</span>' +
        multi + pwd +
        '<span class="lw-spacer"></span>' +
        (urls.length > 1
          ? '<button class="lw-mini" data-act="wake" data-url="' + esc(urls[0]) +
            '" data-pid="' + esc(g.pid) + '" data-kind="' + esc(g.kind) +
            '">唤醒首个</button>'
          : "") +
      '</div>' +
      '<div class="lw-urls">' + rows + '</div>' +
    '</div>';
  }

  // 一条目真正能交给下载器的链接（网页地址不算）
  function dlUrls(e) {
    var out = [];
    (e.groups || []).forEach(function (g) {
      if (g.kind === "web") return;
      (g.urls || []).forEach(function (u) {
        out.push({ url: u, pid: g.pid, name: g.name });
      });
    });
    return out;
  }

  function selLinks() {
    var n = 0;
    Object.keys(S.sel).forEach(function (eid) {
      var e = S.entries.filter(function (x) { return x.id === eid; })[0];
      if (e) n += dlUrls(e).length;
    });
    return n;
  }

  // ---------------------------------------------------------------- 动作
  function wake(url, pid, mode) {
    var body = { url: url, pid: pid };
    if (mode) body.mode = mode;
    apiSend("POST", API + "/wake", body).then(function (r) {
      if (!r || !r.ok) { toast((r && r.error) || "唤醒失败"); return; }
      if (r.action === "copy") { copyText(r.url || url); return; }
      toast(wakeText(r));
    }).catch(function (e) { toast("唤醒失败：" + (e.message || e)); });
  }

  function wakeText(r) {
    if (!r) return "已唤醒";
    if (r.action === "app") return "已交给 " + (r.detail || "程序");
    if (r.action === "qb" || r.action === "aria2") {
      return "已加进 " + (r.detail || r.action) + " 队列";
    }
    return r.detail || "已唤醒";
  }

  // --------------------------------------------------- 优先响应 / 选择弹层
  /* 「优先响应」是这一版加的一层：用户不必理解 mode，只要回答
   * 「磁力交给谁」。点了以后先去 /prepare 问清这是什么链接、默认给谁，
   * 再决定是立刻执行还是弹一层让他当场选。
   */
  function prefOf(kind) {
    var v = (S.prefs || {})[kind];
    return v || "auto";
  }

  function fire(prep, tid, payload) {
    apiSend("POST", API + "/wake",
            { url: prep.url, target: tid, pid: prep.pid })
      .then(function (r) {
        if (!r || !r.ok) { toast((r && r.error) || "唤醒失败"); return; }
        if (r.action === "copy") { copyText(r.url || prep.url); return; }
        toast(wakeText(r));
        if (payload && typeof payload.onDone === "function") payload.onDone(r);
      }).catch(function (e) { toast("唤醒失败：" + (e.message || e)); });
  }

  /** 外部插件（或本页的行）发起的唤醒：先问后端，再执行或弹层。 */
  function requestWake(payload) {
    if (!payload || !payload.url) return;
    apiSend("POST", API + "/prepare", {
      url: payload.url,
      title: payload.title || "",
      pid: payload.pid || "",
      ask: !!payload.ask
    }).then(function (r) {
      if (!r || !r.ok) { toast((r && r.error) || "认不出这个链接"); return; }
      if (!r.canWake) {
        toast(r.reason || "唤醒只能在本机用，这里先复制链接");
        copyText(r.url);
        return;
      }
      if (r.ask) { showSheet(r, payload); return; }
      fire(r, r.default, payload);
    }).catch(function (e) { toast("唤醒失败：" + (e.message || e)); });
  }

  // 弹层挂在 body 上，不挂在视图里：它的调用方常常是别的页面，这时
  // 本插件的视图可能根本没挂载（el 是 null）。
  var sheetRoot = null;

  var TARGET_GLYPH = {
    auto: "∘", qb: "⇩", aria2: "⇩", open: "↗",
    web: "◎", copy: "⧉", ask: "?"
  };

  function ensureSheet() {
    if (sheetRoot && document.body.contains(sheetRoot)) return sheetRoot;
    sheetRoot = document.createElement("div");
    sheetRoot.className = "lw-sheet-root";
    sheetRoot.hidden = true;
    document.body.appendChild(sheetRoot);
    sheetRoot.addEventListener("click", onSheetHit);
    return sheetRoot;
  }

  function showSheet(prep, payload) {
    var root = ensureSheet();
    S.sheet = { prep: prep, payload: payload || null };
    var opts = (prep.targets || []).filter(function (t) {
      return t.id !== "ask";                 // 已经在选择了，再问一次没意义
    });
    root.innerHTML =
      '<div class="lw-sheet-mask" data-act="sheet-close"></div>' +
      '<div class="lw-sheet" role="dialog" aria-label="选择打开方式">' +
        '<div class="lw-sheet-head">' +
          '<span class="lw-badge" style="--lw-c:' + esc(prep.color || "#8a8f98") +
            '">' + esc(prep.badge || "?") + '</span>' +
          '<div class="lw-sheet-title">' +
            '<b>用什么打开？</b>' +
            '<em>' + esc(prep.name || "") + ' · ' + esc(prep.kindText || "") +
              (prep.title ? ' · ' + esc(shortUrl(prep.title, 28)) : "") + '</em>' +
          '</div>' +
          '<button class="lw-sheet-x" data-act="sheet-close" aria-label="关闭">×</button>' +
        '</div>' +
        '<div class="lw-sheet-url" title="' + esc(prep.url) + '">' +
          esc(shortUrl(prep.url, 64)) + '</div>' +
        '<div class="lw-sheet-opts">' + opts.map(function (t) {
          // ready 缺省当能用：老后端没这个字段时不该整排变灰
          var off = t.ready === false;
          var cls = "lw-opt" + (off ? " lw-opt-off" : "") +
            (t.id === prep.default ? " lw-opt-def" : "");
          return '<button class="' + cls + '" data-tid="' + esc(t.id) + '"' +
            (off ? " disabled" : "") + '>' +
            '<i class="lw-opt-ico">' + esc(TARGET_GLYPH[t.id] || "·") + '</i>' +
            '<span class="lw-opt-text"><b>' + esc(t.text) + '</b>' +
              '<em>' + esc(t.note || "") + '</em></span>' +
            (t.id === prep.default ? '<kbd>默认</kbd>' : '') +
          '</button>';
        }).join("") + '</div>' +
        '<div class="lw-sheet-foot">' +
          '<label class="lw-sheet-keep"><input type="checkbox" data-el="sheet-keep">' +
            '<span>以后' + esc(prep.kindText || "") + '都用它，不再问</span></label>' +
          '<button class="lw-btn lw-btn-ghost" data-act="sheet-settings">改默认</button>' +
        '</div>' +
      '</div>';
    root.hidden = false;
    document.body.classList.add("lw-sheet-open");
    var def = root.querySelector(".lw-opt-def");
    if (def) { try { def.focus(); } catch (e) {} }
  }

  function closeSheet() {
    S.sheet = null;
    if (sheetRoot) { sheetRoot.hidden = true; sheetRoot.innerHTML = ""; }
    document.body.classList.remove("lw-sheet-open");
  }

  function onSheetHit(ev) {
    var opt = ev.target.closest("[data-tid]");
    if (opt && !opt.disabled && S.sheet) {
      var tid = opt.getAttribute("data-tid");
      var keep = sheetRoot.querySelector('[data-el="sheet-keep"]');
      var st = S.sheet;
      closeSheet();
      if (keep && keep.checked) savePref(st.prep.kind, tid);
      fire(st.prep, tid, st.payload);
      return;
    }
    var a = ev.target.closest("[data-act]");
    if (!a) return;
    var act = a.getAttribute("data-act");
    if (act === "sheet-close") { closeSheet(); return; }
    if (act === "sheet-settings") {
      closeSheet();
      S.openDrawerOnMount = true;
      if (ARR.openView) ARR.openView("linkwake");
      if (el) { S.drawer = true; renderDrawer(); }
    }
  }

  /** 把某个类型的默认目标存起来（弹层里勾了「以后都用它」）。 */
  function savePref(kind, tid) {
    var next = {};
    for (var k in (S.prefs || {})) next[k] = S.prefs[k];
    next[kind] = tid;
    apiSend("POST", API + "/apps", { prefs: next }).then(function (r) {
      if (!r || !r.ok) { toast((r && r.error) || "没记住，下次还会问"); return; }
      S.prefs = r.cfg.prefs || next;
      S.cfg = r.cfg || S.cfg;
      toast("已记住：以后" + kindText(kind) + "直接用它");
      if (el && S.drawer) renderDrawer();
    }).catch(function () { toast("没记住，下次还会问"); });
  }

  // 收藏走主程序自己的接口，改完回主界面是同一份状态。
  // 在下载页顺手标一下，比切回去找星标再切回来顺手得多。
  function star(id, btn) {
    var e = S.entries.filter(function (x) { return x.id === id; })[0];
    if (!e) return;
    var next = !e.starred;
    function paint(on) {
      e.starred = on;
      btn.classList.toggle("on", on);
      btn.title = on ? "取消收藏" : "收藏";
    }
    paint(next);
    apiSend("POST", "/api/star", { id: id, starred: next }).then(function (r) {
      if (r && r.ok === false) { paint(!next); toast(r.error || "收藏失败"); return; }
      toast(next ? "已收藏" : "已取消收藏");
    }).catch(function (err) {
      paint(!next);
      toast("收藏失败：" + (err && err.message ? err.message : err));
    });
  }

  function copyText(text) {
    function done() { toast("已复制"); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); }
      catch (e) { toast("复制失败，请手动选中"); }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  function batch(action) {
    var ids = Object.keys(S.sel);
    if (!ids.length) { toast("先勾选几个条目"); return; }
    var urls = [];
    ids.forEach(function (eid) {
      var e = S.entries.filter(function (x) { return x.id === eid; })[0];
      if (e) dlUrls(e).forEach(function (x) { urls.push(x.url); });
    });
    if (!urls.length) {
      toast("勾选的条目只有网页地址，没有能下载的链接");
      return;
    }
    if (action === "copy") {
      // 复制在前端做，省一次往返，也不把长文本塞进接口
      copyText(urls.join("\n"));
      toast("已复制 " + urls.length + " 条");
      return;
    }
    var label = { qb: "qBittorrent", aria2: "aria2", open: "默认程序" }[action];
    toast("正在交给 " + label + "…");
    apiSend("POST", API + "/batch", { action: action, urls: urls })
      .then(function (r) {
        if (!r || !r.ok) { toast((r && r.error) || "失败了"); return; }
        var msg = "已提交 " + (r.count || 0) + " 条给 " + label;
        if (r.failed && r.failed.length) msg += "，" + r.failed.length + " 条失败";
        toast(msg);
      }).catch(function (e) { toast("失败：" + (e.message || e)); });
  }

  function renderSelBar() {
    var bar = $('[data-el="selbar"]');
    var n = selCount();
    if (!n) { bar.hidden = true; bar.innerHTML = ""; return; }
    bar.hidden = false;
    var m = selLinks();
    bar.innerHTML =
      '<span class="lw-sel-n">已选 <b>' + n + '</b> 个条目 · <b>' + m + '</b> 条链接</span>' +
      '<button class="lw-btn" data-act="batch-copy">复制</button>' +
      '<button class="lw-btn" data-act="batch-qb">发到 qB</button>' +
      '<button class="lw-btn" data-act="batch-aria2">发到 aria2</button>' +
      '<button class="lw-btn" data-act="batch-open">逐个唤醒</button>' +
      '<button class="lw-btn lw-btn-ghost" data-act="clear-sel">清空</button>';
  }

  // ---------------------------------------------------------------- 设置
  function providerCfg(pid) {
    var apps = (S.cfg && S.cfg.apps) || {};
    return apps[pid] || { mode: "web", scheme: "", exe: "", args: "{url}" };
  }

  function renderDrawer() {
    var d = $('[data-el="drawer"]');
    if (!S.drawer) { d.hidden = true; d.innerHTML = ""; return; }
    d.hidden = false;
    var cfg = S.cfg || {};
    var qb = cfg.qb || {};
    var ar = cfg.aria2 || {};
    var modeOpts = (S.modes || []).map(function (m) {
      return '<option value="' + esc(m.id) + '">' + esc(m.text) + '</option>';
    }).join("");

    // 「优先响应」：四种类型各一行。选项按类型过滤 —— 网盘不可能交给 qB，
    // 列出来只会让人以为点了会有用。
    var prefs = S.prefs || {};
    var prefRows = [
      ["bt", "磁力 / 种子", "magnet:?、.torrent、thunder://"],
      ["pan", "网盘", "百度 / 阿里 / 夸克 / 115 …"],
      ["file", "直链", "http 上的 mkv / zip 等"],
      ["web", "网页", "资料站、群链接"]
    ].map(function (k) {
      var cur = prefs[k[0]] || "auto";
      var opts = (S.targets || []).filter(function (t) {
        return (t.kinds || []).indexOf(k[0]) >= 0;
      }).map(function (t) {
        var miss = "";
        if (t.id === "qb" && !(qb.url || "")) miss = "（未配置）";
        if (t.id === "aria2" && !(ar.url || "")) miss = "（未配置）";
        return '<option value="' + esc(t.id) + '"' +
          (t.id === cur ? " selected" : "") + '>' +
          esc(t.text) + miss + '</option>';
      }).join("");
      return '<label class="lw-pref" title="' + esc(k[2]) + '">' +
        '<span>' + esc(k[1]) + '</span>' +
        '<select class="lw-input" data-pref="' + k[0] + '">' + opts + '</select>' +
        '</label>';
    }).join("");

    var rows = (S.providers || []).map(function (p) {
      var c = providerCfg(p.id);
      var needApp = c.mode === "app";
      var needScheme = c.mode === "scheme";
      return '<tr data-pid="' + esc(p.id) + '">' +
        '<td><span class="lw-badge" style="--lw-c:' + esc(p.color) + '">' +
          esc(p.badge) + '</span> ' + esc(p.name) + '</td>' +
        '<td><select class="lw-input lw-mode" data-k="mode">' + modeOpts + '</select></td>' +
        '<td><input class="lw-input lw-exe" data-k="exe" ' +
          'placeholder="C:\\Tools\\qbittorrent.exe"' +
          (needApp ? "" : " disabled") + ' value="' + esc(c.exe || "") + '"></td>' +
        '<td><input class="lw-input lw-args" data-k="args" placeholder="{url}"' +
          (needApp ? "" : " disabled") + ' value="' + esc(c.args || "{url}") + '"></td>' +
        '<td><input class="lw-input lw-scheme" data-k="scheme" placeholder="baiduyun"' +
          (needScheme ? "" : " disabled") + ' value="' + esc(c.scheme || "") + '"></td>' +
      '</tr>';
    }).join("");

    d.innerHTML =
      '<div class="lw-drawer-mask" data-act="close-drawer"></div>' +
      '<div class="lw-drawer-panel">' +
        '<header class="lw-drawer-head">' +
          '<b>客户端设置</b>' +
          '<button class="lw-btn lw-btn-ghost" data-act="close-drawer">关闭</button>' +
        '</header>' +
        '<div class="lw-drawer-body">' +
          '<section class="lw-sec">' +
            '<h4>优先响应（点链接时先找谁）</h4>' +
            '<p class="lw-note">深度搜索的「下载」、阅读区里的「打开网盘」都会照这里走。' +
            '选「每次都问我」时，点一下会先弹一层让你当场挑。' +
            '「自动」表示按下面「每种链接交给谁」的逐条设置。</p>' +
            '<div class="lw-prefs">' + prefRows + '</div>' +
          '</section>' +
          '<section class="lw-sec">' +
            '<h4>下载器（唯一能确认「真的加进去了」的方式）</h4>' +
            '<div class="lw-form">' +
              '<label>qBittorrent 地址<input class="lw-input" data-sec="qb" data-k="url" ' +
                'placeholder="http://127.0.0.1:8080" value="' + esc(qb.url || "") + '"></label>' +
              '<label>用户名<input class="lw-input" data-sec="qb" data-k="user" value="' +
                esc(qb.user || "admin") + '"></label>' +
              '<label>密码<input class="lw-input" data-sec="qb" data-k="password" type="password" ' +
                'placeholder="' + (qb.passwordSet ? "已设置，留空不改" : "没设就留空") + '"></label>' +
              '<label>保存目录<input class="lw-input" data-sec="qb" data-k="savepath" ' +
                'placeholder="留空用 qB 默认" value="' + esc(qb.savepath || "") + '"></label>' +
              '<div class="lw-form-act"><button class="lw-btn" data-act="probe" ' +
                'data-which="qb">测试 qB</button></div>' +
            '</div>' +
            '<div class="lw-form">' +
              '<label>aria2 RPC 地址<input class="lw-input" data-sec="aria2" data-k="url" ' +
                'placeholder="http://127.0.0.1:6800/jsonrpc" value="' + esc(ar.url || "") + '"></label>' +
              '<label>RPC 令牌<input class="lw-input" data-sec="aria2" data-k="token" type="password" ' +
                'placeholder="' + (ar.tokenSet ? "已设置，留空不改" : "没设就留空") + '"></label>' +
              '<label>保存目录<input class="lw-input" data-sec="aria2" data-k="dir" ' +
                'placeholder="留空用默认" value="' + esc(ar.dir || "") + '"></label>' +
              '<div class="lw-form-act"><button class="lw-btn" data-act="probe" ' +
                'data-which="aria2">测试 aria2</button></div>' +
            '</div>' +
          '</section>' +
          '<section class="lw-sec">' +
            '<h4>每种链接交给谁</h4>' +
            '<p class="lw-note">「系统默认程序」最省事：磁力会交给系统关联的 BT 客户端。' +
            '「浏览器打开」适合网盘。「指定程序」填 exe 路径最可靠，' +
            '{url} 会被替换成链接本身。</p>' +
            '<table class="lw-table"><thead><tr>' +
              '<th>类型</th><th>方式</th><th>程序路径</th><th>参数</th><th>协议名</th>' +
            '</tr></thead><tbody>' + rows + '</tbody></table>' +
          '</section>' +
          '<div class="lw-drawer-foot">' +
            '<button class="lw-btn lw-btn-main" data-act="save-cfg">保存</button>' +
            '<span class="lw-hint">保存后立即生效，不用重启</span>' +
          '</div>' +
        '</div>' +
      '</div>';

    // select 的值要在插入 DOM 之后回填（option 是拼进去的）
    $$("tr[data-pid]", d).forEach(function (tr) {
      var c = providerCfg(tr.getAttribute("data-pid"));
      var sel = $(".lw-mode", tr);
      if (sel) sel.value = c.mode || "web";
    });
    // 抽屉容器是复用的，renderDrawer 每次都会被调（刷新一次就一次）。
    // 事件只能挂一次，否则监听器越攒越多。
    if (!d._lwBound) {
      d.addEventListener("change", onDrawerChange);
      d._lwBound = true;
    }
  }

  function onDrawerChange(ev) {
    var t = ev.target;
    if (!t.classList || !t.classList.contains("lw-mode")) return;
    var tr = t.closest("tr[data-pid]");
    if (!tr) return;
    $(".lw-exe", tr).disabled = t.value !== "app";
    $(".lw-args", tr).disabled = t.value !== "app";
    $(".lw-scheme", tr).disabled = t.value !== "scheme";
  }

  function saveCfg() {
    var d = $('[data-el="drawer"]');
    var apps = {};
    $$("tr[data-pid]", d).forEach(function (tr) {
      apps[tr.getAttribute("data-pid")] = {
        mode: $(".lw-mode", tr).value,
        exe: $(".lw-exe", tr).value.trim(),
        args: $(".lw-args", tr).value.trim() || "{url}",
        scheme: $(".lw-scheme", tr).value.trim()
      };
    });
    var body = { apps: apps, qb: {}, aria2: {}, prefs: {} };
    $$("[data-pref]", d).forEach(function (sel) {
      body.prefs[sel.getAttribute("data-pref")] = sel.value;
    });
    $$("[data-sec]", d).forEach(function (inp) {
      body[inp.getAttribute("data-sec")][inp.getAttribute("data-k")] = inp.value;
    });
    apiSend("POST", API + "/apps", body).then(function (r) {
      if (!r || !r.ok) { toast((r && r.error) || "保存失败"); return; }
      S.cfg = r.cfg;
      S.prefs = (r.cfg && r.cfg.prefs) || S.prefs;
      toast("已保存");
      renderDrawer();
    }).catch(function (e) { toast("保存失败：" + (e.message || e)); });
  }

  function probe(which) {
    toast("正在测试…");
    apiSend("POST", API + "/probe", { which: which }).then(function (r) {
      if (!r || !r.ok) { toast((r && r.error) || "连不上"); return; }
      toast(r.detail || "已连通");
    }).catch(function (e) { toast("测试失败：" + (e.message || e)); });
  }

  // ---------------------------------------------------------------- 注册
  ARR.registerView({
    id: "linkwake",
    title: "链接唤醒",
    icon: "⇱",
    mount: function (host, ctx) {
      el = host;
      ctxRef = ctx;
      try {
        var saved = localStorage.getItem(KIND_KEY);
        if (saved) S.kind = saved;
      } catch (e) {}
      if (S.openDrawerOnMount) { S.drawer = true; S.openDrawerOnMount = false; }
      skeleton();
      renderAll();
      load();
    },
    unmount: function () {
      el = null;
      ctxRef = null;
      S.sel = {};
    }
  });

  /* ---------------------------------------------------------------- 对外能力
   *
   * 深度搜索的「下载」按钮不知道网盘域名表，也不该知道本插件的 id。它只要
   * ARR.invoke("link.wake", {url, title}) —— 剩下全在这里：认出是哪家、查
   * 用户的「优先响应」、该弹层就弹层。
   *
   * 这个能力在脚本加载时就注册，不依赖视图挂载：从阅读区、搜索结果里点过来
   * 时，本页面通常根本没打开。
   */
  if (ARR.provide) {
    ARR.provide("link.wake", function (p) {
      if (!p || !p.url) return false;
      requestWake(p);
      return true;
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && S.sheet) { closeSheet(); }
  });

  /* ---------------------------------------------------------------- 联动
   *
   * 主程序打开条目时会 emit("article:open")，我们在这里做两件主界面做不了的事：
   *   1. 给正文里那堆裸链接标上是哪家网盘 —— 主程序不认识这些域名
   *   2. 给网盘组加「打开」按钮 —— 主程序只会用浏览器打开，提取码白补了
   *
   * 监听在脚本加载时就挂上，不随视图挂载/卸载。否则用户一从「链接唤醒」
   * 切回列表，阅读区里刚加的按钮就没了。
   */
  var articleSeq = 0;

  ARR.on("article:open", function (it) {
    if (!it || !it.id) return;
    var mine = ++articleSeq;
    var acts = document.getElementById("artActions");
    if (!acts) return;

    apiGet(API + "/item/" + encodeURIComponent(it.id)).then(function (r) {
      if (mine !== articleSeq) return;        // 已经翻到下一条了，这批结果作废
      if (!r || !r.ok || !r.entry) return;
      paintArticle(acts, document.getElementById("artLinks"), r.entry.groups || []);
    }).catch(function () { /* 这条没链接，安静地什么也不加 */ });
  });

  function paintArticle(acts, linksBox, groups) {
    // 正文链接加提供商角标：一堆裸 URL 里，哪个是网盘、哪家网盘，一眼看出来
    if (linksBox) {
      var byUrl = {};
      groups.forEach(function (g) {
        if (g.kind === "web") return;          // 详情页标个角标没有意义
        (g.urls || []).forEach(function (u) { byUrl[u] = g; });
      });
      Array.prototype.forEach.call(linksBox.querySelectorAll("a[href]"), function (a) {
        var g = byUrl[a.getAttribute("href")];
        if (!g || a.querySelector(".lw-pin")) return;
        var pin = document.createElement("em");
        pin.className = "lw-pin";
        pin.style.setProperty("--lw-c", g.color || "#8a8f98");
        pin.textContent = g.badge || "?";
        a.insertBefore(pin, a.firstChild);
        a.title = g.name + (g.pwd ? "（提取码 " + g.pwd + "）" : "");
      });
    }

    var pans = groups.filter(function (g) { return g.kind === "pan"; });
    if (!pans.length) return;
    pans.slice(0, 3).forEach(function (g) {
      var url = (g.urls || [])[0];
      if (!url) return;
      var b = document.createElement("button");
      b.className = "line-btn";
      b.setAttribute("data-lw-wake", url);
      b.setAttribute("data-lw-pid", g.pid);
      b.title = g.name + (g.pwd ? "（提取码 " + g.pwd + "）" : "");
      b.textContent = "打开 " + g.name;
      acts.appendChild(b);
    });
  }

  // 阅读区不是我们的地盘，没法就地绑定，走文档级委托。
  // 走 requestWake 而不是直接 wake：这样「每次都问我」对阅读区同样生效。
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-lw-wake]");
    if (!b) return;
    requestWake({ url: b.getAttribute("data-lw-wake"),
                  pid: b.getAttribute("data-lw-pid") });
  });
})();
