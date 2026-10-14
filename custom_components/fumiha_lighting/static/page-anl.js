/* ==========================================================================
 * auto_night_light page — <anl-page>
 *
 * Same dual-mode architecture as the lux_balance page (v0.3.0): served by
 * the integration, rendered inside the Fumiha Hub shell when present or as
 * a standalone sidebar panel otherwise (module bootstraps the vendored kit
 * itself). The element is defined synchronously and guarded so a kit
 * bootstrap failure still yields a defined element that renders its own
 * failure.
 *
 * This integration has ZERO entity platforms — this page is its only
 * runtime-visual surface. Data: GET /api/auto_night_light/snapshot
 * (poll-driven). Writes: POST /api/fumiha_lighting/anl/trigger.
 * ========================================================================== */

const SELF_VER = new URL(import.meta.url).search;
const LOCAL_TOKENS_HREF = "/fumiha_lighting/tokens.css" + SELF_VER;
const LOCAL_KIT_SRC = "/fumiha_lighting/kit.js" + SELF_VER;
const SNAPSHOT_URL = "/api/fumiha_lighting/anl/snapshot";
const POLL_MS = 5000;

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

/* State machine chip tone + label (9 states from manager.LightState). */
const STATE_LABELS = {
  idle: { text: "等待锚点", tone: "" },
  turn_on_pending: { text: "监听开灯", tone: "warn" },
  pending: { text: "待检查", tone: "warn" },
  matched: { text: "已符合", tone: "ok" },
  setting: { text: "下发中", tone: "warn" },
  verified: { text: "已验证", tone: "ok" },
  mismatch: { text: "偏离", tone: "err" },
  offline: { text: "实体离线", tone: "err" },
  skipped_off: { text: "灯关·不接管", tone: "" },
};

const fmtTime = (d) => d.toLocaleTimeString("zh-Hans", { hour12: false });
const fmtAnchor = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("zh-Hans", { hour12: false });
};
const MODE_LABELS = { night: "夜间", day: "日间" };
const modeLabel = (mode) => {
  if (!mode) return "无（灯不接管）";
  if (MODE_LABELS[mode]) return MODE_LABELS[mode];
  // extra_N is 0-based internally but 1-based in config_flow / README.
  if (String(mode).startsWith("extra_")) {
    return "额外时段 " + (Number(String(mode).slice(6)) + 1);
  }
  return mode;
};

class AnlPage extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._managers = [];
    this._payloadJson = "";
    this._rendered = false;
    this._stopPoll = null;
    this._pollMs = 0;
    this._confirmingTrigger = false;
    this._conn = "idle";
    this._lastOk = null;
    this._failStreak = 0;
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first && this.isConnected && window.FUMIHA && window.FUMIHA.kit) {
      this._refresh();
    }
  }

  get hass() {
    return this._hass;
  }

  set route(_value) {
    // No path routing: the shell's #pn= deep link lands here directly.
  }

  connectedCallback() {
    if (Object.prototype.hasOwnProperty.call(this, "hass")) {
      const early = this.hass;
      delete this.hass;
      this.hass = early;
    }
    window.__anlPageReady.then(
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
    this.shadowRoot.innerHTML =
      '<div style="font:13px sans-serif;padding:24px;color:#c62828">夜灯助手页面加载失败：' +
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
    // Reconnect resets the render gate (same lesson as shell FXH-F-001 /
    // lux LX-F-003) and any dead confirm-dialog flag.
    this._rendered = false;
    this._payloadJson = "";
    this._managers = [];
    this._confirmingTrigger = false;

    const root = this.shadowRoot;
    root.innerHTML = "";
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
    root.append(kit.el("div", { class: "fx-page" }, this._statusRow, this._body));
    this._refresh();
    this._setPoll(POLL_MS);
  }

  _setPoll(ms) {
    if (this._pollMs === ms && this._stopPoll) return;
    if (this._stopPoll) this._stopPoll();
    this._pollMs = ms;
    this._stopPoll = window.FUMIHA.kit.startPolling(() => this._refresh(), ms);
  }

  async _refresh() {
    const kit = window.FUMIHA && window.FUMIHA.kit;
    if (!kit || !this._hass || !this.isConnected) return;
    let managers;
    try {
      const payload = await kit.api(this._hass).get(SNAPSHOT_URL);
      if (!this.isConnected) return; // no zombie re-arm (lux LX-F-004)
      managers =
        payload && Array.isArray(payload.managers) ? payload.managers : null;
      if (managers === null) throw new Error("响应格式非法（缺 managers）");
      this._conn = "ok";
      this._lastOk = new Date();
      this._failStreak = 0;
    } catch (err) {
      if (!this.isConnected) return;
      this._conn = "err";
      this._failStreak += 1;
      console.error("[anl] snapshot poll failed:", err);
      this._renderConn();
      if (!this._rendered || this._failStreak >= 3) {
        this._renderError(String(err && err.message ? err.message : err));
      }
      return;
    }
    const json = JSON.stringify(managers);
    if (json !== this._payloadJson || !this._rendered) {
      this._payloadJson = json;
      this._managers = managers;
      this._render();
    }
    this._renderConn();
  }

  _renderConn() {
    if (!this._lamp) return;
    this._lamp.className =
      "fx-lamp " +
      (this._conn === "ok" ? "ok" : this._conn === "err" ? "err breath" : "");
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
    this._rendered = false; // a recovered poll must re-render even on an identical payload
    this._body.replaceChildren(kit.errorCard(message, () => this._refresh()));
  }

  _render() {
    const kit = window.FUMIHA.kit;
    if (!this._body) return;
    this._rendered = true;
    if (!this._managers.length) {
      this._body.replaceChildren(
        kit.el("div", {
          class: "fx-empty",
          text: "尚无配置 — 先在集成里添加自动夜灯",
        })
      );
      return;
    }
    this._body.replaceChildren(...this._managers.map((m) => this._managerCard(m)));
  }

  _managerCard(m) {
    const kit = window.FUMIHA.kit;
    const modeChip =
      m.current_mode != null
        ? kit.el(
            "span",
            { class: "cx-chip tone-ok" },
            kit.el("span", { class: "fx-lamp ok" }),
            "当前：" + modeLabel(m.current_mode)
          )
        : kit.el("span", { class: "cx-chip", text: "当前：无锚点生效" });
    const anchorChips = [
      modeChip,
      kit.el("span", {
        class: "cx-chip",
        text: "下次锚点 " + fmtAnchor(m.next_anchor),
      }),
      kit.el("button", {
        class: "cx-btn primary",
        "data-action": "trigger",
        // The service triggers EVERY entry (no target schema) — the label
        // must not imply per-card scope (ANL-F-007).
        text: "立即触发（全部条目）",
        title: "对所有已配置的夜灯条目执行锚点归位",
        onclick: async () => {
          if (this._confirmingTrigger) return;
          this._confirmingTrigger = true;
          try {
            // FL-UX-03: 真控灯的全局动作，与 lux 校准同判据——先确认。
            const yes = await kit.confirmDialog(this.shadowRoot, {
              title: "立即触发",
              text: "将立即对所有夜灯条目执行锚点归位，灯会切到当前应处的状态。执行？",
              confirmLabel: "执行",
            });
            if (!yes) return;
            await kit.api(this._hass).post("/api/fumiha_lighting/anl/trigger", {});
            kit.ack(this.shadowRoot, { ok: true, label: "立即触发已执行" });
          } catch (err) {
            kit.ack(this.shadowRoot, {
              ok: false,
              label: "立即触发",
              reason: String(err && err.message ? err.message : err),
            });
          } finally {
            this._confirmingTrigger = false;
          }
        },
      }),
    ];

    const summaryBits = [
      "入夜 " + (m.start_mode === "sunset" ? "日落" : m.start_mode === "sunrise" ? "日出" : "固定"),
      "天亮 " + (m.end_mode === "sunrise" ? "日出" : m.end_mode === "sunset" ? "日落" : "固定"),
    ];
    if (m.transition_enabled) summaryBits.push("过渡带开启");
    if (m.day_enabled) summaryBits.push("日间时段");
    if (m.respect_manual) summaryBits.push("尊重手动");
    const summary = kit.el(
      "div",
      { class: "cx-status", style: { marginTop: "8px" } },
      summaryBits.map((t) => kit.el("span", { class: "cx-tag", text: t }))
    );

    const lightCards = (m.lights || []).map((lt) => this._lightCard(lt));
    const anchorRows = (m.anchors || []).map((a) =>
      kit.el("tr", {},
        kit.el("td", { class: "num", text: a.time }),
        kit.el("td", { text: modeLabel(a.mode) }))
    );

    return kit.el("div", { class: "fx-card", "data-manager": m.entry_id },
      kit.el("h2", { text: m.title }),
      kit.el("div", { class: "cx-status" }, anchorChips),
      summary,
      kit.el("div", { class: "fx-grid", style: { marginTop: "12px" } }, lightCards),
      kit.el("div", { style: { marginTop: "12px" } },
        kit.el("h2", { text: "锚点时刻表" }),
        (m.anchors || []).length
          ? kit.el("table", { class: "cx-table" },
              kit.el("thead", {},
                kit.el("tr", {},
                  kit.el("th", { text: "时刻" }),
                  kit.el("th", { text: "时段" }))),
              kit.el("tbody", {}, anchorRows))
          : kit.el("div", { class: "cx-note", text: "无锚点。" }))
    );
  }

  _lightCard(lt) {
    const kit = window.FUMIHA.kit;
    const st = STATE_LABELS[lt.state] || { text: lt.state, tone: "" };
    const chips = [
      kit.el(
        "span",
        { class: "cx-chip " + (st.tone ? "tone-" + st.tone : "") },
        kit.el("span", { class: "fx-lamp " + (st.tone || "") }),
        st.text
      ),
    ];
    if (lt.manual_override) {
      chips.push(
        kit.el("span", { class: "cx-chip tone-warn", text: "手动接管" })
      );
    }
    if (lt.mismatch_steps > 0) {
      chips.push(
        kit.el("span", {
          class: "cx-chip tone-err",
          text: "连续偏离 " + lt.mismatch_steps + " 步",
        })
      );
    }
    if (lt.last_error) {
      chips.push(kit.el("span", { class: "cx-chip tone-err", text: lt.last_error }));
    }
    const params = kit.el("div", { class: "cx-status", style: { marginTop: "8px" } });
    if (lt.target) {
      params.append(
        kit.el("span", {
          class: "cx-chip",
          text: "目标 " + lt.target[0] + "% / " + lt.target[1] + "K",
        })
      );
    }
    if (lt.applied) {
      params.append(
        kit.el("span", {
          class: "cx-chip",
          text: "已下发 " + lt.applied[0] + "% / " + lt.applied[1] + "K",
        })
      );
    }
    if (!lt.target && !lt.applied) {
      params.append(
        kit.el("span", { class: "cx-chip", text: "本轮尚未下发参数" })
      );
    }
    // FL-UX-04: 人类读友好名；slug 保留在 title 供排查
    const friendly =
      this._hass?.states?.[lt.entity_id]?.attributes?.friendly_name || lt.entity_id;
    return kit.el("div", { class: "fx-card", "data-light": lt.entity_id },
      kit.el("h2", { text: friendly, title: lt.entity_id }),
      kit.el("div", { class: "cx-status" }, chips),
      params
    );
  }
}

/* Defined synchronously and guarded (a shell re-import re-evaluates this
 * module); the ready promise is assigned BEFORE the define call — upgrading
 * an in-DOM element fires its connectedCallback synchronously. */
window.__anlPageReady = ensureKit();
if (!customElements.get("anl-page")) {
  customElements.define("anl-page", AnlPage);
}
