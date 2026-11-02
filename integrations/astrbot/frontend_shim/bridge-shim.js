/*
 * FishTool → AstrBot 插件视图运行时适配（原业务 JS 零改动）。
 *
 * 加载顺序：<head> 最前依次是 bridge-sdk.js、本文件、shim.css，之后才是 FishTool 原有资源。
 * 服务端发现已有 bridge-sdk 标签时只改写其 URL、不重复注入（plugin_page_service.py:601-617），
 * 所以执行到这里时 window.AstrBotPluginView 必然已存在。
 *
 * 适配内容：
 *   1) fetch('/api/…') → bridge.apiGet / apiPost（POST 隧道还原 PUT/PATCH/DELETE 与 query），
 *      后端统一信封 {http_status, body} 还原成标准 Response，原 apiRequest 的错误分支照常工作。
 *   2) WebSocket('/ws') → bridge.subscribeSSE('_events')。
 *   3) 沙箱 null-origin 下 sessionStorage/localStorage 访问抛 SecurityError → 内存实现。
 *   4) 页面切换写 hash，支持刷新后回到原页面。
 */
(function () {
  "use strict";
  var B = function () { return window.AstrBotPluginView; };
  if (!B()) {
    console.error("[FishTool] AstrBot bridge SDK 未加载，插件视图无法工作");
    return;
  }
  var ready = B().ready();

  // ---------- 1) fetch ----------
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;

  function toEndpoint(pathname) {
    // "/api/logs/" → "api/logs"：bridge 拒绝空路径段；每段先 decode，避免 bridge 再编码成 %25xx。
    var segs = pathname.slice("/api/".length).split("/").filter(Boolean).map(function (s) {
      try { return decodeURIComponent(s); } catch (e) { return s; }
    });
    return "api/" + segs.join("/");
  }

  function jsonResponse(status, body) {
    var noBody = [101, 204, 205, 304].indexOf(status) >= 0;
    return new Response(noBody ? null : JSON.stringify(body === undefined ? null : body), {
      status: status,
      headers: { "Content-Type": "application/json" },
    });
  }

  window.fetch = async function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.indexOf("/api/") !== 0) {
      if (!nativeFetch) throw new TypeError("fetch unavailable");
      return nativeFetch(input, init);
    }
    await ready;
    var u = new URL(url, "http://fishtool.local");
    var endpoint = toEndpoint(u.pathname);
    var query = {};
    u.searchParams.forEach(function (v, k) { query[k] = v; });
    var method = String(init.method || "GET").toUpperCase();
    var body;
    if (init.body !== undefined && init.body !== null) {
      try { body = JSON.parse(init.body); } catch (e) { body = init.body; }
    }
    var env;
    try {
      env = method === "GET"
        ? await B().apiGet(endpoint, query)
        : await B().apiPost(endpoint, { __fishtool_tunnel: 1, method: method, query: query, body: body });
    } catch (e) {
      return jsonResponse(502, { detail: (e && e.message) || "bridge error" });
    }
    if (!env || typeof env.http_status !== "number") {
      return jsonResponse(502, { detail: "插件返回格式异常" });
    }
    return jsonResponse(env.http_status, env.body);
  };

  // ---------- 2) WebSocket('/ws') → SSE ----------
  var NativeWS = window.WebSocket;
  function BridgeSocket() {
    var self = new EventTarget();
    self.readyState = 0;
    self.url = "/ws";
    self.send = function () { /* 原 /ws 客户端不发业务消息 */ };
    var closed = false;
    var markClosed = function () {
      if (closed) return;
      closed = true;
      self.readyState = 3;
      self.dispatchEvent(new Event("close"));
    };
    self.close = function () {
      if (self._sid) {
        Promise.resolve(B().unsubscribeSSE(self._sid)).catch(function () { /* 已断开 */ });
      }
      markClosed();
    };
    ready.then(function () {
      return B().subscribeSSE("_events", {
        onOpen: function () {
          self.readyState = 1;
          self.dispatchEvent(new Event("open"));
        },
        onMessage: function (ev) {
          if (ev.eventType === "ready") return;
          self.dispatchEvent(new MessageEvent("message", { data: ev.raw }));
        },
        onClose: markClosed,
        onError: function () {
          self.dispatchEvent(new Event("error"));
          markClosed();
        },
      });
    }).then(function (sid) {
      self._sid = sid;
    }).catch(function (e) {
      console.warn("[FishTool] 事件订阅失败:", e && e.message);
      markClosed();
    });
    return self;
  }
  var ShimWebSocket = function (url, protocols) {
    if (/\/ws(\?.*)?$/.test(String(url))) return BridgeSocket();
    return new NativeWS(url, protocols);
  };
  ShimWebSocket.CONNECTING = 0;
  ShimWebSocket.OPEN = 1;
  ShimWebSocket.CLOSING = 2;
  ShimWebSocket.CLOSED = 3;
  window.WebSocket = ShimWebSocket;

  // ---------- 3) Storage ----------
  function memoryStorage() {
    var mem = new Map();
    return {
      getItem: function (k) { k = String(k); return mem.has(k) ? mem.get(k) : null; },
      setItem: function (k, v) { mem.set(String(k), String(v)); },
      removeItem: function (k) { mem.delete(String(k)); },
      clear: function () { mem.clear(); },
      key: function (i) { var keys = Array.from(mem.keys()); return i < keys.length ? keys[i] : null; },
      get length() { return mem.size; },
    };
  }
  ["sessionStorage", "localStorage"].forEach(function (name) {
    try {
      var s = window[name];
      s.getItem("__fishtool_probe__");
    } catch (e) {
      try {
        Object.defineProperty(window, name, { value: memoryStorage(), configurable: true });
      } catch (e2) {
        console.warn("[FishTool] 无法替换 " + name + ":", e2 && e2.message);
      }
    }
  });

  // ---------- 4) hash 深链 ----------
  window.addEventListener("load", function () {
    setTimeout(function () {
      var orig = window.navigateTo;
      if (typeof orig !== "function") return;
      window.navigateTo = function (name) {
        var result = orig.apply(this, arguments);
        try { history.replaceState(null, "", "#" + name); } catch (e) { /* null-origin 下不可用则放弃深链 */ }
        return result;
      };
      var h = (location.hash || "").slice(1);
      if (h && document.getElementById(h + "-page")) window.navigateTo(h);
    }, 0);
  });

  window.__FISHTOOL_ASTRBOT__ = true;
})();
