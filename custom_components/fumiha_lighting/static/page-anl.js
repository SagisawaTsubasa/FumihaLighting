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
 * runtime-visual surface. Data: GET /api/fumiha_lighting/anl/snapshot
 * (poll-driven). Writes: POST /api/fumiha_lighting/anl/trigger and the
 * config editor — GET/POST /api/fumiha_lighting/anl/config (V1.4.0):
 * per-light overrides and extra periods are edited HERE in form dialogs
 * and saved into entry options (the native wizard covers only
 * lights/times/params; extras+overrides are GUI-managed).
 * V1.5.0: settings-dialog rows are conditionally visible (row registry +
 * single sync function, event delegation), and a day constant-illumination
 * mode hands the day segment over to the lux-side closed loop instead of
 * direct real-light control. V1.6.0: the takeover list is per-light
 * (day_ci_lights). V1.6.1: the toggle moved into the per-light dialog;
 * the group dialog only keeps the group-level virtual light row (always
 * visible while day mode is on).
 * ========================================================================== */

const SELF_VER = new URL(import.meta.url).search;
const LOCAL_TOKENS_HREF = "/fumiha_lighting/tokens.css" + SELF_VER;
const LOCAL_KIT_SRC = "/fumiha_lighting/kit.js" + SELF_VER;
const SNAPSHOT_URL = "/api/fumiha_lighting/anl/snapshot";
const CONFIG_URL = "/api/fumiha_lighting/anl/config";
const POLL_MS = 5000;

const ANCHOR_OPTIONS = [
  ["fixed", "固定时间"],
  ["sunset", "日落"],
  ["sunrise", "日出"],
];

/* 客户端兜底范围（服务端 GET limits 缺失时使用；与 anl/config_schema.py
 * 同源——缺省 default 字段也保留，null 值输入回落默认）。 */
const FALLBACK_LIMITS = {
  times: {
    start_offset: { min: -120, max: 120, step: 5, default: 0, unit: "min" },
    end_offset: { min: -120, max: 120, step: 5, default: 0, unit: "min" },
    start_transition: { min: 0, max: 120, step: 5, default: 0, unit: "min" },
    end_transition: { min: 0, max: 120, step: 5, default: 0, unit: "min" },
    transition_interval: { min: 1, max: 15, step: 1, default: 5, unit: "min" },
  },
  params: {
    brightness: { min: 1, max: 100, step: 1, default: 25, unit: "%" },
    color_temp_kelvin: { min: 1500, max: 6500, step: 100, default: 2200, unit: "K" },
    day_brightness: { min: 1, max: 100, step: 1, default: 100, unit: "%" },
    day_color_temp_kelvin: { min: 1500, max: 6500, step: 100, default: 4000, unit: "K" },
    tolerance_brightness: { min: 0, max: 25, step: 1, default: 4, unit: "%" },
    tolerance_kelvin: { min: 0, max: 1000, step: 50, default: 150, unit: "K" },
    verify_delay: { min: 0, max: 60, step: 1, default: 5, unit: "s" },
    settle_delay: { min: 0, max: 10, step: 0.5, default: 1, unit: "s" },
  },
  extras: {
    brightness: { min: 1, max: 100, step: 1, default: 60, unit: "%" },
    color_temp_kelvin: { min: 1500, max: 6500, step: 100, default: 3000, unit: "K" },
    transition: { min: 0, max: 120, step: 5, default: 0, unit: "min" },
  },
};

const lim = (limits, section, key) => {
  const fromServer = limits && limits[section] && limits[section][key];
  if (!fromServer) {
    // 兜底必须响（ANL-F-043）：服务端 limits 形状漂移时静默用硬编码
    // 会让主路径的回归在浏览器测试里不可见
    console.warn("[anl] limits 缺失或形状漂移，回落内置范围:", section, key);
  }
  return fromServer || FALLBACK_LIMITS[section][key];
};

const maxExtras = (limits) => {
  if (!(limits && limits.max_extras)) {
    console.warn("[anl] limits.max_extras 缺失，回落 5");
  }
  return (limits && limits.max_extras) || 5;
};

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

const anchorLabel = (value) => {
  const hit = ANCHOR_OPTIONS.find(([v]) => v === value);
  return hit ? hit[1] : String(value);
};

/* ---- dialog building blocks ------------------------------------------- */

const dlgRow = (label, control, help, title) => {
  const kit = window.FUMIHA.kit;
  return kit.el(
    "div",
    {
      // title 挂行容器（ANL-F-048）：disabled 控件不派发鼠标事件，
      // 悬停提示挂在 input 上多数浏览器看不到
      ...(title ? { title } : {}),
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "10px",
        margin: "5px 0",
      },
    },
    kit.el("span", { text: label, style: { flex: "1" } }),
    control,
    ...(help ? [kit.el("span", { class: "cx-note", text: help })] : [])
  );
};

const dlgSection = (title, rows) => {
  const kit = window.FUMIHA.kit;
  return kit.el(
    "div",
    { style: { marginTop: "10px" } },
    kit.el("h3", { text: title, style: { margin: "0 0 4px" } }),
    ...rows
  );
};

const numInput = (value, spec) => {
  const kit = window.FUMIHA.kit;
  const effective = value == null ? spec.default : value;
  return kit.el("input", {
    class: "cx-input",
    type: "number",
    min: String(spec.min),
    max: String(spec.max),
    step: String(spec.step),
    value: effective == null ? "" : String(effective),
  });
};

const readNum = (input, label) => {
  const v = parseFloat(input.value);
  if (!Number.isFinite(v)) throw new Error(label + " 不是有效数字");
  return v;
};

const toHms = (value, label) => {
  if (!value) throw new Error(label + " 未填写");
  const parts = String(value).split(":");
  while (parts.length < 3) parts.push("00");
  return parts.map((p) => p.padStart(2, "0")).join(":");
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
    this._dialogOpen = 0;
    this._pendingRender = false;
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
    this._dialogOpen = 0;
    this._pendingRender = false;

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
      if (this._dialogOpen > 0) {
        // 弹窗打开期间冻结 body 重建（弹窗挂在 shadowRoot 本不受影响，
        // 但冻结可让用户看到的底页与打开时一致）；关闭时补一次强制刷新。
        this._pendingRender = true;
      } else {
        this._payloadJson = json;
        this._managers = managers;
        this._render();
      }
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

  /* ---- dialog scaffold -------------------------------------------------- */

  _dialogClosed() {
    this._dialogOpen = Math.max(0, this._dialogOpen - 1);
    if (this._dialogOpen === 0 && this._pendingRender) {
      this._pendingRender = false;
      this._payloadJson = ""; // force re-render against the freshest snapshot
      this._refresh();
    }
  }

  _openDialog(title, bodyEls, onSave, saveLabel) {
    const kit = window.FUMIHA.kit;
    return new Promise((resolve) => {
      const err = kit.el("div", {
        class: "fx-error",
        style: { display: "none", marginTop: "8px", whiteSpace: "pre-wrap" },
      });
      const saveBtn = kit.el("button", {
        class: "cx-btn primary",
        text: saveLabel || "保存",
      });
      const cancelBtn = kit.el("button", { class: "cx-btn", text: "取消" });
      const dlg = kit.el(
        "dialog",
        {
          class: "fx-dialog",
          style: {
            minWidth: "360px",
            maxWidth: "92vw",
            maxHeight: "85vh",
            overflow: "auto",
          },
        },
        kit.el("h2", { text: title, style: { marginTop: "0" } }),
        ...bodyEls,
        err,
        kit.el(
          "div",
          {
            style: {
              display: "flex",
              justifyContent: "flex-end",
              gap: "8px",
              marginTop: "12px",
            },
          },
          cancelBtn,
          saveBtn
        )
      );
      this.shadowRoot.append(dlg);
      this._dialogOpen += 1;
      dlg.showModal();
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        try {
          dlg.close();
        } catch (_e) {
          /* already closed via Esc */
        }
        resolve(result);
      };
      dlg.addEventListener("close", () => {
        dlg.remove();
        this._dialogClosed();
        if (!settled) {
          settled = true;
          resolve(false);
        }
      });
      cancelBtn.addEventListener("click", () => finish(false));
      saveBtn.addEventListener("click", async () => {
        err.style.display = "none";
        saveBtn.disabled = true;
        try {
          await onSave();
          finish(true);
        } catch (e) {
          err.textContent = String(e && e.message ? e.message : e);
          err.style.display = "";
          saveBtn.disabled = false;
        }
      });
    });
  }

  async _fetchConfig(entryId) {
    const kit = window.FUMIHA.kit;
    const payload = await kit
      .api(this._hass)
      .get(CONFIG_URL + "?entry_id=" + encodeURIComponent(entryId));
    if (!payload || !payload.config) throw new Error("响应缺 config");
    return payload;
  }

  /* ---- global settings dialog ------------------------------------------ */

  async _openSettingsDialog(m) {
    let config;
    let limits = null;
    try {
      const payload = await this._fetchConfig(m.entry_id);
      config = payload.config;
      limits = payload.limits || null;
    } catch (e) {
      window.FUMIHA.kit.ack(this.shadowRoot, {
        ok: false,
        label: "读取设置",
        reason: String(e && e.message ? e.message : e),
      });
      return;
    }
    const kit = window.FUMIHA.kit;
    const inputs = {};
    // 行引用注册表（V1.5.0 条件显隐）：key → dlgRow 行元素，单一同步函数
    // 按条件切 display；行容器打 data-anl-row 供测试/排查定位
    const rows = {};

    // label 随绑定记录（ANL-F-034）：保存时空值以字段名报错，不再静默跳过
    const bind = (key, el, read, label) => {
      inputs[key] = { el, read: read || (() => el.value), label };
    };
    const selInput = (key, value) => {
      // 选项取服务端 limits.enums（ANL-F-024），本地 ANCHOR_OPTIONS 仅兜底；
      // 当前值不在枚举内时追加展示（ANL-F-031：select 静默回落首项会把
      // 未知/缺省锚点改成「固定时间」，宁可显示原值让用户看见）
      const values = (limits && limits.enums && limits.enums[key]) || null;
      const opts = (
        values ? values.map((v) => [v, anchorLabel(v)]) : ANCHOR_OPTIONS
      ).slice();
      if (value != null && !opts.some(([v]) => v === value)) {
        opts.push([value, anchorLabel(value)]);
      }
      const sel = kit.el(
        "select",
        { class: "cx-input", style: { width: "130px" } },
        opts.map(([v, t]) =>
          kit.el("option", { value: v, text: t, ...(v === value ? { selected: true } : {}) })
        )
      );
      bind(key, sel, null, key === "start_mode" ? "夜间开始方式" : "夜间结束方式");
      return sel;
    };
    const timeInput = (key, value, label) => {
      const inp = kit.el("input", {
        class: "cx-input",
        type: "time",
        value: value || "",
        style: { width: "130px" },
      });
      bind(key, inp, null, label);
      return inp;
    };
    const numField = (section, key, value, label) => {
      const inp = numInput(value, lim(limits, section, key));
      bind(key, inp, () =>
        inp.value === "" ? null : readNum(inp, label)
      , label);
      rows[key] = dlgRow(label + (lim(limits, section, key).unit ? "（" + lim(limits, section, key).unit + "）" : ""), inp);
      return rows[key];
    };
    const checkInput = (key, checked, label) => {
      const cb = kit.el("input", { type: "checkbox", ...(checked ? { checked: true } : {}) });
      bind(key, cb, () => cb.checked, label);
      rows[key] = dlgRow(label, cb);
      return rows[key];
    };
    const textInput = (key, value, label, placeholder, help) => {
      const inp = kit.el("input", {
        class: "cx-input",
        type: "text",
        value: value || "",
        style: { width: "180px" },
        placeholder: placeholder || "sun.sun",
      });
      bind(key, inp, () => inp.value.trim(), label);
      rows[key] = dlgRow(label, inp, help);
      return rows[key];
    };

    /* 额外时段动态行 */
    const extrasRows = [];
    const extrasBox = kit.el("div");
    const addExtraRow = (extra) => {
      const idx = extrasRows.length;
      const row = { live: true };
      const bInp = numInput(extra ? extra.brightness : 60, lim(limits, "extras", "brightness"));
      const kInp = numInput(extra ? extra.color_temp_kelvin : 3000, lim(limits, "extras", "color_temp_kelvin"));
      const tInp = numInput(extra ? extra.transition : 0, lim(limits, "extras", "transition"));
      // transEl 单独留引用：显隐同步判「任一额外时段过渡>0」时不走 read()
      //（read 会因空值抛错，显隐必须对半填状态容错）
      row.transEl = tInp;
      const sInp = kit.el("input", {
        class: "cx-input",
        type: "time",
        value: extra ? (extra.start || "").slice(0, 5) : "18:00",
        style: { width: "110px" },
      });
      const nInp = kit.el("input", {
        class: "cx-input",
        type: "text",
        value: extra ? extra.name || "" : "",
        style: { width: "90px" },
        placeholder: "名称",
      });
      const delBtn = kit.el("button", { class: "cx-btn danger", text: "删除" });
      const wrap = kit.el(
        "div",
        {
          "data-extra-row": String(idx),
          style: {
            border: "1px solid var(--fx-edge, #ccc)",
            borderRadius: "8px",
            padding: "6px 8px",
            margin: "6px 0",
          },
        },
        dlgRow("名称", nInp),
        dlgRow("开始时间", sInp),
        dlgRow("亮度（%）", bInp),
        dlgRow("色温（K）", kInp),
        dlgRow("过渡（min，0=立即）", tInp),
        dlgRow("", delBtn)
      );
      row.read = () => ({
        name: nInp.value.trim(),
        start: toHms(sInp.value, "额外时段开始时间"),
        brightness: readNum(bInp, "额外时段亮度"),
        color_temp_kelvin: readNum(kInp, "额外时段色温"),
        transition: readNum(tInp, "额外时段过渡"),
      });
      delBtn.addEventListener("click", () => {
        row.live = false;
        wrap.remove();
        // 增删行可能改变「任一过渡>0」判定 → 手动补调一次显隐（V1.5.0）
        syncVisibility();
      });
      extrasRows.push(row);
      extrasBox.append(wrap);
    };
    (config.extras || []).forEach((e) => addExtraRow(e));
    const extraCap = maxExtras(limits);
    const addBtn = kit.el("button", {
      class: "cx-btn",
      text: "添加时段",
      onclick: () => {
        if (extrasRows.filter((r) => r.live).length >= extraCap) {
          kit.toast(this.shadowRoot, "额外时段最多 " + extraCap + " 个", { isError: true });
          return;
        }
        addExtraRow(null);
        syncVisibility();
      },
    });

    // 选择/时间行的行容器在此注册（输入类行由各 helper 注册进 rows）
    rows.start_mode = dlgRow("夜间开始于", selInput("start_mode", config.start_mode));
    rows.trigger_time = dlgRow("夜间开始固定时间", timeInput("trigger_time", config.trigger_time, "夜间开始固定时间"));
    rows.end_mode = dlgRow("夜间结束于", selInput("end_mode", config.end_mode));
    rows.end_time = dlgRow("夜间结束固定时间", timeInput("end_time", config.end_time, "夜间结束固定时间"));

    const body = [
      dlgSection("时间锚点", [
        rows.start_mode,
        rows.trigger_time,
        numField("times", "start_offset", config.start_offset, "夜间开始偏移"),
        rows.end_mode,
        rows.end_time,
        numField("times", "end_offset", config.end_offset, "夜间结束偏移"),
        textInput("sun_entity", config.sun_entity, "日出日落实体"),
        numField("times", "start_transition", config.start_transition, "入夜过渡时长"),
        numField("times", "end_transition", config.end_transition, "天亮过渡时长"),
        numField("times", "transition_interval", config.transition_interval, "过渡步进间隔"),
      ]),
      dlgSection("时段参数", [
        checkInput("day_enabled", !!config.day_enabled, "启用日间模式"),
        numField("params", "brightness", config.brightness, "夜间亮度"),
        numField("params", "color_temp_kelvin", config.color_temp_kelvin, "夜间色温"),
        numField("params", "day_brightness", config.day_brightness, "日间亮度"),
        numField("params", "day_color_temp_kelvin", config.day_color_temp_kelvin, "日间色温"),
        // V1.7.0 反查推导：虚拟灯行删除（CI 面清零）——移交对象由系统经
        // 灯→区域反查表推导，不再是用户选项；接管入口在逐灯弹窗
        numField("params", "tolerance_brightness", config.tolerance_brightness, "亮度容差"),
        numField("params", "tolerance_kelvin", config.tolerance_kelvin, "色温容差"),
        numField("params", "verify_delay", config.verify_delay, "验证延迟"),
        numField("params", "settle_delay", config.settle_delay, "开灯稳定延迟"),
        checkInput("turn_on_listen", config.turn_on_listen !== false, "开灯时自动应用"),
        checkInput("only_when_on", !!config.only_when_on, "仅调整已开启的灯"),
        checkInput("respect_manual", config.respect_manual !== false, "尊重手动调节"),
      ]),
      dlgSection(
        "额外时段（可选，最多 " + extraCap + " 个）",
        [extrasBox, kit.el("div", { style: { marginTop: "6px" } }, addBtn)]
      ),
    ];

    // 行容器打 key 标记（V1.5.0）：测试/排查按 data-anl-row 定位行
    for (const [key, row] of Object.entries(rows)) {
      row.dataset.anlRow = key;
    }

    /* 单一显隐同步（V1.5.0 重构）：所有条件行集中在此按 inputs 当前值切
     * display——不逐控件挂监听，改对弹窗内容容器做事件委托（input/change
     * 都驱动），extras 增删行后手动补调。显隐只动 display，值不清——
     * 全量键提交不丢存储值。 */
    const show = (key, visible) => {
      // F-063：dlgRow 行容器是 inline display:flex——恢复可见写 "" 会抹掉
      // 内联 flex，整行退化为 block 布局；必须显式写回 "flex"
      if (rows[key]) rows[key].style.display = visible ? "flex" : "none";
    };
    // V1.6.1：清单编辑面移入逐灯弹窗——只读存量清单作判据（虚拟灯行显隐、
    // 过渡提示与保存守卫共用；弹窗生命周期内恒定）
    const ciAny = (config.day_ci_lights || []).length > 0;
    const rawNum = (key) => {
      const field = inputs[key];
      const v = field ? parseFloat(field.el.value) : NaN;
      return Number.isFinite(v) ? v : 0;
    };
    const syncVisibility = () => {
      const sm = inputs.start_mode.el.value;
      const em = inputs.end_mode.el.value;
      const dayOn = inputs.day_enabled.el.checked;
      show("trigger_time", sm === "fixed");
      show("start_offset", sm !== "fixed");
      show("end_time", em === "fixed");
      show("end_offset", em !== "fixed");
      show("sun_entity", sm !== "fixed" || em !== "fixed");
      // F-104 终裁（用户拍板 2026-10-23）：全局日间参数行与 CI 完全解耦——
      // 非 CI 灯消费全局日间参数，混合组/全 CI 组都恒显（只随 day_enabled）；
      // 亮度恒定本身是逐灯开关（day_ci_lights），不在全局层表达
      show("day_brightness", dayOn);
      show("day_color_temp_kelvin", dayOn);
      // V1.7.0 反查推导：虚拟灯行已删除（移交对象由反查表推导，CI 面清零）
      const extraTrans = extrasRows.some((r) => {
        if (!r.live || !r.transEl) return false;
        const v = parseFloat(r.transEl.value);
        return Number.isFinite(v) && v > 0;
      });
      show(
        "transition_interval",
        rawNum("start_transition") > 0 ||
          rawNum("end_transition") > 0 ||
          extraTrans
      );
      // 天亮过渡只在日间模式下生效（ANL-F-041）：manager 会把 day_enabled=false
      // 时的 end_transition 归零——输入框跟随 day_enabled 置灰（V1.4.0 的
      // syncEndTransition 并入此处统一同步），不留静默无操作
      const gated = !dayOn;
      // F-062（V1.5.0）：CI 开时日侧过渡带对该灯被跳过——两个过渡输入挂
      // 悬停提示，值不禁用（存储保留，CI 关后恢复生效）；天亮过渡的置灰
      // 提示优先（日间模式关时 CI 行已隐藏，无从谈起）
      const ciTransNote = "日间恒照度开启时，日侧过渡带对被接管的灯跳过，此值暂不生效";
      const ciNoteOn = ciAny && dayOn;
      inputs.start_transition.el.title = ciNoteOn ? ciTransNote : "";
      const endTransInp = inputs.end_transition.el;
      endTransInp.disabled = gated;
      endTransInp.title = gated
        ? "天亮过渡需要先启用日间模式"
        : ciNoteOn
        ? ciTransNote
        : "";
      if (rows.end_transition) {
        rows.end_transition.title = gated ? "天亮过渡需要先启用日间模式" : "";
      }
    };

    // 弹窗内容包一层容器承载事件委托；构建后立即同步一次（初始态正确，
    // 不依赖用户操作）
    const content = kit.el("div", {}, ...body);
    content.addEventListener("input", syncVisibility);
    content.addEventListener("change", syncVisibility);
    syncVisibility();

    await this._openDialog(
      m.title + " — 全局设置",
      [content],
      async () => {
        const times = {};
        const params = {};
        // 空值显式报错（ANL-F-034）：清空字段保存却报「已保存」是静默无操作，
        // 错误必须在弹窗 err 行可见，而不是被 patch 语义悄悄吞掉
        const pick = (obj, key) => {
          const field = inputs[key];
          const v = field.read();
          if (v === null || v === undefined || v === "") {
            throw new Error((field.label || key) + " 不能为空");
          }
          obj[key] = v;
        };
        ["start_mode", "end_mode"].forEach((k) => pick(times, k));
        ["start_offset", "end_offset", "start_transition", "end_transition", "transition_interval"].forEach(
          (k) => pick(times, k)
        );
        times.trigger_time = toHms(inputs.trigger_time.el.value, "夜间开始固定时间");
        times.end_time = toHms(inputs.end_time.el.value, "夜间结束固定时间");
        if (!inputs.sun_entity.el.value.trim()) {
          throw new Error("日出日落实体 不能为空");
        }
        times.sun_entity = inputs.sun_entity.el.value.trim();
        [
          "day_enabled",
          "brightness",
          "color_temp_kelvin",
          "day_brightness",
          "day_color_temp_kelvin",
          "tolerance_brightness",
          "tolerance_kelvin",
          "verify_delay",
          "settle_delay",
          "turn_on_listen",
          "only_when_on",
          "respect_manual",
        ].forEach((k) => pick(params, k));
        // V1.7.0 反查推导：全局保存零 CI 键——day_ci_lights 缺省（编辑面在
        // 逐灯弹窗，patch 语义不动存量清单）；虚拟灯键已废除不再提交
        const extras = extrasRows.filter((r) => r.live).map((r) => r.read());
        const resp = await kit.api(this._hass).post(CONFIG_URL, {
          entry_id: m.entry_id,
          patch: { times, params, extras },
        });
        const pruned = resp && resp.pruned_override_extras;
        kit.ack(this.shadowRoot, {
          ok: true,
          label: pruned
            ? "设置已保存，正在应用（时段数量变化，" + pruned + " 条逐灯时段覆盖已清除）"
            : "设置已保存，正在应用",
        });
      },
      "保存设置"
    );
  }

  /* ---- per-light override dialog --------------------------------------- */

  async _openLightDialog(m, lt) {
    let config;
    let limits = null;
    try {
      const payload = await this._fetchConfig(m.entry_id);
      config = payload.config;
      limits = payload.limits || null;
    } catch (e) {
      window.FUMIHA.kit.ack(this.shadowRoot, {
        ok: false,
        label: "读取设置",
        reason: String(e && e.message ? e.message : e),
      });
      return;
    }
    const kit = window.FUMIHA.kit;
    const gid = lt.entity_id;
    const ovr = (config.overrides || {})[gid] || null;
    const dayOn = !!config.day_enabled;
    // 日间恒照度（V1.5.0 引入，V1.6.0 逐灯化，V1.7.0 反查推导）：本灯真
    // 被接管（快照生效值 + ∈反查表）时日间段不渲染——日间段不渲染时保存
    // 走保留分支（存储保留，退出清单后恢复生效）。F-061：生效值才决定
    // 日间段是否真被恒照度接管；V1.7.0 起逐灯有效性由 ci_zone_name 表达
    // （清单内但区域已删的灯 ci_zone_name=null → 日间段照常渲染）。
    const ciOn = !!m.day_ci_active && lt.ci_zone_name != null;
    const dayVisible = dayOn && !ciOn;
    const extras = config.extras || [];
    const controls = [];
    // 显式引用绑定（ANL-F-025）：保存按 key 读输入，不按 DOM 位置
    const refs = {};
    const numField = (section, key, value, label) => {
      const inp = numInput(value, lim(limits, section, key));
      controls.push(inp);
      refs[key] = inp;
      return dlgRow(label + "（" + lim(limits, section, key).unit + "）", inp);
    };
    // 取值链（ANL-F-018）：覆盖值 → 全局值（→ 规格默认值，在 numInput 内）；
    // 日间键在后端可选，缺失时回落全局而不是留空阻断保存
    const pick = (key) => (ovr && ovr[key] != null ? ovr[key] : config[key]);

    // 日间恒照度接管（V1.6.1 逐灯开关 + V1.7.0 归属标注）：勾选态读存储
    // 值 day_ci_lights（编辑面与数据同源；生效值 ciOn 只决定日间段显隐与
    // 接管说明）。B2：灯卡归属随快照逐灯 ci_zone_name 下发——∈恒照度区域
    // 的灯标「由区域 X 接管」，∉区域的灯开关禁用（接管是无效控制）；
    // 存量勾选但区域已删的灯保持可取消（禁用只挡新勾选）。
    const ciList = (config.day_ci_lights || []).slice();
    const ciListOriginal = ciList.includes(gid);
    const ciZone = lt.ci_zone_name || null;
    const ciCb = kit.el("input", {
      type: "checkbox",
      "data-anl-ci-toggle": "1",
      ...(ciListOriginal ? { checked: true } : {}),
      ...(!ciZone && !ciListOriginal ? { disabled: true } : {}),
    });
    const ciHint = ciZone
      ? "由恒照度区域「" + ciZone + "」接管"
      : "此灯不属于任何恒照度区域";
    const ciRow = dlgRow("日间恒照度接管", ciCb, ciHint);

    const followCb = kit.el("input", {
      type: "checkbox",
      "data-anl-follow-toggle": "1",
      ...(ovr ? {} : { checked: true }),
    });
    const followRow = dlgRow("跟随全局设置", followCb);

    const nightRowEls = [
      numField("params", "brightness", pick("brightness"), "夜间亮度"),
      numField("params", "color_temp_kelvin", pick("color_temp_kelvin"), "夜间色温"),
    ];
    const dayRowEls = dayVisible
      ? [
          numField("params", "day_brightness", pick("day_brightness"), "日间亮度"),
          numField(
            "params",
            "day_color_temp_kelvin",
            pick("day_color_temp_kelvin"),
            "日间色温"
          ),
        ]
      : [];
    const extraRefs = extras.map((extra, i) => {
      const pair = ovr && ovr.extras ? ovr.extras[String(i)] : null;
      const bInp = numInput(
        pair && pair.brightness != null ? pair.brightness : extra.brightness,
        lim(limits, "extras", "brightness")
      );
      const kInp = numInput(
        pair && pair.color_temp_kelvin != null
          ? pair.color_temp_kelvin
          : extra.color_temp_kelvin,
        lim(limits, "extras", "color_temp_kelvin")
      );
      controls.push(bInp, kInp);
      const title = extra.name || "额外时段 " + (i + 1);
      return {
        b: bInp,
        k: kInp,
        el: kit.el(
          "div",
          { "data-ovr-extra": String(i) },
          dlgRow(title + " 亮度（%）", bInp),
          dlgRow(title + " 色温（K）", kInp)
        ),
      };
    });

    const syncDisabled = () => {
      controls.forEach((c) => {
        c.disabled = followCb.checked;
      });
    };
    followCb.addEventListener("change", syncDisabled);
    syncDisabled();

    const body = [
      ciRow,
      followRow,
      dlgSection("夜间参数", nightRowEls),
      ...(dayVisible ? [dlgSection("日间参数", dayRowEls)] : []),
      ...(dayOn && ciOn
        ? [
            kit.el("div", {
              class: "cx-note",
              style: { marginTop: "10px" },
              text:
                "日间恒照度模式开启中——日间参数由恒照度闭环接管，" +
                "逐灯日间覆盖暂不生效（保留存储）",
            }),
          ]
        : []),
      ...(extras.length
        ? [dlgSection("额外时段覆盖", extraRefs.map((r) => r.el))]
        : []),
    ];

    await this._openDialog(
      (this._hass?.states?.[gid]?.attributes?.friendly_name || gid) + " — 逐灯设置",
      body,
      async () => {
        // V1.6.1：恒照度接管开关（逐灯）——读存量清单增/删本灯后整表提交；
        // 与 overrides 修复/恢复合并为一次多分节 POST（patch 缺省分节=不改）
        const patch = {};
        const wantCi = ciCb.checked;
        if (wantCi !== ciListOriginal) {
          if (wantCi && !dayOn) {
            throw new Error("日间恒照度需先在全局设置启用日间模式");
          }
          // V1.7.0 反查推导：虚拟灯前置校验废除——区域归属由服务端 zone
          // 归属校验兜底（∉区域的灯开关本页已禁用；存量勾选可取消）
          const nextList = wantCi
            ? ciList.concat([gid])
            : ciList.filter((e) => e !== gid);
          patch.params = { day_ci_lights: nextList };
        }
        if (followCb.checked) {
          patch.overrides_remove = [gid];
        } else {
          const override = {};
          // 夜间对同判据（ANL-F-040）：值=全局不写覆盖——只为真正改动的键建
          // 覆盖，全局再改夜间参数时本灯继续跟随
          for (const k of ["brightness", "color_temp_kelvin"]) {
            const v = readNum(refs[k], k === "brightness" ? "夜间亮度" : "夜间色温");
            if (v !== config[k]) override[k] = v;
          }
          if (dayVisible) {
            // 与 extras 同判据（ANL-F-033）：值=全局就不写覆盖——预填值就是
            // 全局值，无条件写会把当时的全局日间参数钉死成覆盖，之后全局
            // 改日间参数对该灯永久失效
            for (const k of ["day_brightness", "day_color_temp_kelvin"]) {
              const v = readNum(refs[k], k === "day_brightness" ? "日间亮度" : "日间色温");
              if (v !== config[k]) override[k] = v;
            }
          } else if (ovr) {
            // 未渲染字段不因本次保存丢失（ANL-F-027 静默丢弃面）：日间已关
            // 或日间恒照度接管时都不渲染日间段——保留该灯既有的日间覆盖键，
            // 恢复跟随条件后仍然生效
            if (ovr.day_brightness != null) {
              override.day_brightness = ovr.day_brightness;
            }
            if (ovr.day_color_temp_kelvin != null) {
              override.day_color_temp_kelvin = ovr.day_color_temp_kelvin;
            }
          }
          if (extras.length) {
            // 只提交与全局不同的行（ANL-F-027 固化面）：值=全局的行不写覆盖
            // ——整灯替换语义下旧覆盖随之消失，等效「该时段恢复跟随全局」
            const ovrExtras = {};
            extras.forEach((extra, i) => {
              const b = readNum(extraRefs[i].b, "额外时段亮度");
              const k = readNum(extraRefs[i].k, "额外时段色温");
              if (b !== extra.brightness || k !== extra.color_temp_kelvin) {
                ovrExtras[String(i)] = { brightness: b, color_temp_kelvin: k };
              }
            });
            if (Object.keys(ovrExtras).length) override.extras = ovrExtras;
          }
          if (Object.keys(override).length) {
            patch.overrides = { [gid]: override };
          }
        }
        if (!patch.params && !patch.overrides && !patch.overrides_remove) {
          // 后端拒绝空覆盖（ANL-F-040）；这里提前给出可读提示
          throw new Error(
            "没有改任何值（全部与全局一致）。如需恢复跟随全局，请保持勾选「跟随全局设置」"
          );
        }
        await kit.api(this._hass).post(CONFIG_URL, {
          entry_id: m.entry_id,
          patch,
        });
        kit.ack(this.shadowRoot, { ok: true, label: "逐灯设置已保存，正在应用" });
      },
      "保存"
    );
  }

  /* ---- cards ------------------------------------------------------------ */

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
        class: "cx-btn",
        "data-action": "settings",
        text: "⚙ 设置",
        title: "编辑本条目的时间锚点、参数与额外时段",
        onclick: () => {
          // 构建期异常也要响（ANL-F-035）：读取失败有 ack，构建段抛错
          // 不能落成无人接的 unhandled rejection（按钮看似坏了）
          this._openSettingsDialog(m).catch((e) =>
            kit.ack(this.shadowRoot, {
              ok: false,
              label: "打开设置",
              reason: String(e && e.message ? e.message : e),
            })
          );
        },
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
      "入夜 " + anchorLabel(m.start_mode),
      "天亮 " + anchorLabel(m.end_mode),
    ];
    if (m.transitions_active) summaryBits.push("过渡带开启");
    if (m.day_enabled) summaryBits.push("日间时段");
    // 日间恒照度（V1.5.0）：接管态随快照下发，摘要直接可见。F-061：读生效值
    // day_ci_active——存储开着但未配灯（active=false）时不得谎报接管
    if (m.day_ci_active) summaryBits.push("日间恒照度");
    if (m.respect_manual) summaryBits.push("尊重手动");
    const summary = kit.el(
      "div",
      { class: "cx-status", style: { marginTop: "8px" } },
      summaryBits.map((t) => kit.el("span", { class: "cx-tag", text: t }))
    );

    const lightCards = (m.lights || []).map((lt) => this._lightCard(m, lt));
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

  _lightCard(m, lt) {
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
      params,
      kit.el("div", { style: { marginTop: "8px" } },
        kit.el("button", {
          class: "cx-btn",
          "data-action": "light-settings",
          text: "逐灯设置",
          title: "为此灯单独设置参数（跟随全局或覆盖）",
          onclick: () => {
            this._openLightDialog(m, lt).catch((e) =>
              kit.ack(this.shadowRoot, {
                ok: false,
                label: "打开逐灯设置",
                reason: String(e && e.message ? e.message : e),
              })
            );
          },
        }))
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
