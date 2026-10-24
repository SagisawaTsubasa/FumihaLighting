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
const LINK_URL = "/api/fumiha_lighting/lux/link_group";
const POLL_SLOW_MS = 5000;
const POLL_FAST_MS = 1000; // while any zone is calibrating
const FAIL_STREAK_ERROR_CARD = 3; // consecutive failures before the error card
// ANL-F-155：保存联动组 → entry reload 是异步任务，轮询可能落进出桶窗口
//（zones 短暂缺刚保存的条目）——此窗内保留现有 DOM 不渲染空态
const RELOAD_GRACE_MS = 15000;

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
    // ANL-F-155 reload 窗口 grace 判定源；ANL-F-166 起为多槽 Map（单槽会
    // 被 15s 内的第二个保存顶掉，先存区出桶窗重新裸奔）：
    // entry_id -> { at, group, fade, step }（保存响应三键）
    this._pendingSaves = new Map();
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
    this._pendingSaves.clear();

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

    // ANL-F-155：保存后的轮询若撞上 reload 出桶窗口（刚保存的条目不在
    // zones 里），grace 内保留现有 DOM 与 _zones 不渲染空态——旧码会把
    // 卡片闪没最长 5s；收敛或超时后正常放行
    if (this._pendingSaves.size) {
      // ANL-F-162/164：收敛判据=条目在且三键全等。「条目仍在」≠ reload
      // 已完成——保存前已发出、保存后才落地的 overview（或 reload 尚未
      // pop 的桶）里组标签可能与保存值相同（只调 cross_fade/max_step_pct
      // 的保存组标签不变，旧快照仅比标签会被误判收敛清掉 grace，随后真
      // 出桶窗照旧闪空态），故 overview 自 R4 修复起下发两数，与保存响应
      // 同源全等比对（POST 响应与 _serialize_lux_zone 对空组都发 null，
      // 两侧 || "" 归一）。15s 超时无条件放行，兜住条目被删等永不收敛的
      // 场景。ANL-F-166：槽按 entry_id 存 Map，收敛/超时各自删除；渲染是
      // 整页 all-or-nothing，任一未收敛条目在窗内即整页保持
      let hold = false;
      const now = Date.now();
      for (const [entryId, saved] of this._pendingSaves) {
        const zone = zones.find((z) => z.entry_id === entryId);
        const notConverged =
          !zone
          || (zone.link_group || "") !== saved.group
          || zone.cross_fade !== saved.fade
          || zone.max_step_pct !== saved.step;
        if (notConverged && now - saved.at < RELOAD_GRACE_MS) {
          hold = true;
        } else {
          this._pendingSaves.delete(entryId);
        }
      }
      if (hold) {
        this._renderConn();
        return;
      }
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
    // V1.7.1 联动组面板内编辑：chip 可点开编辑弹窗（三键原藏在原生
    // options 流第 2 页「校准与降级」）；未设组给弱提示 chip 同样可点
    chips.push(this._linkChip(z));
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

  /* ---- link group editor chip + dialog (V1.7.1) -------------------------- */

  // chip 文案单一来源（卡片渲染与保存后乐观更新共用，防两处漂移）
  _linkChipText(link) {
    return link.link_group
      ? // V1.3.0 联动组：组标签+组内成员名单（ambient 已扣除对方贡献）
        "联动组「" + link.link_group + "」" +
        (link.link_peers && link.link_peers.length
          ? "：与 " + link.link_peers.join("、") + " 交叉扣除"
          : "（暂无同组成员）")
      : "联动组未设置（点击配置）";
  }

  _linkChip(z) {
    const kit = window.FUMIHA.kit;
    const chip = kit.el("button", {
      class: "cx-tag",
      type: "button",
      "data-action": "link-group",
      text: this._linkChipText(z),
      title: "编辑联动组（组标签 / 交叉系数 / 缓变步幅）",
      // button 无 UA 重置，补 border/cursor 对齐 span.cx-tag 观感
      style: { border: "none", cursor: "pointer" },
      onclick: async () => {
        try {
          // ANL-F-150：去掉保存后立即 _refresh()——POST 只落 options，
          // entry reload 是随后调度的异步任务，立即 GET 会撞旧 runtime
          // （载荷不变→渲染门不重绘=旧 chip）或 unload 窗口（区出桶=
          // 空态闪 5s）；保存响应（三键+link_peers）直接乐观更新 chip，
          // 5s 轮询是唯一真相。ANL-F-155：reload 出桶窗口由 _refresh 的
          // grace 兜住（记保存事件，窗口内轮询缺本区不渲染空态）
          const saved = await this._openLinkGroupDialog(z);
          if (saved) {
            // ANL-F-155/162/164：reload 窗口 grace 判定源记三键（组标签+
            // 交叉系数+缓变步幅）——只调两数的保存组标签不变，旧 overview
            // 快照仅比标签会误判收敛；响应两数与 overview 下发的
            // _finite_or_none 归一值同源可比（GUI 档位 0.05/5 均在舍入精
            // 度内）。ANL-F-166：Map 多槽并存，15s 内先后保存互不顶掉
            this._pendingSaves.set(z.entry_id, {
              at: Date.now(),
              group: saved.link_group || "",
              fade: saved.cross_fade,
              step: saved.max_step_pct,
            });
            // ANL-F-161：保存 await 期间任何载荷变化都会 _render()
            // replaceChildren 重建全部卡片（弹窗挂 shadowRoot 不受影响，
            // 用户可跨重渲染继续保存）——闭包里的 chip 可能已是游离节点，
            // 直接写=静默落在不可见 DOM，可见 chip 停旧组而 toast 已宣称
            // 已保存。按 entry_id 重查当前在 DOM 的 chip 再写；查不到
            //（区出桶/卡片重建中）则跳过乐观更新，等 5s 轮询收敛
            const live = this._body
              && this._body.querySelector(
                '[data-zone="' + z.entry_id + '"] [data-action="link-group"]'
              );
            if (live) live.textContent = this._linkChipText(saved);
          }
        } catch (err) {
          kit.ack(this.shadowRoot, {
            ok: false,
            label: "打开联动组编辑",
            reason: String(err && err.message ? err.message : err),
          });
        }
      },
    });
    return chip;
  }

  async _openLinkGroupDialog(z) {
    const kit = window.FUMIHA.kit;
    // 返回值契约：保存成功=POST 响应对象（truthy，供 chip 乐观更新），
    // 取消/Esc/GET 失败=false（ANL-F-150）
    // 先 GET 现值+组员名单（服务端 link_members 契约）再开弹窗——名单是
    // 本编辑器超越原生 options 流对话框的价值面，不从 overview 缓存拼
    let cur;
    try {
      cur = await kit
        .api(this._hass)
        .get(LINK_URL + "?entry_id=" + encodeURIComponent(z.entry_id));
    } catch (err) {
      kit.ack(this.shadowRoot, {
        ok: false,
        label: "读取联动组",
        reason: String(err && err.message ? err.message : err),
      });
      return false;
    }
    // 行样式与 page-anl dlgRow 同款（两页弹窗观感一致）
    const row = (label, control) =>
      kit.el(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "10px",
            margin: "5px 0",
          },
        },
        kit.el("span", { text: label, style: { flex: "1" } }),
        control
      );
    // 客户端范围与 options flow selector 同源（lux/flow.py：cross_fade
    // 0.1~1.0 step 0.05 / max_step_pct 5~50 step 5；服务端同范围二次校验）
    const groupInp = kit.el("input", {
      class: "cx-input",
      type: "text",
      value: cur.link_group || "",
      placeholder: "如：卫生间（留空停用）",
      style: { width: "180px" },
      "aria-label": "联动组标签",
    });
    const fadeInp = kit.el("input", {
      class: "cx-input",
      type: "number",
      min: "0.1",
      max: "1.0",
      step: "0.05",
      value: String(cur.cross_fade ?? 0.5),
      "aria-label": "交叉系数",
    });
    const stepInp = kit.el("input", {
      class: "cx-input",
      type: "number",
      min: "5",
      max: "50",
      step: "5",
      value: String(cur.max_step_pct ?? 20),
      "aria-label": "缓变步幅（%）",
    });
    const membersNote = kit.el("div", {
      class: "cx-note",
      "data-lux-link-members": "1",
      style: { margin: "2px 0 5px" },
      text: cur.link_group
        ? "组内成员：" +
          (cur.link_members || [cur.zone_name]).map(
            (n) => (n === cur.zone_name ? n + "（本区）" : n)
          ).join("、")
        : "联动组未设置——给需要互认的区域填同一标签即成组",
    });
    // 名单脚注（V1.7.1 首审裁定②）：只列已加载区——对等区 disabled/
    // SETUP_RETRY 时名单会显得缺人，文案先讲清边界（不改语义）
    const membersFoot = cur.link_group
      ? kit.el("div", {
          class: "cx-note",
          style: { margin: "-3px 0 5px", color: "var(--fx-sub)" },
          text: "仅显示已加载区域",
        })
      : null;
    // ANL-F-149：校准中保存会经 update listener 触发 entry reload，
    // reload 取消 _cal_task 且新 runtime 无痕（cal_last_error=None）——
    // 15~40 分钟 sweep 被一次点击静默丢弃必须有醒目提示。软提示路线：
    // 用户可能确需中途改组，不做 409 硬保护
    const calWarn =
      z.mode === "calibrating"
        ? kit.el("div", {
            class: "cx-note warn",
            style: { margin: "0 0 5px" },
            text: "校准进行中：保存会重载条目并中止本次校准，已采集数据弃用",
          })
        : null;
    return new Promise((resolve) => {
      const errLine = kit.el("div", {
        class: "fx-error",
        style: { display: "none", marginTop: "8px", whiteSpace: "pre-wrap" },
      });
      const saveBtn = kit.el("button", { class: "cx-btn primary", text: "保存" });
      const cancelBtn = kit.el("button", { class: "cx-btn", text: "取消" });
      const dlg = kit.el(
        "dialog",
        { class: "fx-dialog", style: { minWidth: "360px", maxWidth: "92vw" } },
        kit.el("h2", { text: z.zone_name + " — 联动组", style: { marginTop: "0" } }),
        calWarn,
        membersNote,
        membersFoot,
        row("联动组标签", groupInp),
        row("交叉系数（0.1–1.0）", fadeInp),
        row("缓变步幅（5–50，%）", stepInp),
        errLine,
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
        if (!settled) {
          settled = true;
          resolve(false);
        }
      });
      cancelBtn.addEventListener("click", () => finish(false));
      saveBtn.addEventListener("click", async () => {
        errLine.style.display = "none";
        saveBtn.disabled = true;
        try {
          const group = groupInp.value.trim();
          const fade = parseFloat(fadeInp.value);
          const step = parseFloat(stepInp.value);
          // 空值显式报错（ANL-F-034 同哲学）：清空保存却报「已保存」是静默无操作
          if (!Number.isFinite(fade) || fade < 0.1 || fade > 1.0) {
            throw new Error("交叉系数需在 0.1–1.0 之间");
          }
          if (!Number.isFinite(step) || step < 5 || step > 50) {
            throw new Error("缓变步幅需在 5–50 之间");
          }
          // 响应三键+link_peers 交给调用方乐观更新 chip（ANL-F-150：
          // resolve 值=保存响应对象，取消/Esc/GET 失败=false）
          const res = await kit.api(this._hass).post(LINK_URL, {
            entry_id: z.entry_id,
            link_group: group,
            cross_fade: fade,
            max_step_pct: step,
          });
          kit.ack(this.shadowRoot, { ok: true, label: "联动组已保存，正在应用" });
          finish(res);
        } catch (e) {
          errLine.textContent = String(e && e.message ? e.message : e);
          errLine.style.display = "";
          saveBtn.disabled = false;
        }
      });
    });
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
          text: "校准进行中（默认 5% 步长约 15~40 分钟；2%/3% 细扫最坏可能 2~3 小时），期间本区闭环暂停，其他区域闭环自动让路。",
        }),
        kit.el("div", { style: { marginTop: "8px" } },
          kit.el("button", {
            class: "cx-btn danger",
            "data-action": "cancel-calibrate",
            text: "中止校准",
            onclick: async () => {
              if (this._confirmingCalibrate) return;
              this._confirmingCalibrate = true;
              try {
                const yes = await kit.confirmDialog(this.shadowRoot, {
                  title: "中止校准",
                  text: "将中止本次校准并恢复灯的原状态，已采集的数据弃用。",
                  confirmLabel: "中止",
                  danger: true,
                });
                if (!yes) return;
                const res = await kit.api(this._hass).post(
                  "/api/fumiha_lighting/lux/calibrate/cancel",
                  { entry_id: z.entry_id }
                );
                // FL-IMP-24: cancelled:false = 校准刚好自行结束（甚至已完成），
                // 文案不得宣称"已中止/数据弃用"
                if (res.cancelled === false) {
                  kit.ack(this.shadowRoot, {
                    ok: true,
                    label: "校准已自行结束（未需要中止）",
                  });
                } else {
                  kit.ack(this.shadowRoot, { ok: true, label: "校准已中止" });
                }
              } catch (err) {
                kit.ack(this.shadowRoot, {
                  ok: false,
                  label: "中止校准",
                  reason: String(err && err.message ? err.message : err),
                });
              } finally {
                this._confirmingCalibrate = false;
              }
            },
          })
        )
      );
    }
    const min = z.target_min ?? 10;
    const max = z.target_max ?? 500;
    // target_step 随载荷下发（web._serialize_lux_zone，R1 发现 10）——
    // ?? 5 仅为载荷缺字段时的兜底，不再各自硬编码
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
            text: "灯将自动 0→100→0 扫描，默认 5% 步长约 15~40 分钟；2%/3% 细扫最坏可能 2~3 小时（含传感器上报等待）。期间该区不可用。现在开始？" + staleNote,
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
