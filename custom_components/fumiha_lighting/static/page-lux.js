/* ==========================================================================
 * lux_balance page — <lux-balance-page>
 *
 * Served by the lux_balance integration and loaded in one of two ways:
 *   - inside the Fumiha Hub shell (dynamic import, kit.js already loaded);
 *   - as a standalone sidebar panel (HA loads this module directly, so the
 *     module bootstraps window.FUMIHA.kit itself from the vendored copy).
 * Both paths end in the same custom element; the only difference is which
 * tokens.css the stylesheet link points at (hub's versioned copy when the
 * shell set window.FUMIHA.tokensHref, our vendored copy otherwise).
 *
 * The element is defined SYNCHRONOUSLY at module evaluation (guarded), so a
 * kit bootstrap failure can never leave an undefined element rendering a
 * silent blank panel — the instance renders the failure via _fatal instead
 * (LX-F-007).
 *
 * Data: GET /api/fumiha_lighting/lux/overview (poll-driven). Writes go
 * through this integration's own REST views (target / calibrate) — the
 * number entity is an automation adapter, never the GUI backend.
 * ========================================================================== */

const SELF_VER = new URL(import.meta.url).search;
const LOCAL_TOKENS_HREF = "/fumiha_lighting/tokens.css" + SELF_VER;
const LOCAL_KIT_SRC = "/fumiha_lighting/kit.js" + SELF_VER;
const OVERVIEW_URL = "/api/fumiha_lighting/lux/overview";
const POLL_SLOW_MS = 5000;
const POLL_FAST_MS = 1000; // while any zone is calibrating
const FAIL_STREAK_ERROR_CARD = 3; // consecutive failures before the error card

async function ensureKit() {
  if (window.FUMIHA && window.FUMIHA.kit) return;
  await new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = LOCAL_KIT_SRC;
    s.onload = resolve;
    s.onerror = () => reject(new Error("kit.js 加载失败（" + s.src + "）"));
    document.head.appendChild(s);
  });
  if (!window.FUMIHA || !window.FUMIHA.kit) {
    throw new Error("kit.js 加载后 window.FUMIHA.kit 缺失");
  }
}

const MODE_LABELS = {
  idle: { text: "空闲", tone: "" },
  active: { text: "闭环中", tone: "ok" },
  calibrating: { text: "校准中", tone: "warn" },
  passthrough: { text: "手动直通", tone: "warn" },
};

const fmtTime = (d) =>
  d.toLocaleTimeString("zh-Hans", { hour12: false });

class LuxBalancePage extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._zones = [];
    this._payloadJson = ""; // render gate: unchanged payload never rebuilds
    this._rendered = false;
    this._stopPoll = null;
    this._pollMs = 0;
    this._confirmingCalibrate = false;
    this._targetDrafts = {}; // entry_id -> 未应用的输入草稿（重渲染保留，UX-M-01）
    this._conn = "idle"; // idle | ok | err
    this._lastOk = null; // Date of last successful overview
    this._failStreak = 0;
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    // Kit may not have finished booting (standalone path) — the initial
    // refresh happens in _boot, which runs after the ready promise.
    if (first && this.isConnected && window.FUMIHA && window.FUMIHA.kit) {
      this._refresh();
    }
    // Later pushes carry entity churn; our data is poll-driven.
  }

  get hass() {
    return this._hass;
  }

  set route(_value) {
    // No path routing: the shell's #pn= deep link lands here directly.
  }

  connectedCallback() {
    // A hass assignment before upgrade becomes an own property and shadows
    // the setter (P0 lesson) — re-route it.
    if (Object.prototype.hasOwnProperty.call(this, "hass")) {
      const early = this.hass;
      delete this.hass;
      this.hass = early;
    }
    // Kit may still be booting (standalone path); boot or fail visibly.
    window.__luxBalancePageReady.then(
      () => {
        if (this.isConnected) this._boot();
      },
      (err) => this._fatal(String(err && err.message ? err.message : err))
    );
  }

  disconnectedCallback() {
    if (this._stopPoll) this._stopPoll();
    this._stopPoll = null;
  }

  _fatal(message) {
    // 错误必须响: no kit → no errorCard helper; bare shadow message.
    this.shadowRoot.innerHTML =
      '<div style="font:13px sans-serif;padding:24px;color:#c62828">亮度均衡页面加载失败：' +
      String(message).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
      ) +
      "</div>";
  }

  _boot() {
    const kit = window.FUMIHA.kit;
    if (this._stopPoll) {
      this._stopPoll();
      this._stopPoll = null;
    }
    this._pollMs = 0;
    // Reset render-gate state: reconnect reuses the same element instance,
    // and stale gates would leave the fresh DOM blank until data changes —
    // same lesson as the shell's FXH-F-001 (LX-F-003). An in-flight confirm
    // dialog died with the old DOM; its promise never settles, so the flag
    // must be cleared too or every calibrate button stays dead.
    this._rendered = false;
    this._payloadJson = "";
    this._zones = [];
    this._confirmingCalibrate = false;
    this._targetDrafts = {};

    const root = this.shadowRoot;
    root.innerHTML = "";
    // Prefer the shell's versioned tokens (set before pages boot); fall back
    // to our vendored copy in standalone mode.
    if (!window.FUMIHA.tokensHref) window.FUMIHA.tokensHref = LOCAL_TOKENS_HREF;
    kit.addTokens(root);

    this._lamp = kit.el("span", { class: "fx-lamp", title: "数据连接" });
    this._connText = kit.el("span", { text: "连接中…" });
    this._statusRow = kit.el(
      "div",
      { class: "cx-status", style: { marginBottom: "10px" } },
      this._lamp,
      this._connText
    );
    this._body = kit.el("div", { class: "fx-body" });
    root.append(
      kit.el("div", { class: "fx-page" }, this._statusRow, this._body)
    );
    this._refresh();
    this._setPoll(POLL_SLOW_MS);
  }

  /* ---- polling ------------------------------------------------------------ */

  _setPoll(ms) {
    if (this._pollMs === ms && this._stopPoll) return;
    if (this._stopPoll) this._stopPoll();
    this._pollMs = ms;
    this._stopPoll = window.FUMIHA.kit.startPolling(() => this._refresh(), ms);
  }

  async _refresh() {
    const kit = window.FUMIHA && window.FUMIHA.kit;
    if (!kit || !this._hass || !this.isConnected) return;
    let zones;
    try {
      const payload = await kit.api(this._hass).get(OVERVIEW_URL);
      // The element may have been detached while the request was in flight:
      // proceeding would re-arm a timer no disconnect can ever stop again
      // (LX-F-004 zombie poll).
      if (!this.isConnected) return;
      zones = payload && Array.isArray(payload.zones) ? payload.zones : null;
      if (zones === null) throw new Error("响应格式非法（缺 zones）");
      this._conn = "ok";
      this._lastOk = new Date();
      this._failStreak = 0;
    } catch (err) {
      if (!this.isConnected) return;
      this._conn = "err";
      this._failStreak += 1;
      console.error("[lux-balance] overview poll failed:", err);
      this._renderConn();
      if (!this._rendered || this._failStreak >= FAIL_STREAK_ERROR_CARD) {
        this._renderError(String(err && err.message ? err.message : err));
      }
      return;
    }

    const json = JSON.stringify(zones);
    if (json !== this._payloadJson || !this._rendered) {
      this._payloadJson = json;
      this._zones = zones;
      this._render();
    }
    this._renderConn();
    const calibrating = zones.some((z) => z.mode === "calibrating");
    this._setPoll(calibrating ? POLL_FAST_MS : POLL_SLOW_MS);
  }

  /* ---- rendering ----------------------------------------------------------- */

  _renderConn() {
    if (!this._lamp) return;
    this._lamp.className =
      "fx-lamp " + (this._conn === "ok" ? "ok" : this._conn === "err" ? "err breath" : "");
    this._lamp.title =
      this._conn === "ok" ? "数据正常" : this._conn === "err" ? "数据获取失败" : "未连接";
    this._connText.textContent = this._lastOk
      ? (this._conn === "ok" ? "已连接 · 更新于 " : "获取失败 · 数据截至 ") +
        fmtTime(this._lastOk)
      : "连接中…";
  }

  _renderError(message) {
    const kit = window.FUMIHA.kit;
    if (!this._body) return;
    // Mark the gate dirty: a recovered poll whose payload is byte-identical
    // to the pre-failure one must still re-render, or the error card sticks
    // around forever next to a green status lamp (LX-F-020).
    this._rendered = false;
    this._body.replaceChildren(kit.errorCard(message, () => this._refresh()));
  }

  _render() {
    const kit = window.FUMIHA.kit;
    if (!this._body) return;
    this._rendered = true;
    if (!this._zones.length) {
      this._body.replaceChildren(
        kit.el("div", {
          class: "fx-empty",
          text: "尚无区域 — 先在集成里添加一个恒照度区域",
        })
      );
      return;
    }
    const anyCalibrating = this._zones.some((z) => z.mode === "calibrating");
    // UX-M-01: 重建会新建目标照度输入框——先记焦点位置，重建后恢复，
    // 避免轮询落地瞬间清掉用户正在输入的值（草稿值由 _targetDrafts 承载）。
    const active = this.shadowRoot.activeElement;
    const focusZone = active?.closest?.("[data-zone]")?.dataset.zone ?? null;
    const focusWasTarget = active?.getAttribute?.("aria-label") === "目标照度（lux）";
    this._body.replaceChildren(
      ...this._zones.map((z) => this._zoneCard(z, anyCalibrating))
    );
    if (focusZone && focusWasTarget) {
      const again = this._body.querySelector(
        `[data-zone="${focusZone}"] input[aria-label="目标照度（lux）"]`
      );
      if (again) {
        again.focus();
        const n = again.value.length;
        try {
          again.setSelectionRange(n, n);
        } catch {
          // number 输入无选区语义（HTML 规范抛 InvalidStateError）——焦点已恢复即可
        }
      }
    }
  }

  _zoneCard(z, anyCalibrating) {
    const kit = window.FUMIHA.kit;
    const mode = MODE_LABELS[z.mode] || { text: z.mode, tone: "" };
    const chips = [
      kit.el(
        "span",
        { class: "cx-chip " + (mode.tone ? "tone-" + mode.tone : "") },
        kit.el("span", { class: "fx-lamp " + (mode.tone || "") }),
        mode.text +
          (z.mode === "calibrating" && z.cal_progress
            ? " · " + z.cal_progress
            : "")
      ),
    ];
    if (z.lux_stale) {
      chips.push(
        kit.el("span", {
          class: "cx-chip tone-err",
          text:
            "照度数据不可信（不可用或超 " +
            (z.stale_limit_min ?? 30) +
            " 分钟未上报）",
        })
      );
    }
    if (z.curve && z.curve.source === "manual") {
      chips.push(kit.el("span", { class: "cx-tag", text: "手动系数曲线" }));
    }
    if (z.ambient_lux_est != null) {
      chips.push(
        kit.el("span", {
          class: "cx-chip",
          text: "环境光 ≈ " + z.ambient_lux_est + " lx",
        })
      );
    }

    const children = [
      kit.el("h2", { text: z.zone_name }),
      kit.el("div", { class: "cx-status" }, chips),
    ];
    if (z.cal_last_error && z.mode !== "calibrating") {
      // UX-M-02: 校准中止此前只进 HA 日志，页面观感与"完成"一致。
      children.push(
        kit.el("div", {
          class: "cx-note warn",
          style: { marginTop: "8px" },
          text: "上次校准未能完成：" + z.cal_last_error + "（可排查传感器后重试）",
        })
      );
    }
    children.push(
      this._targetRow(z, anyCalibrating),
      this._curveSection(z),
      this._decisionsSection(z)
    );
    return kit.el("div", { class: "fx-card", "data-zone": z.entry_id }, ...children);
  }

  _targetRow(z, anyCalibrating) {
    const kit = window.FUMIHA.kit;
    if (z.mode === "calibrating") {
      const pct = Math.max(0, Math.min(100, z.commanded_pct ?? 0));
      return kit.el("div", { style: { marginTop: "10px" } },
        kit.el("div", { class: "cx-bar tone-warn" },
          kit.el("div", { class: "fill", style: { width: pct + "%" } })),
        kit.el("div", {
          class: "cx-note warn",
          style: { marginTop: "8px" },
          text: "校准进行中（约 15~40 分钟，上报不畅时可能接近 1 小时），期间本区闭环暂停，其他区域闭环自动让路。",
        })
      );
    }
    const min = z.target_min ?? 10;
    const max = z.target_max ?? 500;
    const step = z.target_step ?? 5;
    const input = kit.el("input", {
      class: "cx-input",
      type: "number",
      min: String(min),
      max: String(max),
      step: String(step),
      value: String(this._targetDrafts[z.entry_id] ?? z.target_lux),
      "aria-label": "目标照度（lux）",
    });
    const submit = async () => {
      const v = parseFloat(input.value);
      if (!Number.isFinite(v) || v < min || v > max) {
        kit.toast(this.shadowRoot, "目标照度需在 " + min + "–" + max + " lx 之间", {
          isError: true,
        });
        return;
      }
      try {
        // Own-REST write: straight to runtime.set_target (the number
        // entity follows via the dispatcher — adapter, not backend).
        await kit.api(this._hass).post("/api/fumiha_lighting/lux/target", {
          entry_id: z.entry_id,
          value: v,
        });
        delete this._targetDrafts[z.entry_id];
        kit.ack(this.shadowRoot, { ok: true, label: "应用目标 " + v + " lx" });
      } catch (err) {
        kit.ack(this.shadowRoot, {
          ok: false,
          label: "应用目标",
          reason: String(err && err.message ? err.message : err),
        });
      }
    };
    input.addEventListener("input", () => {
      // UX-M-01: 草稿随输入更新，轮询重渲染后由 value 恢复
      this._targetDrafts[z.entry_id] = input.value;
    });
    input.addEventListener("keydown", (ev) => {
      // FL-UX-06: 回车即应用
      if (ev.key === "Enter") {
        ev.preventDefault();
        submit();
      }
    });
    const apply = kit.el("button", {
      class: "cx-btn primary",
      text: "应用目标",
      onclick: submit,
    });
    return kit.el("div", { class: "cx-status", style: { marginTop: "10px" } },
      kit.el("span", { class: "cx-metric" },
        kit.el("span", { class: "v", text: (z.target_lux ?? "—") + " lx" }),
        kit.el("span", { class: "l", text: "目标照度" })),
      input,
      apply,
      this._calibrateButton(z, anyCalibrating)
    );
  }

  _calibrateButton(z, anyCalibrating) {
    const kit = window.FUMIHA.kit;
    const busyElsewhere = anyCalibrating && z.mode !== "calibrating";
    const btn = kit.el("button", {
      class: "cx-btn",
      "data-action": "calibrate",
      text: z.curve ? "重新校准" : "开始校准",
      // First UI line of defence for the calibration mutex (LX-F-002): the
      // service now raises on skip, but the user should never reach it.
      disabled: busyElsewhere ? true : null,
      title: busyElsewhere ? "另一区域校准进行中，同一卫生间一次只能校准一个区" : null,
      onclick: async () => {
        if (this._confirmingCalibrate) return;
        this._confirmingCalibrate = true;
        try {
          // FL-UX-02: 心跳型传感器（数十分钟一报）跑不完 60s/档 的采样窗，
          // 启动前把实测新鲜度亮给用户，避免白等几十分钟后只得到一条日志。
          let staleNote = "";
          const luxState = this._hass?.states?.[z.lux_entity];
          const lc = luxState?.last_changed ? new Date(luxState.last_changed) : null;
          const ageMin = lc ? Math.round((Date.now() - lc.getTime()) / 60000) : null;
          if (ageMin != null && ageMin >= 10) {
            staleNote =
              " ⚠ 该传感器已 " + ageMin +
              " 分钟未上报（上报周期可能长达数十分钟），校准大概率无法完成，建议先换用上报更频繁的照度传感器。";
          }
          const yes = await kit.confirmDialog(this.shadowRoot, {
            title: "校准 " + z.zone_name,
            text: "灯将自动 0→100→0 扫描，约 15~40 分钟（传感器上报不畅时可能接近 1 小时），期间该区不可用。现在开始？" + staleNote,
            confirmLabel: "开始校准",
          });
          if (!yes) return;
          const res = await kit.api(this._hass).post(
            "/api/fumiha_lighting/lux/calibrate",
            { entry_id: z.entry_id }
          );
          if (res.started === false) {
            throw new Error(res.reason || "校准未能启动");
          }
          kit.ack(this.shadowRoot, { ok: true, label: "校准已启动" });
        } catch (err) {
          kit.ack(this.shadowRoot, {
            ok: false,
            label: "校准",
            reason: String(err && err.message ? err.message : err),
          });
        } finally {
          this._confirmingCalibrate = false;
        }
      },
    });
    return btn;
  }

  _curveSection(z) {
    const kit = window.FUMIHA.kit;
    if (!z.curve || !z.curve.points || z.curve.points.length < 2) {
      return kit.el("div", { style: { marginTop: "12px" } },
        kit.el("div", {
          class: "cx-note",
          text: "尚未标定：先运行一次校准，页面会出现「亮度 % → 照度增量」标定曲线。",
        })
      );
    }
    const c = z.curve;
    const W = 560, H = 180, PAD_L = 46, PAD_R = 12, PAD_T = 12, PAD_B = 26;
    const pts = c.points;
    // Server-side cleaning can null the scalars while the point list
    // survived (LX-F-026) — recompute from the points rather than plotting
    // garbage ("null lx" chips, everything squeezed out of the viewBox).
    const maxLux = Math.max(
      1,
      Number.isFinite(c.max_lux) ? c.max_lux : Math.max(...pts.map((p) => p[1]))
    );
    const x = (p) => PAD_L + (p / 100) * (W - PAD_L - PAD_R);
    const y = (v) => H - PAD_B - (v / maxLux) * (H - PAD_T - PAD_B);
    const poly = pts
      .map(([p, v]) => x(p).toFixed(1) + "," + y(v).toFixed(1))
      .join(" ");
    const NS = "http://www.w3.org/2000/svg";
    const mk = (tag, attrs, text) => {
      const n = document.createElementNS(NS, tag);
      for (const k of Object.keys(attrs)) n.setAttribute(k, attrs[k]);
      if (text != null) n.textContent = text;
      return n;
    };
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "标定曲线：亮度百分比对照度增量");
    svg.style.width = "100%";
    svg.style.height = "auto";
    svg.append(
      mk("line", { x1: PAD_L, y1: H - PAD_B, x2: W - PAD_R, y2: H - PAD_B, stroke: "var(--fx-edge)", "stroke-width": "1" }),
      mk("line", { x1: PAD_L, y1: PAD_T, x2: PAD_L, y2: H - PAD_B, stroke: "var(--fx-edge)", "stroke-width": "1" }),
      mk("text", { x: PAD_L, y: H - 8, "text-anchor": "start", fill: "var(--fx-sub)", "font-size": "10" }, "0%"),
      mk("text", { x: W - PAD_R, y: H - 8, "text-anchor": "end", fill: "var(--fx-sub)", "font-size": "10" }, "100%"),
      mk("text", { x: PAD_L - 6, y: PAD_T + 4, "text-anchor": "end", fill: "var(--fx-sub)", "font-size": "10" }, maxLux + " lx"),
      mk("text", { x: PAD_L - 6, y: H - PAD_B + 4, "text-anchor": "end", fill: "var(--fx-sub)", "font-size": "10" }, "0"),
      mk("polyline", { points: poly, fill: "none", stroke: "var(--fx-accent)", "stroke-width": "2" })
    );
    for (const [p, v] of pts) {
      const dot = mk("circle", {
        cx: x(p).toFixed(1), cy: y(v).toFixed(1), r: "3.5", fill: "var(--fx-accent)",
      });
      dot.appendChild(mk("title", {}, p + "% → +" + v + " lx"));
      svg.append(dot);
    }
    const chips = [kit.el("span", { class: "cx-chip", text: "标定曲线" })];
    if (Number.isFinite(c.max_lux)) {
      chips.push(kit.el("span", { class: "cx-chip", text: "最大增量 " + c.max_lux + " lx" }));
    }
    if (Number.isFinite(c.slope_20_80)) {
      chips.push(kit.el("span", { class: "cx-chip", text: "20–80% 斜率 " + c.slope_20_80 + " lx/%" }));
    }
    return kit.el("div", { style: { marginTop: "12px" } },
      kit.el("div", { class: "cx-status", style: { marginBottom: "6px" } }, chips),
      svg
    );
  }

  _decisionsSection(z) {
    const kit = window.FUMIHA.kit;
    const lines = z.decisions || [];
    const body = lines.length
      ? kit.el("div", { style: { maxHeight: "220px", overflowY: "auto" } },
          kit.el("table", { class: "cx-table" },
            kit.el("tbody", {},
              lines.map((line) =>
                kit.el("tr", {},
                  kit.el("td", {
                    text: line,
                    style: {
                      fontFamily: "var(--fx-mono)",
                      whiteSpace: "pre-wrap",
                    },
                  }))))
          ))
      : kit.el("div", {
          class: "cx-note",
          text: "暂无决策记录（闭环未运行或尚无变化）。",
        });
    return kit.el("div", { style: { marginTop: "12px" } },
      kit.el("h2", { text: "最近决策" }),
      body
    );
  }
}

/* Defined synchronously and guarded (a shell re-import under a new version
 * re-evaluates this module): a kit failure must still yield a defined
 * element that can render its own failure (LX-F-007). The ready promise is
 * assigned BEFORE the define call — upgrading an in-DOM element fires its
 * connectedCallback synchronously, and that callback reads the promise. */
window.__luxBalancePageReady = ensureKit();
if (!customElements.get("lux-balance-page")) {
  customElements.define("lux-balance-page", LuxBalancePage);
}
