/* ==========================================================================
   Fumiha Hub frontend kit — shared helpers for the shell and all pages.

   Loaded as a classic script that installs `window.FUMIHA` (the Codex boot
   contract): the shell injects this file itself with the hub's ?ver= cache
   buster, and pages read window.FUMIHA at call time — so page modules never
   need to know the hub version and no build step is required.

   The fetch wrapper is the hardened superset of the WHA panel's _api():
   timeout via AbortController, HTTP status preserved on errors, non-JSON
   bodies never fabricate data, and aborts always surface as transient
   (the lessons recorded as WHA-F-140/204/222/236/241).
   ========================================================================== */
(function () {
  "use strict";

  var F = (window.FUMIHA = window.FUMIHA || {});

  /* ---- DOM ------------------------------------------------------------ */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Safe DOM builder: attrs through setAttribute, strings through
   * textContent — no HTML string interpolation anywhere. */
  function el(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
        else if (k.slice(0, 2) === "on" && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (v === true) node.setAttribute(k, "");
        else node.setAttribute(k, v);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      appendChild(node, arguments[i]);
    }
    return node;
  }

  function appendChild(node, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(function (c) { appendChild(node, c); });
      return;
    }
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  /* ---- i18n ------------------------------------------------------------ */

  /* dict shape: { key: { zh: "...", en: "..." } }; language comes from
   * F.lang, kept current by the shell from hass.language. Falls back
   * zh -> en -> key, then interpolates {name} placeholders. */
  function makeT(dict) {
    return function (key, vars) {
      var entry = dict[key];
      var lang = F.lang === "en" ? "en" : "zh";
      var text = entry ? (entry[lang] != null ? entry[lang] : entry.zh) : key;
      if (text == null) text = key;
      if (vars) {
        Object.keys(vars).forEach(function (k) {
          var re = new RegExp("\\{" + k + "\\}", "g");
          // Function replacement: a string second argument would interpret
          // $&, $` etc. inside the value as replacement patterns and eat
          // content (FXH-F-017).
          text = text.replace(re, function () { return String(vars[k]); });
        });
      }
      return text;
    };
  }

  /* ---- REST ------------------------------------------------------------ */

  var API_TIMEOUT_MS = 10000;

  function httpError(message, status, extra) {
    var e = new Error(message);
    e.status = status;
    if (extra) Object.assign(e, extra);
    return e;
  }

  /* call(hass, path, options) -> parsed payload; throws Error with
   * .status on any failure. */
  async function call(hass, path, options) {
    if (!hass || typeof hass.fetchWithAuth !== "function") {
      throw httpError("hass not ready", 0, { transient: true });
    }
    options = options || {};
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, API_TIMEOUT_MS);
    var res;
    try {
      res = await hass.fetchWithAuth(path, {
        method: options.method || "GET",
        headers: options.body != null ? { "Content-Type": "application/json" } : undefined,
        body: options.body != null ? JSON.stringify(options.body) : undefined,
        signal: ctrl.signal,
      });
    } catch (err) {
      // WHA-F-140/241: an aborted/failed fetch is transient by definition —
      // it must never look like a parse error or busy-guards latch forever.
      var aborted = err && (err.name === "AbortError" || ctrl.signal.aborted);
      throw httpError(aborted ? "请求超时" : "网络错误", 0, { transient: true });
    } finally {
      clearTimeout(timer);
    }

    var text = "";
    try {
      text = await res.text();
    } catch (err) {
      // Body read failed after a successful status: treat like an abort,
      // not like an empty payload (WHA-F-241).
      throw httpError("响应读取失败", res.status || 0, { transient: true });
    }

    var payload = null;
    var parseFailed = false;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (err) {
        parseFailed = true;
      }
    }

    if (!res.ok) {
      // WHA-F-222: keep the HTTP status; WHA-F-236: never return a
      // fabricated {} on error paths.
      var msg =
        payload && !parseFailed && (payload.error || payload.message || payload.reason);
      throw httpError(
        msg || "HTTP " + res.status,
        res.status,
        parseFailed && text ? { detail: text.slice(0, 200) } : undefined
      );
    }

    if (payload && typeof payload === "object" && payload.ok === false) {
      // Consumer REST views use the {ok:false, error} shell convention.
      throw httpError(payload.error || "请求失败", res.status);
    }
    return payload;
  }

  function api(hass) {
    return {
      get: function (path) { return call(hass, path); },
      post: function (path, body) {
        return call(hass, path, { method: "POST", body: body == null ? {} : body });
      },
      put: function (path, body) {
        return call(hass, path, { method: "PUT", body: body == null ? {} : body });
      },
      del: function (path) { return call(hass, path, { method: "DELETE" }); },
    };
  }

  /* ---- feedback --------------------------------------------------------- */

  /* Toast: remove-old-create-new so messages never queue invisibly.
   * opts: { isError, ms }. Returns the toast element. */
  function toast(root, message, opts) {
    opts = opts || {};
    var old = root.querySelector(".fx-toast");
    if (old) old.remove();
    var t = el("div", { class: "fx-toast" + (opts.isError ? " err" : ""), role: "status" }, message);
    root.appendChild(t);
    var ms = opts.ms || (opts.isError ? 8000 : 2600);
    setTimeout(function () { t.remove(); }, ms);
    return t;
  }

  /* Codex ack semantics: ok = short-lived, fail = long red. */
  function ack(root, result) {
    var label = result.label || "操作";
    if (result.ok) return toast(root, label + " ✓");
    return toast(root, label + " 失败" + (result.reason ? "：" + result.reason : ""), { isError: true });
  }

  /* Native <dialog> confirm; resolves true/false. Returns a Promise. */
  function confirmDialog(root, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = document.createElement("dialog");
      dlg.className = "fx-dialog";
      var done = false;
      function finish(val) {
        if (done) return;
        done = true;
        dlg.close();
        resolve(val);
      }
      var cancelBtn = el("button", { class: "cx-btn", onclick: function () { finish(false); } },
        opts.cancelLabel || "取消");
      var okBtn = el("button", {
        class: "cx-btn " + (opts.danger ? "danger" : "primary"),
        onclick: function () { finish(true); },
      }, opts.confirmLabel || "确认");
      dlg.append(
        el("div", { style: { fontWeight: "600", marginBottom: "8px" } }, opts.title || "确认"),
        el("div", { style: { fontSize: "12.5px", marginBottom: "14px" } }, opts.text || ""),
        el("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end" } },
          cancelBtn, okBtn)
      );
      dlg.addEventListener("cancel", function (e) { e.preventDefault(); finish(false); });
      dlg.addEventListener("close", function () {
        if (!done) { done = true; resolve(false); }
        dlg.remove();
      });
      root.appendChild(dlg);
      if (typeof dlg.showModal === "function") dlg.showModal();
      else finish(false); // no dialog support: refuse the action loudly
      okBtn.focus();
    });
  }

  /* ---- polling ----------------------------------------------------------- */

  /* setInterval wrapper that skips ticks while the document is hidden.
   * Returns a stop function. */
  function startPolling(fn, ms) {
    var id = setInterval(function () {
      if (!document.hidden) fn();
    }, ms);
    return function () { clearInterval(id); };
  }

  /* ---- styles & elements -------------------------------------------------- */

  function addTokens(root) {
    root.appendChild(el("link", { rel: "stylesheet", href: F.tokensHref || "/fumiha_hub/tokens.css" }));
  }

  /* customElements.define that survives module re-execution (a page module
   * fetched under a new ?ver= runs again; re-defining the same name would
   * throw and break the import). The caller must handle the `false` return
   * when a version upgrade actually matters — see the shell's reload card. */
  function defineOnce(name, ctor) {
    if (customElements.get(name)) {
      console.warn("[fumiha-hub] element <" + name + "> already defined; skipping re-definition");
      return false;
    }
    customElements.define(name, ctor);
    return true;
  }

  /* Error card: message + retry callback. 错误必须响 — never a blank area. */
  function errorCard(message, onRetry, retryLabel) {
    var card = el("div", { class: "fx-error", role: "alert" },
      el("div", { style: { fontWeight: "600" } }, "页面加载失败"),
      el("div", { class: "msg" }, message),
      onRetry ? el("button", { class: "cx-btn primary", onclick: onRetry }, retryLabel || "重试") : null
    );
    return card;
  }

  F.kit = {
    esc: esc,
    el: el,
    makeT: makeT,
    api: api,
    toast: toast,
    ack: ack,
    confirmDialog: confirmDialog,
    startPolling: startPolling,
    addTokens: addTokens,
    defineOnce: defineOnce,
    errorCard: errorCard,
  };
})();
