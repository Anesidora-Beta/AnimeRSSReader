/* ============================================================
   动漫资源 · RSS 阅读器  —  前端逻辑
   原生 JS，无构建、无依赖
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var SITE_NAMES = {};
  var PAGE = 60;

  // 无限滚动的哨兵：跟着列表一起重绘，所以用 JS 建、每次渲染时重新挂到末尾
  var sentinel = document.createElement("div");
  sentinel.className = "load-more";
  sentinel.innerHTML = "<span>加载更多…</span>";
  sentinel.hidden = true;

  var state = {
    boot: null,
    feeds: [],
    watches: [],
    editingWatch: null,
    counts: { total: 0, unread: 0, starred: 0, feeds: {}, watches: {} },
    view: "items",                                           // items | schedule
    filter: localStorage.getItem("arr.filter") || "all",     // all | unread | starred
    feed: localStorage.getItem("arr.feed") || "all",
    q: "",
    items: [],
    offset: 0,
    hasMore: false,
    currentId: null,
    loading: false,
    seq: 0,
    pollTimer: null,
    theme: localStorage.getItem("arr.theme") || "auto",
    feedItems: null
  };

  /* ------------------------------------------------------------ 工具 */

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function get(url) {
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); });
  }

  function send(method, url, body) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null
    }).then(function (r) { return r.json(); });
  }

  function timeAgo(ts) {
    if (!ts) return "";
    var d = Math.floor(Date.now() / 1000 - ts);
    if (d < 0) d = 0;
    if (d < 60) return "刚刚";
    if (d < 3600) return Math.floor(d / 60) + " 分钟前";
    if (d < 86400) return Math.floor(d / 3600) + " 小时前";
    if (d < 86400 * 8) return Math.floor(d / 86400) + " 天前";
    var dt = new Date(ts * 1000);
    return (dt.getMonth() + 1) + "月" + dt.getDate() + "日";
  }

  function fullTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  var toastTimer = null;
  function toast(msg) {
    var el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 1900);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (res, rej) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); res(); } catch (e) { rej(e); }
      document.body.removeChild(ta);
    });
  }

  function badgeClass(src) {
    return ["dmhy", "animegarden", "mikan", "acgrip", "nyaa", "bangumi"].indexOf(src) >= 0
      ? "b-" + src : "b-custom";
  }

  /* ------------------------------------------------------------ 主题 */

  function applyTheme(t) {
    state.theme = t;
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("arr.theme", t);
    $("#themeLabel").textContent = t === "auto" ? "自动" : (t === "dark" ? "深色" : "浅色");
  }

  /* ------------------------------------------------------------ 侧栏 */

  function renderSidebar() {
    var builtin = state.feeds.filter(function (f) { return f.kind === "builtin"; });
    var custom = state.feeds.filter(function (f) { return f.kind === "custom"; });
    var c = state.counts.feeds || {};

    $("#srcCount").textContent = builtin.length;

    $("#feedBuiltin").innerHTML = builtin.map(function (f) {
      var n = (c[f.id] && c[f.id].unread) || 0;
      return feedRow(f, n);
    }).join("");

    $("#feedCustom").innerHTML = custom.map(function (f) {
      var n = (c[f.id] && c[f.id].unread) || 0;
      return feedRow(f, n, true);
    }).join("");
    $("#customEmpty").hidden = custom.length > 0;

    renderWatches();

    $$(".feed-item").forEach(function (el) {
      el.classList.toggle("active", state.feed === el.dataset.feed);
      el.classList.toggle("off", el.dataset.enabled === "0");
    });
    syncNav();
  }

  function renderWatches() {
    var wc = state.counts.watches || {};
    var list = state.watches || [];
    $("#watchList").innerHTML = list.map(function (w) {
      var n = (wc[w.id] && wc[w.id].unread) || 0;
      var total = (wc[w.id] && wc[w.id].total) || w.total || 0;
      var title = w.include ? "关键词：" + w.include : "";
      if (w.exclude) title += "　排除：" + w.exclude;
      if (total) title += "　命中 " + total + " 条";
      return '<button class="feed-item watch-item' + (w.enabled ? "" : " off") +
        '" data-feed="watch:' + esc(w.id) + '" data-enabled="' + (w.enabled ? "1" : "0") +
        '" title="' + esc(title) + '">' +
        '<span class="feed-dot watch-dot"></span>' +
        '<span class="feed-name">' + esc(w.name) + "</span>" +
        (n ? '<span class="feed-badge">' + n + "</span>" : "") +
        '<span class="feed-edit" data-editwatch="' + esc(w.id) + '" title="编辑">✎</span>' +
        '<span class="feed-del" data-delwatch="' + esc(w.id) + '" title="删除">×</span>' +
        "</button>";
    }).join("");
    $("#watchEmpty").hidden = list.length > 0;
  }

  function currentWatch() {
    if (!state.feed || state.feed.indexOf("watch:") !== 0) return null;
    var wid = state.feed.slice(6);
    return (state.watches || []).filter(function (w) { return w.id === wid; })[0] || null;
  }

  function feedRow(f, unread, deletable) {
    var color = {
      dmhy: "#2f6fed", mikan: "#e8763a", acgrip: "#7c5cf0",
      nyaa: "#22a06b", bangumi: "#e0538c"
    }[f.id] || "var(--fg-3)";
    var title = esc(f.title);
    var err = f.lastError ? ' title="抓取失败：' + esc(f.lastError) + '"' : "";
    return '<button class="feed-item' + (f.lastError ? " has-err" : "") + '" data-feed="' + esc(f.id) +
      '" data-enabled="' + (f.enabled ? "1" : "0") + '"' + err + '>' +
      '<span class="feed-dot" style="background:' + color + '"></span>' +
      '<span class="feed-name">' + title + (f.lastError ? " ⚠" : "") + "</span>" +
      (unread ? '<span class="feed-badge">' + unread + "</span>" : "") +
      (deletable ? '<span class="feed-del" data-del="' + esc(f.id) + '" title="删除">×</span>' : "") +
      "</button>";
  }

  function syncNav() {
    $$(".nav-item").forEach(function (el) {
      var nav = el.dataset.nav;
      var active;
      if (nav === "schedule") active = state.view === "schedule";
      else if (nav.indexOf("plugin:") === 0) active = state.view === nav;
      else active = state.view === "items" && state.feed === "all" && state.filter === nav;
      el.classList.toggle("active", active);
    });
    $$(".feed-item").forEach(function (el) {
      el.classList.toggle("active", state.view === "items" && state.feed === el.dataset.feed);
    });
  }

  function updateCounts() {
    var c = state.counts || {};
    $("#cUnread").textContent = c.unread || 0;
    $("#cAll").textContent = c.total || 0;
    $("#cStar").textContent = c.starred || 0;
    if (SCH.data && SCH.data.stats) {
      $("#cToday").textContent = SCH.data.stats.today || 0;
    } else if (state.boot && state.boot.schedule) {
      $("#cToday").textContent = state.boot.schedule.today || 0;
    }
    var per = c.feeds || {};
    var wper = c.watches || {};
    $$(".feed-item").forEach(function (el) {
      var fid = el.dataset.feed || "";
      var n = 0;
      if (fid.indexOf("watch:") === 0) {
        var w = wper[fid.slice(6)];
        n = (w && w.unread) || 0;
      } else {
        n = (per[fid] && per[fid].unread) || 0;
      }
      var badge = el.querySelector(".feed-badge");
      if (n > 0) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "feed-badge";
          el.insertBefore(badge, el.querySelector(".feed-del"));
        }
        badge.textContent = n;
      } else if (badge) {
        badge.remove();
      }
    });
  }

  function currentFeed() {
    return state.feeds.filter(function (f) { return f.id === state.feed; })[0] || null;
  }

  function channelTitle() {
    var w = currentWatch();
    if (w) return w.name;
    if (state.feed !== "all") {
      var f = currentFeed();
      return f ? f.title : "全部条目";
    }
    return state.filter === "unread" ? "未读"
      : state.filter === "starred" ? "收藏" : "全部条目";
  }

  /* ------------------------------------------------------------ 列表 */

  function skeleton(n) {
    var h = "";
    for (var i = 0; i < n; i++) h += '<div class="skel"><i></i><i></i></div>';
    $("#listBody").innerHTML = h;
  }

  function loadItems(reset) {
    var seq = ++state.seq;
    if (reset) {
      state.offset = 0;
      state.hasMore = true;
      if (!state.items.length) skeleton(7);
    }
    state.loading = true;

    var p = new URLSearchParams({
      feed: state.feed, filter: state.filter, q: state.q,
      limit: PAGE, offset: state.offset
    });

    get("/api/items?" + p.toString()).then(function (res) {
      if (seq !== state.seq) return;
      state.loading = false;
      if (!res.ok) { toast(res.error || "加载失败"); return; }

      if (reset) state.items = [];
      state.items = state.items.concat(res.items || []);
      state.offset = state.items.length;
      state.hasMore = !!res.hasMore;
      state.counts = res.counts || state.counts;

      renderList();
      updateCounts();
      updateChannelHead();
    }).catch(function (e) {
      if (seq === state.seq) {
        state.loading = false;
        $("#listBody").innerHTML =
          '<div class="list-empty"><b>加载失败</b>' + esc(e.message) +
          '<br><br><button class="line-btn" data-action="reload">重试</button></div>';
      }
    });
  }

  function updateChannelHead() {
    var title = channelTitle();
    $("#channelTitle").textContent = title;
    $("#mobileTitle").textContent = title;

    var bits = [];
    var w = currentWatch();
    var f = currentFeed();
    if (w) {
      if (w.include) bits.push("关注 " + w.include);
      if (w.remainDays) bits.push("保留 " + w.remainDays + " 天");
    }
    if (state.filter === "unread") bits.push("仅未读");
    if (state.filter === "starred") bits.push("仅收藏");
    if (f && f.kind === "custom") bits.push("自定义订阅");
    if (f && f.lastOk) bits.push("更新于 " + timeAgo(f.lastOk));
    else if (f && f.lastError) bits.push("上次抓取失败");
    bits.push(state.items.length + " 条");
    $("#channelSub").textContent = bits.join(" · ");
  }

  function renderList() {
    var body = $("#listBody");
    if (!state.items.length) {
      if (state.q) {
        body.innerHTML = '<div class="list-empty"><b>没有匹配的条目</b>换个关键词试试，或清空搜索。</div>';
      } else if (currentWatch()) {
        body.innerHTML = '<div class="list-empty"><b>这个关注还没匹配到条目</b>' +
          '刷新一轮试试，或把关键词放宽一点（多个词是「全部都要包含」的关系）。</div>';
      } else {
        var empty = state.counts.total === 0;
        body.innerHTML = empty
          ? '<div class="list-empty"><b>还没有抓到内容</b>点下面的按钮抓一轮，或按 ' +
            '<kbd>R</kbd> 刷新。<br><br>' +
            '<button class="primary-btn" data-action="refresh">开始抓取</button></div>'
          : '<div class="list-empty"><b>这里暂时是空的</b>' +
            (state.filter === "unread" ? "所有条目都读完了。" :
              state.filter === "starred" ? "点条目标题旁的 ☆ 收藏会出现在这里。" :
                "换个频道或清空筛选看看。") + "</div>";
      }
      sentinel.hidden = true;
      body.appendChild(sentinel);
      return;
    }

    body.innerHTML = state.items.map(function (it) {
      var cls = "row" + (it.read ? " read" : "") + (it.id === state.currentId ? " active" : "");
      var size = it.size ? '<span>' + esc(it.size) + "</span>" : "";
      var seed = (it.seeders != null && it.seeders > 0)
        ? '<span class="tag">做种 ' + it.seeders + "</span>" : "";
      var mag = it.magnet ? '<span class="tag mag">磁力</span>'
        : (it.torrent ? '<span class="tag">种子</span>' : "");
      return '<article class="' + cls + '" data-id="' + esc(it.id) + '" tabindex="0">' +
        '<span class="row-dot"><i></i></span>' +
        '<div class="row-main">' +
        '<div class="row-title">' + esc(it.title) + "</div>" +
        '<div class="row-meta">' +
        '<span class="badge ' + badgeClass(it.source) + '">' + esc(it.sourceName || it.source) + "</span>" +
        size + mag + seed +
        '<span class="sep">·</span><span>' + timeAgo(it.ts) + "</span>" +
        (it.starred ? '<span title="已收藏">★</span>' : "") +
        "</div></div></article>";
    }).join("");

    sentinel.hidden = !state.hasMore;
    body.appendChild(sentinel);
  }

  function updateRowState(it) {
    var el = $('.row[data-id="' + CSS.escape(it.id) + '"]');
    if (!el) return;
    el.classList.toggle("read", !!it.read);
    var star = el.querySelector('[title="已收藏"]');
    if (it.starred && !star) {
      var s = document.createElement("span");
      s.title = "已收藏";
      s.textContent = "★";
      el.querySelector(".row-meta").appendChild(s);
    } else if (!it.starred && star) {
      star.remove();
    }
  }

  /* ------------------------------------------------------------ 阅读区 */

  function selectItem(id, opts) {
    opts = opts || {};
    var it = state.items.filter(function (x) { return x.id === id; })[0];
    if (!it) {
      get("/api/item/" + encodeURIComponent(id)).then(function (r) {
        if (r.ok) { renderArticle(r.item); openReader(); }
      });
      return;
    }
    renderArticle(it);
    if (!opts.silent && SET.values.autoRead !== false) markRead(it, true);
    openReader();
    var row = $('.row[data-id="' + CSS.escape(id) + '"]');
    if (row && opts.scroll !== false) row.scrollIntoView({ block: "nearest" });
  }

  function renderArticle(it) {
    state.currentId = it.id;
    $$(".row").forEach(function (el) {
      el.classList.toggle("active", el.dataset.id === it.id);
    });

    $("#welcome").hidden = true;
    $("#article").hidden = false;
    $("#readerScroll").scrollTop = 0;

    $("#readerCrumb").textContent = currentFeed() && state.feed !== "all"
      ? currentFeed().title : (it.sourceName || it.source || "");

    $("#artTitle").textContent = it.title;

    var meta = [
      '<span class="badge ' + badgeClass(it.source) + '">' + esc(it.sourceName || it.source) + "</span>"
    ];
    if (it.category) meta.push("<span>" + esc(it.category) + "</span>");
    if (it.size) meta.push("<span>" + esc(it.size) + "</span>");
    if (it.seeders != null) {
      meta.push('<span class="seed">做种 ' + it.seeders + "</span>");
    }
    if (it.author) meta.push("<span>" + esc(it.author) + "</span>");
    if (it.ts) meta.push('<span class="dot">·</span><span title="' +
      fullTime(it.ts) + '">' + timeAgo(it.ts) + "</span>");
    $("#artMeta").innerHTML = meta.join("");

    var acts = [];
    if (it.magnet) {
      acts.push('<button class="primary-btn" data-act="copy-magnet">' +
        ico("copy") + "复制磁力</button>");
      acts.push('<a class="line-btn" href="' + esc(it.magnet) + '">' +
        ico("magnet") + "<span>打开磁力</span></a>");
    }
    if (it.torrent) {
      acts.push('<a class="line-btn ok" href="' + esc(it.torrent) +
        '" target="_blank" rel="noopener">' + ico("download") + "<span>下载种子</span></a>");
    }
    if (it.link) {
      acts.push('<a class="line-btn" href="' + esc(it.link) +
        '" target="_blank" rel="noopener">' + ico("link") + "<span>帖子页</span></a>");
    }
    $("#artActions").innerHTML = acts.join("");

    $("#artSummary").textContent = it.summary || "";

    var links = it.links || [];
    if (links.length) {
      $("#artLinks").innerHTML = "<h4>正文链接 · " + links.length + "</h4>" +
        links.map(function (u) {
          return '<a class="link-item" href="' + esc(u) + '" target="_blank" rel="noopener">' +
            ico("arrow") + "<span>" + esc(u.replace(/^https?:\/\//, "")) + "</span></a>";
        }).join("");
      $("#artLinks").hidden = false;
    } else {
      $("#artLinks").hidden = true;
      $("#artLinks").innerHTML = "";
    }

    if (it.magnet) {
      $("#artMagnetBox").hidden = false;
      $("#artMagnet").textContent = it.magnet;
      $("#artMagnetBox").open = false;
    } else {
      $("#artMagnetBox").hidden = true;
    }

    var foot = [];
    if (it.infohash) foot.push("<span>btih " + esc(it.infohash.slice(0, 12)) + "…</span>");
    foot.push("<span>来源 " + esc(it.sourceName || it.source) + "</span>");
    if (it.added) foot.push("<span>抓取于 " + fullTime(it.added) + "</span>");
    $("#artFoot").innerHTML = foot.join("");

    syncReaderTools(it);
  }

  function syncReaderTools(it) {
    $("#btnStar").classList.toggle("on", !!(it && it.starred));
    $("#btnStar").title = it && it.starred ? "取消收藏（S）" : "收藏（S）";
    $("#btnOpenPage").disabled = !(it && it.link);
  }

  function markRead(it, read) {
    if (!it || it.read === read) return;
    it.read = read;
    updateRowState(it);
    send("POST", "/api/read", { ids: [it.id], read: read }).then(function (r) {
      if (r && r.counts) { state.counts = r.counts; updateCounts(); }
    });
  }

  function toggleStar(id) {
    var it = state.items.filter(function (x) { return x.id === id; })[0];
    if (!it) return;
    var next = !it.starred;
    it.starred = next;
    updateRowState(it);
    syncReaderTools(it);
    send("POST", "/api/star", { id: id, starred: next }).then(function (r) {
      if (r && r.counts) { state.counts = r.counts; updateCounts(); }
      toast(next ? "已收藏" : "已取消收藏");
    });
  }

  function step(dir) {
    if (!state.items.length) return;
    var i = -1;
    for (var k = 0; k < state.items.length; k++) {
      if (state.items[k].id === state.currentId) { i = k; break; }
    }
    var n = i + dir;
    if (n < 0) n = 0;
    if (n >= state.items.length) {
      if (state.hasMore) loadItems(false);
      return;
    }
    selectItem(state.items[n].id);
  }

  /* ------------------------------------------------------------ 图标 */

  var ICONS = {
    copy: '<path d="M9 9V6.5A1.5 1.5 0 0 1 10.5 5h7A1.5 1.5 0 0 1 19 6.5v7a1.5 1.5 0 0 1-1.5 1.5H15M6.5 9h7A1.5 1.5 0 0 1 15 10.5v7A1.5 1.5 0 0 1 13.5 19h-7A1.5 1.5 0 0 1 5 17.5v-7A1.5 1.5 0 0 1 6.5 9Z" stroke="currentColor" stroke-width="1.6"/>',
    magnet: '<path d="M7 4v9a5 5 0 0 0 10 0V4M7 9h4M13 9h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    download: '<path d="M12 4v9m0 0 3.2-3.2M12 13 8.8 9.8M5 16.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-1.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    link: '<path d="M10.5 13.5a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1 1M13.5 10.5a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1-1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    arrow: '<path d="M8 16 16 8M9 8h7v7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
  };
  function ico(name) {
    return '<svg viewBox="0 0 24 24" fill="none">' + (ICONS[name] || "") + "</svg>";
  }

  /* ------------------------------------------------------------ 刷新 */

  function setRefreshBar(s) {
    var bar = $(".refresh-bar");
    var fill = $("#refreshFill");
    var txt = $("#refreshText");
    var btns = [$("#btnRefresh"), $("#btnRefreshM")];

    if (s && s.running) {
      bar.classList.add("busy");
      btns.forEach(function (b) { b.classList.add("busy"); });
      var pct = s.total ? Math.round(s.done / s.total * 100) : 6;
      fill.style.width = Math.max(pct, 6) + "%";
      txt.textContent = (s.message || "抓取中…") + (s.total ? " " + s.done + "/" + s.total : "");
      txt.title = txt.textContent;
    } else {
      bar.classList.remove("busy");
      btns.forEach(function (b) { b.classList.remove("busy"); });
      fill.style.width = "0%";
      var last = 0;
      state.feeds.forEach(function (f) { if (f.lastOk > last) last = f.lastOk; });
      txt.textContent = last ? "更新于 " + timeAgo(last) : "尚未抓取";
      txt.title = last ? fullTime(last) : "";
    }
  }

  function refreshAll() {
    return send("POST", "/api/refresh", {}).then(function (r) {
      if (r.started) toast("已开始抓取");
      pollRefresh();
    });
  }

  function pollRefresh() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(function () {
      get("/api/refresh").then(function (s) {
        setRefreshBar(s);
        if (!s.running) {
          clearInterval(state.pollTimer);
          get("/api/bootstrap").then(function (b) {
            if (!b.ok) return;
            state.feeds = b.feeds;
            state.watches = b.watches || [];
            state.counts = b.counts;
            (b.sources || []).forEach(function (s) { SITE_NAMES[s.id] = s.name; });
            renderSidebar();
            updateCounts();
            setRefreshBar(s);
            loadItems(true);
            var hits = s.watchHits || 0;
            if (s.added) toast("抓取完成，新增 " + s.added + " 条" + (hits ? "，关注命中 " + hits : ""));
            else if (hits) toast("关注命中 " + hits + " 条");
            else if (s.errors && s.errors.length) toast("完成，但有 " + s.errors.length + " 个源出错");
            else toast("已是最新");
          });
        }
      });
    }, 700);
  }

  /* ------------------------------------------------------------ 抽屉 / 弹窗 */

  function openSidebar() {
    $("#sidebar").classList.add("open");
    $("#scrim").classList.add("show");
  }
  function closeSidebar() {
    $("#sidebar").classList.remove("open");
    $("#scrim").classList.remove("show");
  }
  function openReader() {
    if (window.innerWidth <= 1080) $("#reader").classList.add("open");
  }
  function closeReader() {
    $("#reader").classList.remove("open");
  }

  function openModal(id) { $(id).hidden = false; }
  function closeModal(id) { $(id).hidden = true; }

  /* ------------------------------------------------------------ 关注 / 镜像 */

  function openWatchModal(wid) {
    state.editingWatch = wid || null;
    var w = wid ? (state.watches || []).filter(function (x) { return x.id === wid; })[0] : null;
    $("#wHead").textContent = w ? "编辑关注" : "新建关注";
    $("#wName").value = w ? w.name : "";
    $("#wInclude").value = w ? w.include : "";
    $("#wExclude").value = w ? w.exclude : "";
    $("#wFansub").value = w ? w.fansub : "";
    $("#wErr").hidden = true;
    $("#wSubmit").textContent = w ? "保存并重新匹配" : "保存并匹配";
    openModal("#modalWatch");
    setTimeout(function () { (w ? $("#wName") : $("#wInclude")).focus(); }, 60);
  }

  function submitWatch() {
    var include = $("#wInclude").value.trim();
    if (!include) {
      $("#wErr").textContent = "「必须包含」至少填一个关键词";
      $("#wErr").hidden = false;
      return;
    }
    var payload = {
      name: $("#wName").value.trim(),
      include: include,
      exclude: $("#wExclude").value.trim(),
      fansub: $("#wFansub").value.trim()
    };
    var btn = $("#wSubmit");
    var wid = state.editingWatch;
    btn.disabled = true;
    var req = wid
      ? send("PATCH", "/api/watches/" + encodeURIComponent(wid), payload)
      : send("POST", "/api/watches", payload);

    req.then(function (r) {
      btn.disabled = false;
      if (!r.ok) {
        $("#wErr").textContent = r.error || "保存失败";
        $("#wErr").hidden = false;
        return;
      }
      state.watches = r.watches;
      state.counts = r.counts;
      closeModal("#modalWatch");
      if (!wid && r.watch) {
        state.feed = "watch:" + r.watch.id;
        localStorage.setItem("arr.feed", state.feed);
      }
      renderSidebar();
      updateCounts();
      syncNav();
      loadItems(true);
      toast(r.hits ? "关注已保存，匹配到 " + r.hits + " 条" : "关注已保存，暂时没有匹配");
    }).catch(function (e) {
      btn.disabled = false;
      $("#wErr").textContent = e.message;
      $("#wErr").hidden = false;
    });
  }

  function showMirrors() {
    $("#mirrorList").innerHTML = '<div class="mirror-loading">读取中…</div>';
    openModal("#modalMirror");
    get("/api/mirrors").then(function (r) {
      var html = (r.sources || []).map(function (s) {
        var hosts = (s.hosts || []).map(function (h) {
          var cls = h.down ? "bad" : (h.ok ? "ok" : "idle");
          var label = h.down ? "冷却中" : (h.ok ? "可用" : "未检测");
          return '<span class="host ' + cls + '" title="' + esc(h.error || h.url) + '">' +
            esc(h.host) + "<i>" + label + "</i></span>";
        }).join("");
        return '<div class="mirror-row"><div class="mirror-name">' + esc(s.name) + "</div>" +
          '<div class="mirror-hosts">' + (hosts || '<span class="host idle">无</span>') + "</div></div>";
      }).join("");
      $("#mirrorList").innerHTML = html || '<div class="mirror-loading">没有可用入口</div>';
    }).catch(function () {
      $("#mirrorList").innerHTML = '<div class="mirror-loading">读取失败</div>';
    });
  }

  /* ------------------------------------------------------------ 设置 */

  var SET = {
    values: {},
    schema: [],
    info: {},
    group: "",
    loaded: false,
    saveTimer: null,
    pending: {},
    notifyTimer: null,
    seen: {}
  };

  function applyClientSettings() {
    var v = SET.values || {};
    if (v.theme) applyTheme(v.theme);
    document.documentElement.setAttribute("data-fs", v.fontSize || "m");
    document.documentElement.setAttribute("data-density", v.density || "cozy");
    startNotifyTimer();
  }

  function openSettings() {
    openModal("#modalSettings");
    if (SET.loaded) {
      renderSetNav();
      renderSetPanes();
    } else {
      $("#setPanes").innerHTML = '<div class="set-loading">正在读取设置…</div>';
      get("/api/settings").then(function (d) {
        if (!d.ok) { $("#setPanes").innerHTML = '<div class="set-loading">读取失败</div>'; return; }
        fillSettings(d);
      });
    }
  }

  function fillSettings(d) {
    SET.values = d.values || SET.values;
    if (d.schema && d.schema.length) SET.schema = d.schema;   // 恢复默认时不带 schema
    SET.info = d.info || SET.info;
    SET.loaded = true;
    if (!SET.group) SET.group = (SET.schema[0] || {}).group || "";
    renderSetNav();
    renderSetPanes();
    applyClientSettings();
  }

  function renderSetNav() {
    var html = SET.schema.map(function (g) {
      return '<button data-g="' + esc(g.group) + '"' +
        (g.group === SET.group ? ' class="active"' : "") + ">" + esc(g.title) + "</button>";
    }).join("");
    html += '<button data-g="about"' +
      (SET.group === "about" ? ' class="active"' : "") + ">关于与维护</button>";
    $("#setNav").innerHTML = html;
  }

  function setItemRow(it) {
    var v = SET.values[it.k];
    var ctrl = "";
    if (it.type === "switch") {
      ctrl = '<button class="sw' + (v ? " on" : "") + '" role="switch"' +
        ' aria-checked="' + (v ? "true" : "false") + '"' +
        ' data-k="' + esc(it.k) + '" data-t="switch"><i></i></button>';
    } else if (it.type === "number") {
      ctrl = '<span class="num"><input type="number" data-k="' + esc(it.k) + '" data-t="number"' +
        ' min="' + it.min + '" max="' + it.max + '" step="' + (it.step || 1) + '"' +
        ' value="' + esc(v) + '"><em>' + esc(it.unit || "") + "</em></span>";
    } else if (it.type === "text") {
      ctrl = '<input class="txt" type="text" data-k="' + esc(it.k) + '" data-t="text"' +
        ' value="' + esc(v == null ? "" : v) + '"' +
        (it.placeholder ? ' placeholder="' + esc(it.placeholder) + '"' : "") + ">";
    } else if (it.type === "select") {
      ctrl = '<select data-k="' + esc(it.k) + '" data-t="select">' +
        (it.options || []).map(function (o) {
          return '<option value="' + esc(o.value) + '"' +
            (String(o.value) === String(v) ? " selected" : "") + ">" + esc(o.label) + "</option>";
        }).join("") + "</select>";
    } else if (it.type === "checks") {      var arr = v || [];
      ctrl = '<span class="chks">' + (it.options || []).map(function (o) {
        var on = arr.indexOf(o.value) >= 0;
        return '<button class="chk' + (on ? " on" : "") + '" data-k="' + esc(it.k) +
          '" data-v="' + esc(o.value) + '" data-t="check"' +
          ' aria-pressed="' + (on ? "true" : "false") + '">' + esc(o.label) + "</button>";
      }).join("") + "</span>";
    }
    return '<div class="set-row" data-row="' + esc(it.k) + '"' +
      (it.depends ? ' data-dep="' + esc(it.depends) + '"' : "") + ">" +
      '<div class="set-label"><b>' + esc(it.label) + "</b>" +
      (it.desc ? "<span>" + esc(it.desc) + "</span>" : "") + "</div>" +
      '<div class="set-ctrl">' + ctrl + "</div></div>";
  }

  function renderSetPanes() {
    if (SET.group === "about") {
      $("#setPanes").innerHTML = aboutPaneHTML();
      return;
    }
    var g = SET.schema.filter(function (x) { return x.group === SET.group; })[0];
    if (!g) { $("#setPanes").innerHTML = ""; return; }
    var html = '<section class="set-pane">' +
      (g.note ? '<p class="set-note">' + esc(g.note) + "</p>" : "") +
      g.items.map(setItemRow).join("") + "</section>";
    if (g.group === "fetch") html += refreshHintHTML();
    $("#setPanes").innerHTML = html;
    syncDeps();
  }

  function refreshHintHTML() {
    var next = (SET.info && SET.info.nextRefresh) || 0;
    var txt = next ? "下次自动刷新约在 " + fullTime(next) + "（服务端计时，关掉页面也会继续）"
      : "自动刷新已关闭";
    return '<p class="set-hint">' + esc(txt) + "</p>";
  }

  function aboutPaneHTML() {
    var i = SET.info || {};
    var st = i.stats || {};
    var rows = [
      ["版本", i.version || "—"],
      ["访问地址", "http://127.0.0.1:" + (i.port || "") + "/"],
      ["局域网", i.lanUrl || (i.lan ? "已开启" : "未开启（--lan 可开启）")],
      ["数据文件", i.dbPath || "—"],
      ["条目 / 订阅源", (st.items || 0) + " 条 · " + (st.feeds || 0) + " 个源"],
      ["数据库大小", fmtBytes(st.dbBytes || 0)],
      ["已运行", Math.floor((i.uptime || 0) / 60) + " 分钟"]
    ];
    return '<section class="set-pane">' +
      '<p class="set-note">清理只删除超过保留天数的未收藏条目，收藏和关注命中都会保留。</p>' +
      '<div class="kv">' + rows.map(function (r) {
        return '<div><span>' + esc(r[0]) + "</span><b>" + esc(r[1]) + "</b></div>";
      }).join("") + "</div>" +
      '<div class="set-actions">' +
      '<button class="ghost-btn" id="btnClean">立即清理旧条目</button>' +
      '<button class="ghost-btn" id="btnVacuum">压缩数据库</button>' +
      "</div></section>";
  }

  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n = n / 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  // 「自动刷新」关掉时，把下面的间隔置灰
  function syncDeps() {
    var v = SET.values;
    $$("#setPanes [data-dep]").forEach(function (el) {
      var off = !v[el.dataset.dep];
      el.classList.toggle("disabled", off);
      $$("input,select,button", el).forEach(function (c) { c.disabled = off; });
    });
  }

  function queueSave(k, val) {
    SET.values[k] = val;
    SET.pending[k] = val;
    applyClientSettings();
    syncDeps();
    clearTimeout(SET.saveTimer);
    SET.saveTimer = setTimeout(flushSettings, 280);
  }

  function flushSettings() {
    if (!Object.keys(SET.pending).length) return;
    var patch = SET.pending;
    SET.pending = {};
    send("POST", "/api/settings", { values: patch }).then(function (r) {
      if (!r.ok) { toast(r.error || "保存失败"); return; }
      SET.values = r.values || SET.values;
      SET.info = r.info || SET.info;
      $$('#setPanes input[data-k]').forEach(function (inp) {
        var k = inp.dataset.k;
        if (document.activeElement !== inp && String(SET.values[k]) !== inp.value) {
          inp.value = SET.values[k];
        }
      });
      var hint = $("#setPanes .set-hint");
      if (hint && SET.group === "fetch") hint.outerHTML = refreshHintHTML();
    }).catch(function () { toast("保存失败，检查服务是否在运行"); });
  }

  function startNotifyTimer() {
    clearInterval(SET.notifyTimer);
    if (!SET.values.airingNotify || !("Notification" in window)) return;
    if (Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (e) { /* 老浏览器忽略 */ }
    }
    SET.notifyTimer = setInterval(checkAiring, 60000);
  }

  function checkAiring() {
    if (!SET.values.airingNotify) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (!SCH.data || !SCH.data.upcoming) return;
    var now = Date.now() / 1000;
    SCH.data.upcoming.forEach(function (it) {
      var t = it.nextTs || 0;
      if (!t || SET.seen[it.id]) return;
      var d = t - now;
      if (d > 0 && d <= 600) {
        SET.seen[it.id] = 1;
        try {
          new Notification("即将开播 · " + it.title, {
            body: (it.time || "") + " 开始，第 " + (it.episode || 1) + " 集",
            tag: "air-" + it.id
          });
        } catch (e) { /* 通知被系统拦了就算了 */ }
      }
    });
  }

  function runMaintenance(action) {
    var btn = action === "clean" ? $("#btnClean") : $("#btnVacuum");
    if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
    send("POST", "/api/maintenance", { action: action, vacuum: true }).then(function (r) {
      if (!r.ok) { toast(r.error || "操作失败"); renderSetPanes(); return; }
      SET.info.stats = r.stats || SET.info.stats;
      if (action === "clean") {
        toast("已清理 " + (r.removed || 0) + " 条旧条目");
        get("/api/bootstrap").then(function (b) {
          if (!b || !b.ok) return;
          state.counts = b.counts;
          updateCounts();
          loadItems(true);
        });
      } else {
        toast("数据库已压缩");
      }
      renderSetPanes();
    }).catch(function () {
      toast("操作失败，检查服务是否在运行");
      renderSetPanes();
    });
  }

  /* ------------------------------------------------------------ 新番放送表 */

  var WEEK_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

  var SCH = {
    data: null,
    loading: false,
    day: (new Date().getDay() + 6) % 7,   // JS 里 0 是周日，这里换成 0=周一
    plat: "",
    q: "",
    current: null,
    timer: null,
    poll: null
  };

  /* ============================================================
     插件视图契约（给模块 / 第三方脚本用）

     模块仓库里放一个 view.js，里面写：

       ARR.registerView({
         id: "hello",                        // 和后端 Module.views 的 id 一致
         title: "示例插件",
         icon: "◆",
         mount: function (el, ctx) { ... },  // el 是内容容器
         unmount: function (el) { ... }      // 可选，清理定时器
       });

     后端 bootstrap 会把视图清单发过来，这里按清单加载脚本，侧栏自动多一个入口。
     ctx 是“只增不改”的对象：以后加字段不改名，老脚本继续能用。
     ============================================================ */
  var PLUG = { views: {}, manifest: [], mounted: null };
  var pendingView = "";
  var BUS = {
    handlers: {},
    on: function (event, fn) {
      if (typeof fn !== "function") return null;
      (BUS.handlers[event] = BUS.handlers[event] || []).push(fn);
      return function () { BUS.off(event, fn); };
    },
    off: function (event, fn) {
      var list = BUS.handlers[event] || [];
      var i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    emit: function (event, payload) {
      var list = (BUS.handlers[event] || []).slice();
      var wild = (BUS.handlers["*"] || []).slice();
      list.concat(wild).forEach(function (fn) {
        try {
          wild.indexOf(fn) >= 0 ? fn(event, payload) : fn(payload);
        } catch (e) {
          if (window.console) console.warn("[ARR] 事件 " + event + " 的订阅者出错", e);
        }
      });
    }
  };

  function manifestOf(id) {
    var list = PLUG.manifest || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  function addPluginNav(spec) {
    var nav = $("nav.nav");
    if (!nav) return;
    var sel = '[data-nav="plugin:' + spec.id + '"]';
    if (nav.querySelector(sel)) {
      if (state.view === "plugin:" + spec.id) syncNav();
      return;
    }
    var btn = document.createElement("button");
    btn.className = "nav-item";
    btn.dataset.nav = "plugin:" + spec.id;
    btn.title = spec.description || spec.title || spec.id;
    btn.innerHTML = '<i class="nav-ico">' + esc(spec.icon || "◆") + "</i>" +
      "<span>" + esc(spec.title || spec.id) + "</span>" +
      (spec.badge ? "<em>" + esc(spec.badge) + "</em>" : "");
    btn.addEventListener("click", function () { switchView("plugin:" + spec.id); });
    nav.appendChild(btn);
  }

  function mountPlugin(id) {
    var host = $("#pluginHost");
    if (!host) return;
    var spec = PLUG.views[id];
    if (!spec) {
      host.innerHTML = '<div class="list-empty"><b>视图没加载出来</b>' +
        "没有找到 id 为 " + esc(id) + " 的视图，插件脚本可能加载失败了。</div>";
      return;
    }
    host.innerHTML = "";
    PLUG.mounted = id;
    var ctx = {
      id: id,
      api: { get: get, send: send },
      toast: toast,
      esc: esc,
      on: BUS.on,
      off: BUS.off,
      emit: BUS.emit,
      openItem: openItem,
      theme: state.theme,
      boot: state.boot,
      baseUrl: (state.boot && state.boot.baseUrl) || ""
    };
    try {
      spec.mount(host, ctx);
    } catch (e) {
      host.innerHTML = '<div class="list-empty"><b>视图渲染失败</b>' +
        esc(e.message) + "</div>";
    }
  }

  function unmountPlugin(view) {
    if (!view || view.indexOf("plugin:") !== 0) return;
    var spec = PLUG.views[view.slice(7)];
    PLUG.mounted = null;
    if (spec && typeof spec.unmount === "function") {
      try { spec.unmount($("#pluginHost")); } catch (e) { /* 清理失败不影响切换 */ }
    }
    var host = $("#pluginHost");
    if (host) host.innerHTML = "";
  }

  function openItem(id) {
    if (!id) return;
    if (state.view !== "items") switchView("items");
    selectItem(id, { silent: true });
  }

  function applyView(name) {
    if (!name) return false;
    if (name === "schedule" || name === "items") { switchView(name); return true; }
    if (name.indexOf("plugin:") === 0) {
      if (PLUG.views[name.slice(7)]) { switchView(name); return true; }
      pendingView = name;                 // 脚本还没加载完，等注册完再切
      return false;
    }
    return false;
  }

  function loadPluginViews(list) {
    PLUG.manifest = list || [];
    PLUG.manifest.forEach(function (v) {
      if (!v.id) return;
      if (v.style) {
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = v.style;
        document.head.appendChild(link);
      }
      if (!v.script) return;               // 没脚本的视图只能由别的脚本注册
      var s = document.createElement("script");
      s.src = v.script;
      s.async = false;
      s.onerror = function () {
        if (window.console) console.warn("[ARR] 视图脚本加载失败：" + v.script);
      };
      s.onload = function () {
        if (!PLUG.views[v.id]) addPluginNav(v);   // 脚本没注册也留个入口（会提示未注册）
        if (pendingView === "plugin:" + v.id) {
          pendingView = "";
          switchView("plugin:" + v.id);
        }
      };
      document.head.appendChild(s);
    });
  }

  var ARR = {
    version: 1,
    registerView: function (spec) {
      if (!spec || !spec.id || typeof spec.mount !== "function") {
        if (window.console) console.warn("[ARR] registerView 需要 id 和 mount()");
        return false;
      }
      var meta = manifestOf(spec.id) || {};
      var view = {
        id: String(spec.id),
        title: spec.title || meta.title || spec.id,
        icon: spec.icon || meta.icon || "",
        description: spec.description || meta.description || "",
        order: typeof spec.order === "number" ? spec.order : (meta.order || 100),
        mount: spec.mount,
        unmount: spec.unmount || null
      };
      PLUG.views[view.id] = view;
      addPluginNav(view);
      if (pendingView === "plugin:" + view.id) {
        pendingView = "";
        switchView("plugin:" + view.id);
      }
      return true;
    },
    getView: function (id) { return PLUG.views[id] || null; },
    views: function () { return Object.keys(PLUG.views); },
    on: BUS.on,
    off: BUS.off,
    emit: BUS.emit,
    toast: toast,
    esc: esc,
    api: { get: get, send: send },
    openItem: openItem
  };
  window.ARR = ARR;

  function platMap() {
    var m = {};
    ((SCH.data && SCH.data.platforms) || []).forEach(function (p) { m[p.id] = p; });
    return m;
  }

  function untilText(ts) {
    var d = ts - Date.now() / 1000;
    if (d <= 0) return "已开播";
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + " 分钟后";
    if (d < 86400) return Math.round(d / 3600) + " 小时后";
    return Math.round(d / 86400) + " 天后";
  }

  function switchView(v) {
    var prev = state.view;
    if (prev !== v) unmountPlugin(prev);      // 离开插件视图时让它自己收尾
    state.view = v;
    var isPlugin = v.indexOf("plugin:") === 0;
    $("#app").classList.toggle("schedule-mode", v === "schedule");
    $("#app").classList.toggle("plugin-mode", isPlugin);
    if (v === "schedule") {
      closeSidebar();
      syncNav();
      loadSchedule(false);
    } else if (isPlugin) {
      closeSidebar();
      syncNav();
      mountPlugin(v.slice(7));
    } else {
      syncNav();
    }
  }

  function loadSchedule(force) {
    if (SCH.loading) return;
    SCH.loading = true;
    if (force) toast("正在从各平台抓取放送表…");
    get("/api/schedule" + (force ? "?refresh=1" : "")).then(function (res) {
      SCH.loading = false;
      if (!res.ok) { toast(res.error || "读取放送表失败"); return; }
      SCH.data = res;
      renderSchedule();
      if (res.refreshing) pollSchedule();
    }).catch(function (e) {
      SCH.loading = false;
      $("#schGrid").innerHTML =
        '<div class="sch-loading">连不上本地服务：' + esc(e.message) + "</div>";
    });
  }

  function pollSchedule() {
    clearTimeout(SCH.poll);
    SCH.poll = setTimeout(function () {
      get("/api/schedule/status").then(function (s) {
        if (s && s.running) { pollSchedule(); return; }
        SCH.loading = false;
        loadSchedule(false);
        if (s && s.message) toast(s.message);
      }).catch(function () { pollSchedule(); });
    }, 1200);
  }

  function renderSchedule() {
    var d = SCH.data || {};
    var st = d.stats || {};
    var srcs = d.sources || [];
    var bad = srcs.filter(function (s) { return !s.ok; });
    var ok = srcs.filter(function (s) { return s.ok; });

    $("#schSrcLabel").textContent = "来源 " + ok.length + "/" + srcs.length;
    $("#cToday").textContent = st.today || 0;

    var bits = [];
    if (st.total) bits.push(st.total + " 部在放送");
    if (st.today) bits.push("今天 " + st.today + " 部");
    if (d.updated) bits.push("更新于 " + timeAgo(d.updated) + " · " + (d.timezone || "UTC+8"));
    if (d.refreshing) bits.push("正在刷新…");
    else if (bad.length) bits.push(bad.length + " 个平台没取到");
    $("#schSub").textContent = bits.length ? bits.join(" · ") : "还没有数据";

    renderSchChips();
    renderSchWeek();
    renderSchGrid();
    renderSchSoon();

    clearInterval(SCH.timer);
    SCH.timer = setInterval(renderSchSoon, 30000);
  }

  function renderSchChips() {
    var d = SCH.data || {};
    var st = d.stats || {};
    var by = st.byPlatform || {};
    var list = [{ id: "", name: "全部", n: st.total || 0 }];
    (d.platforms || []).forEach(function (p) {
      list.push({ id: p.id, name: p.short || p.name, n: by[p.id] || 0,
                  color: p.color, full: p.name });
    });
    $("#schChips").innerHTML = list.map(function (c) {
      return '<button class="chip' + (SCH.plat === c.id ? " on" : "") +
        '" data-plat="' + esc(c.id) + '"' +
        (c.full ? ' title="' + esc(c.full) + " · " + c.n + ' 部"' : "") + ">" +
        (c.color ? '<i style="background:' + c.color + '"></i>' : "") +
        esc(c.name) + (c.n ? "<em>" + c.n + "</em>" : "") + "</button>";
    }).join("");
  }

  function renderSchWeek() {
    var days = (SCH.data && SCH.data.days) || [];
    $("#schWeek").innerHTML = days.map(function (d) {
      return '<button class="daytab' + (SCH.day === d.weekday ? " on" : "") +
        (d.isToday ? " today" : "") + '" data-day="' + d.weekday + '">' +
        "<b>" + esc(d.label) + "</b>" +
        (d.date ? "<span>" + esc(d.date) + "</span>" : "") +
        "<em>" + filterDay(d).length + "</em></button>";
    }).join("");
  }

  function filterDay(day) {
    var q = (SCH.q || "").toLowerCase();
    return (day.items || []).filter(function (it) {
      if (SCH.plat && (it.via || [it.platform]).indexOf(SCH.plat) < 0) return false;
      if (!q) return true;
      var hay = [it.title, it.titleCn, it.titleJp, it.titleEn]
        .join(" ").toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  function animeKeyword(it) {
    return (it.titleCn || it.title || "").trim();
  }

  function watchOfTitle(it) {
    var kw = animeKeyword(it).toLowerCase();
    if (!kw) return null;
    return (state.watches || []).filter(function (w) {
      return (w.include || "").trim().toLowerCase() === kw;
    })[0] || null;
  }

  function schCard(it) {
    var pm = platMap();
    var cover = it.cover
      ? '<img src="' + esc(it.cover) + '" alt="" loading="lazy" ' +
        'referrerpolicy="no-referrer" onerror="this.remove()">'
      : "";
    var via = (it.via || [it.platform]).map(function (pid) {
      var p = pm[pid] || {};
      return '<span class="pchip" title="' + esc(p.name || pid) + '">' +
        '<i style="background:' + (p.color || "#8d96a4") + '"></i>' +
        esc(p.short || pid) + "</span>";
    }).join("");
    var eps = it.episode
      ? ("第" + it.episode + "话" + (it.episodes ? "/" + it.episodes : ""))
      : (it.episodes ? it.episodes + " 话" : "");
    return '<article class="sch-card' + (it.soon ? " soon" : "") +
      '" data-anime="' + esc(it.id) + '" tabindex="0">' +
      '<div class="sch-cover" data-ch="' + esc((it.title || "?").charAt(0)) + '">' +
      cover + "</div>" +
      '<div class="sch-info">' +
      '<div class="sch-name">' + esc(it.title) + "</div>" +
      '<div class="sch-meta"><span class="sch-time">' + esc(it.time || "待定") + "</span>" +
      (eps ? "<span>" + esc(eps) + "</span>" : "") + "</div>" +
      '<div class="sch-pchips">' + via + "</div></div>" +
      '<span class="sch-star' + (watchOfTitle(it) ? " on" : "") +
      '" data-star-anime="' + esc(it.id) + '" title="加入关注">★</span>' +
      "</article>";
  }

  function renderSchGrid() {
    var days = (SCH.data && SCH.data.days) || [];
    $("#schGrid").innerHTML = days.map(function (day) {
      var rows = filterDay(day);
      var cards = rows.map(schCard).join("") ||
        '<div class="sch-none">' + (day.count ? "没有符合筛选的番" : "没有放送") + "</div>";
      return '<section class="sch-col' + (day.isToday ? " today" : "") +
        (SCH.day === day.weekday ? " active" : "") + '" data-day="' + day.weekday + '">' +
        '<div class="sch-col-head"><b>' + esc(day.label) + "</b>" +
        (day.date ? "<span>" + esc(day.date) + "</span>" : "") +
        "<em>" + rows.length + "</em></div>" +
        '<div class="sch-cards">' + cards + "</div></section>";
    }).join("");

    var total = days.reduce(function (n, d) { return n + filterDay(d).length; }, 0);
    var empty = $("#schEmpty");
    empty.hidden = total > 0;
    if (!total) {
      empty.innerHTML = (SCH.data && SCH.data.stats && SCH.data.stats.total)
        ? "<b>没有符合筛选的番</b>换个平台，或者清空搜索词试试。"
        : "<b>这里还没有放送表</b>点右上角的刷新按钮，从哔哩哔哩 / Bangumi / AniList 抓一份当季番表。";
    }
  }

  function renderSchSoon() {
    var up = (SCH.data && SCH.data.upcoming) || [];
    var pm = platMap();
    var box = $("#schSoon");
    if (!up.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = '<div class="soon-label">接下来</div>' +
      up.slice(0, 8).map(function (it) {
        var p = pm[it.platform] || {};
        return '<button class="soon-item" data-anime="' + esc(it.id) + '">' +
          '<span class="soon-when">' + esc(untilText(it.nextTs)) + "</span>" +
          '<span class="soon-title">' + esc(it.title) + "</span>" +
          '<span class="soon-meta">' +
          (it.weekday >= 0 ? esc(WEEK_LABEL[it.weekday]) + " " + esc(it.time || "") + " · " : "") +
          (it.episode ? "第" + it.episode + "话" : "新一集") + "</span>" +
          '<i class="soon-dot" style="background:' + (p.color || "#8d96a4") + '"></i>' +
          "</button>";
      }).join("");
  }

  function findAnime(id) {
    var days = (SCH.data && SCH.data.days) || [];
    for (var i = 0; i < days.length; i++) {
      var items = days[i].items || [];
      for (var j = 0; j < items.length; j++) {
        if (items[j].id === id) return items[j];
      }
    }
    for (var k = 0; k < ((SCH.data && SCH.data.upcoming) || []).length; k++) {
      if (SCH.data.upcoming[k].id === id) return SCH.data.upcoming[k];
    }
    return null;
  }

  function openAnime(id) {
    var it = findAnime(id);
    if (!it) return;
    SCH.current = it;
    var pm = platMap();

    $("#anCover").innerHTML = it.cover
      ? '<img src="' + esc(it.cover) + '" alt="" referrerpolicy="no-referrer" ' +
        'onerror="this.remove()">'
      : "";
    $("#anCover").setAttribute("data-ch", (it.title || "?").charAt(0));

    var bits = [];
    bits.push(it.weekday >= 0
      ? WEEK_LABEL[it.weekday] + " " + (it.time || "时间待定")
      : "放送时间待定");
    if (it.nextTs) bits.push(untilText(it.nextTs) + "更新");
    if (it.episode) {
      bits.push("第 " + it.episode + " 话" + (it.episodes ? " / 共 " + it.episodes + " 话" : ""));
    } else if (it.episodes) {
      bits.push("共 " + it.episodes + " 话");
    }
    if (it.score) bits.push("★ " + it.score);
    if (it.kind) bits.push(it.kind);
    $("#anSub").textContent = bits.join(" · ");

    $("#anTitle").textContent = it.title;
    $("#anLinks").innerHTML = (it.links || []).map(function (l) {
      var p = pm[l.id] || {};
      return '<a class="anime-link" href="' + esc(l.url) + '" target="_blank" rel="noopener">' +
        '<i style="background:' + (p.color || "#8d96a4") + '"></i>' +
        esc(l.full || l.name) + "</a>";
    }).join("");

    var alias = [];
    if (it.titleJp && it.titleJp !== it.title) alias.push("日文名：" + it.titleJp);
    if (it.titleEn && it.titleEn !== it.title) alias.push("英文名：" + it.titleEn);
    var via = (it.via || []).map(function (pid) {
      var p = pm[pid] || {};
      return p.name || pid;
    });
    if (via.length > 1) alias.push("合并自 " + via.join(" + "));
    $("#anNote").textContent = alias.join("　");

    var w = watchOfTitle(it);
    $("#anWatch").textContent = w ? "已在关注列表" : "关注这部番";
    $("#anWatch").disabled = !!w;
    openModal("#modalAnime");
  }

  function watchAnime(it) {
    it = it || SCH.current;
    if (!it) return;
    if (watchOfTitle(it)) { toast("已经在关注列表里了"); return; }
    var kw = animeKeyword(it);
    if (!kw) return;
    send("POST", "/api/watches", { name: kw, include: kw }).then(function (r) {
      if (!r.ok) { toast(r.error || "关注失败"); return; }
      state.watches = r.watches || state.watches;
      state.counts = r.counts || state.counts;
      renderSidebar();
      updateCounts();
      renderSchedule();
      if (SCH.current && SCH.current.id === it.id) openAnime(it.id);
      toast(r.hits ? "已关注，命中 " + r.hits + " 条资源"
                   : "已关注，下次抓取时会挑出来");
    }).catch(function (e) { toast("关注失败：" + e.message); });
  }

  function toggleWatchAnimeById(id) {
    var it = findAnime(id);
    if (it) watchAnime(it);
  }

  function searchAnime() {
    var it = SCH.current;
    if (!it) return;
    var kw = animeKeyword(it);
    closeModal("#modalAnime");
    switchView("items");
    state.feed = "all";
    state.filter = "all";
    state.q = kw;
    $("#q").value = kw;
    localStorage.setItem("arr.feed", "all");
    localStorage.setItem("arr.filter", "all");
    syncNav();
    loadItems(true);
    toast("正在资源里搜「" + kw + "」");
  }

  function showSchSources() {
    var srcs = (SCH.data && SCH.data.sources) || [];
    $("#schSrcList").innerHTML = srcs.map(function (s) {
      var cls = s.ok ? "ok" : (s.cached ? "idle" : "bad");
      var label = s.ok ? (s.count + " 部") : (s.cached ? "已跳过" : "失败");
      return '<div class="mirror-row"><div class="mirror-name">' + esc(s.name) + "</div>" +
        '<div class="mirror-hosts"><span class="host ' + cls + '" title="' +
        esc(s.error || s.note || "") + '">' + esc(s.short || s.id) +
        "<i>" + esc(label) + "</i></span>" +
        (s.ms ? '<span class="host idle" title="耗时">' + s.ms + "ms</span>" : "") +
        "</div></div>";
    }).join("") || '<div class="mirror-loading">还没有抓过</div>';
    openModal("#modalSchSrc");
  }

  /* ------------------------------------------------------------ 事件 */

  function bind() {
    // 侧栏导航
    $$(".nav-item").forEach(function (el) {
      if (el.dataset.nav.indexOf("plugin:") === 0) return;   // 插件入口自己绑
      el.addEventListener("click", function () {
        if (el.dataset.nav === "schedule") {
          switchView("schedule");
          return;
        }
        if (state.view !== "items") switchView("items");
        state.filter = el.dataset.nav;
        state.feed = "all";
        localStorage.setItem("arr.filter", state.filter);
        localStorage.setItem("arr.feed", "all");
        syncNav();
        closeSidebar();
        loadItems(true);
      });
    });

    // 频道点击（含删除）
    document.addEventListener("click", function (e) {
      var delWatch = e.target.closest("[data-delwatch]");
      if (delWatch) {
        e.stopPropagation();
        e.preventDefault();
        var wid = delWatch.dataset.delwatch;
        var w = (state.watches || []).filter(function (x) { return x.id === wid; })[0];
        if (!w) return;
        if (!confirm('删除关注「' + w.name + '」？命中的条目会从关注里移除（原始条目仍在其它频道）。')) return;
        send("DELETE", "/api/watches/" + encodeURIComponent(wid)).then(function (r) {
          if (!r.ok) { toast(r.error || "删除失败"); return; }
          state.watches = r.watches;
          state.counts = r.counts;
          if (state.feed === "watch:" + wid) {
            state.feed = "all";
            localStorage.setItem("arr.feed", "all");
          }
          renderSidebar();
          updateCounts();
          updateChannelHead();
          loadItems(true);
          toast("已删除关注");
        });
        return;
      }

      var editWatch = e.target.closest("[data-editwatch]");
      if (editWatch) {
        e.stopPropagation();
        e.preventDefault();
        openWatchModal(editWatch.dataset.editwatch);
        return;
      }

      var del = e.target.closest("[data-del]");
      if (del) {
        e.stopPropagation();
        e.preventDefault();
        var fid = del.dataset.del;
        var f = state.feeds.filter(function (x) { return x.id === fid; })[0];
        if (!f) return;
        if (!confirm('删除订阅「' + f.title + '」？已抓到的条目会一并移除。')) return;
        send("DELETE", "/api/feeds/" + encodeURIComponent(fid)).then(function (r) {
          if (!r.ok) { toast(r.error || "删除失败"); return; }
          state.feeds = r.feeds;
          state.counts = r.counts;
          if (state.feed === fid) { state.feed = "all"; localStorage.setItem("arr.feed", "all"); }
          renderSidebar();
          updateCounts();
          loadItems(true);
          toast("已删除");
        });
        return;
      }

      var feedEl = e.target.closest(".feed-item");
      if (feedEl) {
        if (state.view !== "items") switchView("items");
        state.feed = feedEl.dataset.feed;
        localStorage.setItem("arr.feed", state.feed);
        syncNav();
        closeSidebar();
        loadItems(true);
      }
    });

    // 列表
    $("#listBody").addEventListener("click", function (e) {
      var act = e.target.closest("[data-action]");
      if (act) {
        if (act.dataset.action === "refresh") { refreshAll(); return; }
        if (act.dataset.action === "reload") { loadItems(true); return; }
      }
      var row = e.target.closest(".row");
      if (row) selectItem(row.dataset.id);
    });

    $("#listBody").addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        var row = e.target.closest(".row");
        if (row) selectItem(row.dataset.id);
      }
    });

    // 搜索
    var qTimer = null;
    $("#q").addEventListener("input", function (e) {
      state.q = e.target.value.trim();
      clearTimeout(qTimer);
      qTimer = setTimeout(function () { loadItems(true); }, 260);
    });

    // 阅读区工具
    $("#btnStar").addEventListener("click", function () { if (state.currentId) toggleStar(state.currentId); });
    $("#btnUnread").addEventListener("click", function () {
      var it = state.items.filter(function (x) { return x.id === state.currentId; })[0];
      if (!it) return;
      markRead(it, false);
      toast("已标记为未读");
    });
    $("#btnOpenPage").addEventListener("click", function () {
      var it = state.items.filter(function (x) { return x.id === state.currentId; })[0];
      if (it && it.link) window.open(it.link, "_blank", "noopener");
    });
    $("#btnBack").addEventListener("click", closeReader);

    $("#artActions").addEventListener("click", function (e) {
      var btn = e.target.closest('[data-act="copy-magnet"]');
      if (!btn) return;
      var it = state.items.filter(function (x) { return x.id === state.currentId; })[0];
      if (!it || !it.magnet) return;
      copyText(it.magnet).then(function () {
        toast("磁力链接已复制，去下载器粘贴");
      }).catch(function () { toast("复制失败，请手动展开下方原文"); });
    });

    // 顶部按钮
    $("#btnRefresh").addEventListener("click", refreshAll);
    $("#btnRefreshM").addEventListener("click", refreshAll);
    $("#btnMenu").addEventListener("click", openSidebar);
    $("#scrim").addEventListener("click", closeSidebar);

    $("#btnReadAll").addEventListener("click", function () {
      send("POST", "/api/read-all", { feed: state.feed }).then(function (r) {
        if (!r.ok) { toast(r.error || "操作失败"); return; }
        state.counts = r.counts;
        state.items.forEach(function (i) { i.read = true; });
        renderList();
        updateCounts();
        toast("已全部标为已读");
      });
    });

    // 主题循环：自动 → 浅色 → 深色
    $("#btnTheme").addEventListener("click", function () {
      var order = ["auto", "light", "dark"];
      var next = order[(order.indexOf(state.theme) + 1) % 3];
      applyTheme(next);
      if (SET.loaded) queueSave("theme", next);
    });

    // 导出
    $("#btnExport").addEventListener("click", function () {
      var base = location.origin;
      $("#expAll").value = base + "/feed.xml";
      $("#expOpml").value = base + "/opml.xml";
      var lan = state.boot && state.boot.lan;
      $("#expLan").textContent = lan && lan.enabled
        ? "手机/平板同 WiFi 可用：" + base
        : "当前只监听本机。想让手机上的阅读器订阅，用 --lan 参数重启即可。";
      openModal("#modalExport");
    });
    $("#expClose").addEventListener("click", function () { closeModal("#modalExport"); });
    $$("[data-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var input = $("#" + btn.dataset.copy);
        copyText(input.value).then(function () { toast("已复制"); });
      });
    });

    // 关注
    $("#btnAddWatch").addEventListener("click", function () { openWatchModal(null); });
    $("#wCancel").addEventListener("click", function () { closeModal("#modalWatch"); });
    $("#wSubmit").addEventListener("click", submitWatch);
    ["wName", "wInclude", "wExclude", "wFansub"].forEach(function (id) {
      $("#" + id).addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); submitWatch(); }
      });
    });

    // 镜像状态
    $("#btnMirror").addEventListener("click", showMirrors);
    $("#mirrorClose").addEventListener("click", function () { closeModal("#modalMirror"); });

    // 设置
    $("#btnSettings").addEventListener("click", openSettings);
    $("#setClose").addEventListener("click", function () { closeModal("#modalSettings"); });
    $("#setReset").addEventListener("click", function () {
      send("POST", "/api/settings/reset", {}).then(function (r) {
        if (!r.ok) { toast(r.error || "操作失败"); return; }
        fillSettings(r);
        toast("已恢复默认设置");
      });
    });
    $("#setNav").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-g]");
      if (!b) return;
      SET.group = b.dataset.g;
      renderSetNav();
      renderSetPanes();
    });

    $("#setPanes").addEventListener("click", function (e) {
      var sw = e.target.closest(".sw");
      if (sw) {
        var k = sw.dataset.k;
        var on = !SET.values[k];
        sw.classList.toggle("on", on);
        sw.setAttribute("aria-checked", on ? "true" : "false");
        queueSave(k, on);
        return;
      }
      var chk = e.target.closest(".chk");
      if (chk) {
        var key = chk.dataset.k;
        var arr = (SET.values[key] || []).slice();
        var i = arr.indexOf(chk.dataset.v);
        if (i >= 0) {
          if (arr.length === 1) { toast("至少保留一个来源"); return; }
          arr.splice(i, 1);
        } else {
          arr.push(chk.dataset.v);
        }
        chk.classList.toggle("on", arr.indexOf(chk.dataset.v) >= 0);
        chk.setAttribute("aria-pressed", arr.indexOf(chk.dataset.v) >= 0 ? "true" : "false");
        queueSave(key, arr);
        return;
      }
      if (e.target.closest("#btnClean")) {
        runMaintenance("clean");
        return;
      }
      if (e.target.closest("#btnVacuum")) {
        runMaintenance("vacuum");
      }
    });

    $("#setPanes").addEventListener("change", function (e) {
      var el = e.target;
      if (!el.dataset || !el.dataset.k) return;
      if (el.dataset.t === "select") {
        queueSave(el.dataset.k, el.value);
      } else if (el.dataset.t === "number") {
        queueSave(el.dataset.k, parseInt(el.value, 10) || 0);
      } else if (el.dataset.t === "text") {
        queueSave(el.dataset.k, el.value);
      }
    });

    // 新番放送表
    $("#btnMenuSch").addEventListener("click", openSidebar);
    $("#btnSchRefresh").addEventListener("click", function () { loadSchedule(true); });
    $("#btnSchRefreshM").addEventListener("click", function () { loadSchedule(true); });
    $("#btnSchSources").addEventListener("click", showSchSources);
    $("#schSrcClose").addEventListener("click", function () { closeModal("#modalSchSrc"); });

    $("#schChips").addEventListener("click", function (e) {
      var chip = e.target.closest("[data-plat]");
      if (!chip) return;
      SCH.plat = chip.dataset.plat;
      renderSchChips();
      renderSchGrid();
      renderSchWeek();
    });

    $("#schWeek").addEventListener("click", function (e) {
      var tab = e.target.closest("[data-day]");
      if (!tab) return;
      SCH.day = parseInt(tab.dataset.day, 10);
      renderSchWeek();
      renderSchGrid();
      var col = $('.sch-col[data-day="' + SCH.day + '"]');
      if (col) col.scrollIntoView({ block: "nearest" });
    });

    var schQTimer = null;
    $("#schQ").addEventListener("input", function (e) {
      SCH.q = e.target.value.trim();
      clearTimeout(schQTimer);
      schQTimer = setTimeout(function () {
        renderSchGrid();
        renderSchWeek();
      }, 200);
    });

    $("#schGrid").addEventListener("click", function (e) {
      var star = e.target.closest("[data-star-anime]");
      if (star) {
        e.stopPropagation();
        toggleWatchAnimeById(star.dataset.starAnime);
        return;
      }
      var card = e.target.closest(".sch-card");
      if (card) openAnime(card.dataset.anime);
    });

    $("#schGrid").addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      var card = e.target.closest(".sch-card");
      if (card) openAnime(card.dataset.anime);
    });

    $("#schSoon").addEventListener("click", function (e) {
      var item = e.target.closest("[data-anime]");
      if (item) openAnime(item.dataset.anime);
    });

    $("#anWatch").addEventListener("click", function () { watchAnime(SCH.current); });
    $("#anSearch").addEventListener("click", searchAnime);

    // 添加订阅
    $("#btnAddFeed").addEventListener("click", function () {
      $("#fErr").hidden = true;
      $("#fTitle").value = "";
      $("#fUrl").value = "";
      openModal("#modalFeed");
      setTimeout(function () { $("#fUrl").focus(); }, 60);
    });
    $("#fCancel").addEventListener("click", function () { closeModal("#modalFeed"); });
    $("#fSubmit").addEventListener("click", function () {
      var url = $("#fUrl").value.trim();
      var title = $("#fTitle").value.trim();
      if (!/^https?:\/\//i.test(url)) {
        $("#fErr").textContent = "请填写完整的 http:// 或 https:// 地址";
        $("#fErr").hidden = false;
        return;
      }
      var btn = $("#fSubmit");
      btn.disabled = true;
      btn.textContent = "抓取中…";
      send("POST", "/api/feeds", { url: url, title: title }).then(function (r) {
        btn.disabled = false;
        btn.textContent = "添加并抓取";
        if (!r.ok) {
          $("#fErr").textContent = r.error || "添加失败";
          $("#fErr").hidden = false;
          return;
        }
        state.feeds = r.feeds;
        state.counts = r.counts;
        renderSidebar();
        updateCounts();
        closeModal("#modalFeed");
        state.feed = r.feeds[r.feeds.length - 1].id;
        localStorage.setItem("arr.feed", state.feed);
        syncNav();
        loadItems(true);
        toast(r.warning || "订阅已添加" + (r.added ? "，" + r.added + " 条" : ""));
      }).catch(function (e) {
        btn.disabled = false;
        btn.textContent = "添加并抓取";
        $("#fErr").textContent = e.message;
        $("#fErr").hidden = false;
      });
    });
    $("#fUrl").addEventListener("keydown", function (e) {
      if (e.key === "Enter") $("#fSubmit").click();
    });

    $$(".modal-wrap").forEach(function (w) {
      w.addEventListener("click", function (e) {
        if (e.target === w) w.hidden = true;
      });
    });

    // 键盘
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea") {
        if (e.key === "Escape") e.target.blur();
        return;
      }
      var k = e.key;
      if (k === ",") { e.preventDefault(); openSettings(); return; }
      // 设置里关掉快捷键后，只保留 Esc
      if (SET.values.hotkeys === false && k !== "Escape") return;
      if (k === "/") { e.preventDefault(); $("#q").focus(); return; }
      if (k === "t" || k === "T") {
        switchView(state.view === "schedule" ? "items" : "schedule");
        return;
      }
      if (k === "j" || k === "ArrowDown") { e.preventDefault(); step(1); return; }
      if (k === "k" || k === "ArrowUp") { e.preventDefault(); step(-1); return; }
      if (k === "Enter" && state.currentId) { $("#btnOpenPage").click(); return; }
      if (k === "s" || k === "S") { if (state.currentId) toggleStar(state.currentId); return; }
      if (k === "m" || k === "M") {
        var it = state.items.filter(function (x) { return x.id === state.currentId; })[0];
        if (it) markRead(it, !it.read);
        return;
      }
      if (k === "r" || k === "R") {
        if (state.view === "schedule") loadSchedule(true);
        else refreshAll();
        return;
      }
      if (k === "Escape") {
        var modal = $$(".modal-wrap").filter(function (w) { return !w.hidden; })[0];
        closeReader(); closeSidebar();
        closeModal("#modalFeed"); closeModal("#modalExport");
        closeModal("#modalWatch"); closeModal("#modalMirror");
        closeModal("#modalAnime"); closeModal("#modalSchSrc");
        closeModal("#modalSettings");
        if (!modal && state.view === "schedule") switchView("items");
      }
    });

    // 无限滚动
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting && state.hasMore && !state.loading && sentinel.isConnected) {
          loadItems(false);
        }
      }, { root: $("#listBody"), rootMargin: "260px" });
      io.observe(sentinel);
    }

    window.addEventListener("resize", function () {
      if (window.innerWidth > 1080) $("#reader").classList.remove("open");
      if (window.innerWidth > 860) closeSidebar();
    });
  }

  /* ------------------------------------------------------------ 启动 */

  function boot_() {
    applyTheme(state.theme);
    bind();
    skeleton(7);

    get("/api/bootstrap").then(function (b) {
      if (!b.ok) { toast("服务未就绪"); return; }
      state.boot = b;
      state.feeds = b.feeds;
      state.watches = b.watches || [];
      state.counts = b.counts;
      (b.sources || []).forEach(function (s) { SITE_NAMES[s.id] = s.name; });

      // 服务端存的主题 / 字号 / 密度优先于本地缓存
      if (b.settings) {
        SET.values = b.settings;
        applyClientSettings();
      }

      // 深链：?feed=dmhy / ?feed=watch:w:xxx，方便分享某个频道
      var qs = new URLSearchParams(location.search);
      var deepFeed = qs.get("feed");
      if (deepFeed) {
        state.feed = deepFeed;
        localStorage.setItem("arr.feed", state.feed);
      }

      renderSidebar();
      updateCounts();
      updateChannelHead();
      setRefreshBar(b.refresh);
      loadItems(true);

      // 视图清单：插件脚本加载完会自己调 ARR.registerView，侧栏自动出现入口
      loadPluginViews(b.views || []);

      // ?view=schedule / ?view=plugin:<id> 直接进对应视图
      var deepView = qs.get("view");
      if (deepView) applyView(deepView);

      // 支持 ?item=<id> 深链，方便从别处直接打开某一条
      var deep = qs.get("item");
      if (deep) selectItem(deep, { silent: true, scroll: false });

      if (b.refresh && b.refresh.running) {
        pollRefresh();
      } else if (b.counts.total === 0) {
        refreshAll();
      } else {
        // 距上次成功抓取超过半小时就静默补一轮
        var lastOk = 0;
        state.feeds.forEach(function (f) { if (f.lastOk > lastOk) lastOk = f.lastOk; });
        if (Date.now() / 1000 - lastOk > 1800) refreshAll();
      }
    }).catch(function (e) {
      $("#listBody").innerHTML =
        '<div class="list-empty"><b>连不上本地服务</b>' + esc(e.message) + "</div>";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot_);
  } else {
    boot_();
  }
})();
