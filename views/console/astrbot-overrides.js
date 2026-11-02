/*
 * FishTool 插件视图功能替换（在所有 app.*.js 之后加载）。
 *
 * iframe sandbox="allow-scripts allow-forms allow-downloads"，没有 allow-modals / allow-popups /
 * allow-same-origin，因此：
 *   - 自诊报告的 print() 被拦 → 改为把报告 HTML 交给插件保存，再经 bridge.download() 下载 .html；
 *   - 日志导出的 Blob + a.download 在 null-origin 下不可靠 → 改为插件路由生成文件后下载；
 *   - 剪贴板通常被拒 → 失败时弹窗显示文本供手动复制；
 *   - target=_blank 与外站链接打不开 → 弹窗显示链接供复制。
 */
(function () {
  "use strict";
  var B = window.AstrBotPluginView;
  if (!B || !window.__FISHTOOL_ASTRBOT__) return;

  function toast(message, level, title) {
    if (typeof window.showToast === "function") {
      window.showToast(message, { level: level || "info", title: title || "提示" });
    } else if (typeof window.showAppAlert === "function") {
      window.showAppAlert(message);
    }
  }

  function showCopyDialog(title, text) {
    if (typeof window.mountAppModal !== "function") {
      if (typeof window.showAppAlert === "function") window.showAppAlert(text);
      return;
    }
    var backdrop = document.createElement("div");
    backdrop.className = "app-modal-backdrop";
    backdrop.setAttribute("role", "dialog");
    backdrop.setAttribute("aria-modal", "true");
    var modal = document.createElement("div");
    modal.className = "app-modal";
    var h = document.createElement("h3");
    h.textContent = title;
    var area = document.createElement("textarea");
    area.className = "fishtool-astrbot-copy-area";
    area.readOnly = true;
    area.value = text;
    var btn = document.createElement("button");
    btn.className = "btn btn-primary";
    btn.textContent = "关闭";
    modal.append(h, area, btn);
    backdrop.append(modal);
    var close = window.mountAppModal(backdrop);
    btn.addEventListener("click", close);
    backdrop.addEventListener("click", function (e) { if (e.target === backdrop) close(); });
    setTimeout(function () { area.focus(); area.select(); }, 0);
  }

  // ---------- 自诊报告导出 ----------
  if (typeof window.printReportHtml === "function") {
    window.printReportHtml = function (html) {
      var uidInput = document.getElementById("diagnosis-uid");
      var uid = uidInput ? String(uidInput.value || "").trim() : "";
      B.apiPost("_export/report", { html: html, uid: uid })
        .then(function (res) {
          if (!res || !res.file_id) throw new Error((res && res.detail) || "保存报告失败");
          return B.download("_export/file", { id: res.file_id }, res.filename);
        })
        .then(function () {
          toast("报告 HTML 已下载，用浏览器打开后可“打印 → 另存为 PDF”", "info", "导出报告");
        })
        .catch(function (e) {
          toast("导出失败: " + ((e && e.message) || e), "error", "导出失败");
        });
    };
  }

  // ---------- 日志导出 ----------
  if (typeof window.exportProblemLogs === "function") {
    window.exportProblemLogs = function () {
      var records = (typeof logRecords !== "undefined" && Array.isArray(logRecords)) ? logRecords : [];
      var fmt = typeof window.formatLogRecord === "function" ? window.formatLogRecord : function (r) { return JSON.stringify(r); };
      var picked = records.filter(function (r) { return ["WARNING", "ERROR", "CRITICAL"].indexOf(r.level) >= 0; });
      var text = picked.length ? picked.map(fmt).join("\n") : "暂无 WARNING / ERROR 日志";
      var name = "bili_ops_errors_" + new Date().toISOString().slice(0, 19).replace(/:/g, "-") + ".txt";
      B.apiPost("_export/text", { text: text, filename: name })
        .then(function (res) {
          if (!res || !res.file_id) throw new Error((res && res.detail) || "保存日志失败");
          return B.download("_export/file", { id: res.file_id }, res.filename);
        })
        .catch(function (e) {
          toast("导出失败: " + ((e && e.message) || e), "error", "导出失败");
        });
    };
  }

  // ---------- 复制单条日志 ----------
  if (typeof window.copyLogRecord === "function") {
    window.copyLogRecord = async function (index) {
      var rec = (typeof logRecords !== "undefined" && logRecords) ? logRecords[index] : null;
      var text = typeof window.formatLogRecord === "function" ? window.formatLogRecord(rec) : JSON.stringify(rec);
      try {
        await navigator.clipboard.writeText(text);
        toast("已复制", "info", "复制日志");
        return;
      } catch (e) { /* 沙箱通常拒绝剪贴板权限，退到 execCommand */ }
      var ok = false;
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand("copy");
        ta.remove();
      } catch (e) { ok = false; }
      if (ok) toast("已复制", "info", "复制日志");
      else showCopyDialog("请手动复制", text);
    };
  }

  // ---------- 外链 ----------
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (!/^https?:\/\//i.test(href)) return;
    e.preventDefault();
    e.stopPropagation();
    showCopyDialog("插件视图内无法打开新窗口，请复制链接到浏览器打开", a.href);
  }, true);
})();
