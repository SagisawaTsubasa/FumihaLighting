#!/usr/bin/env python3
"""Playwright smoke for BOTH Fumiha Lighting pages inside the shell (L2)."""

from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHOTS = Path(__file__).resolve().parent / "shots"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(
        ("  ok  " if cond else "FAIL  ")
        + name
        + (f": {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILURES.append(f"{name}: {detail}")


# ---- V1.5.0 条件显隐用 JS 片段（与既有用例同一 shadowRoot 穿透模式） ---------
OPEN_SETTINGS_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const card = el.shadowRoot && el.shadowRoot.querySelector('[data-manager="anl1"]');
    if (card) {
      const btn = card.querySelector('[data-action="settings"]');
      if (btn) { btn.click(); return true; }
    }
  }
  return false;
}"""

WAIT_SETTINGS_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d && d.querySelector('[data-extra-row]')) return true;
  }
  return false;
}"""

# 把当前设置弹窗与行状态读取器登记到 window，供后续步骤反复读行显隐
INSTALL_ROW_STATE_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d && d.querySelector('[data-extra-row]')) {
      window.__anlSettingsDialog = d;
      window.__anlRowState = () => {
        const keys = ['trigger_time', 'start_offset', 'end_time', 'end_offset',
          'sun_entity', 'day_brightness',
          'day_color_temp_kelvin',
          'transition_interval'];
        const out = {};
        keys.forEach((k) => {
          const row = d.querySelector('[data-anl-row="' + k + '"]');
          out[k] = !!row && row.style.display !== 'none';
          // F-063：可见行还须断 computed display 为 flex——dlgRow 行容器是
          // inline flex，'' 清内联会让行退化为 block，布尔显隐抓不到
          out[k + '_display'] = row ? getComputedStyle(row).display : null;
        });
        return out;
      };
      return true;
    }
  }
  return false;
}"""

CANCEL_DIALOG_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d) {
      const cancel = [...d.querySelectorAll('button')].find(
        (x) => x.textContent === '取消'
      );
      if (cancel) { cancel.click(); return true; }
    }
  }
  return false;
}"""

# dialog close 事件的 remove() 是排队任务：close 后节点会短暂残留 DOM——
# 连开两个弹窗前必须等旧节点真正移除，否则行状态捕获到旧弹窗（其保存键
# 在成功保存后保持 disabled，二次点击无效）
NO_DIALOG_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    if (el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog')) {
      return false;
    }
  }
  return true;
}"""

# V1.7.1 联动组 chip 点击（lux 区卡内 data-action=link-group）
LUX_CHIP_CLICK_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const card = el.shadowRoot && el.shadowRoot.querySelector('[data-zone="lux1"]');
    if (card) {
      const chip = card.querySelector('[data-action="link-group"]');
      if (chip) { chip.click(); return true; }
    }
  }
  return false;
}"""

WAIT_LINK_DIALOG_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d && d.querySelector('[data-lux-link-members]')) return true;
  }
  return false;
}"""

# 联动组弹窗状态读取（预填/重开复用；含 ANL-F-148 计算样式钉）
READ_LINK_DIALOG_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d && d.querySelector('[data-lux-link-members]')) {
      const val = (label) => {
        const inp = [...d.querySelectorAll('input')].find(
          (x) => x.getAttribute('aria-label') === label);
        return inp ? inp.value : null;
      };
      const note = d.querySelector('[data-lux-link-members]');
      return {
        title: d.querySelector('h2').textContent,
        members: note.textContent,
        group: val('联动组标签'),
        fade: val('交叉系数'),
        step: val('缓变步幅（%）'),
        // ANL-F-148 计算样式钉：Hub 壳路径链接的是 FumihaHub 仓的
        // tokens.css——计算样式断言真实校验跨仓同步，防两仓再漂移。
        // ANL-F-157 矩阵补全：基础 note 四项（fs/radius/无 accent 竖条/
        // 遮罩）一次读齐，warn 11.5px 由 ?cal=1 分支补第 5 项
        noteFs: getComputedStyle(note).fontSize,
        noteRadius: getComputedStyle(note).borderRadius,
        noteBorderLeftW: getComputedStyle(note).borderLeftWidth,
        backdrop: getComputedStyle(d, '::backdrop').backgroundColor,
        footnote: d.textContent.includes('仅显示已加载区域'),
      };
    }
  }
  return null;
}"""

# F-062：读当前设置弹窗里两个过渡输入的 title（CI 跳过提示 / 置灰提示）
READ_TRANS_TITLES_JS = """() => {
  const p = document.getElementById('hub');
  for (const el of p.shadowRoot.querySelectorAll('*')) {
    const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
    if (d && d.querySelector('[data-extra-row]')) {
      const pick = (k) => {
        const row = d.querySelector('[data-anl-row="' + k + '"]');
        const inp = row && row.querySelector('input');
        return inp ? inp.title : null;
      };
      return { start: pick('start_transition'), end: pick('end_transition') };
    }
  }
  return null;
}"""


def start_server() -> tuple[ThreadingHTTPServer, int]:
    spec = importlib.util.spec_from_file_location(
        "fl_serve", Path(__file__).parent / "serve.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.DevHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> int:
    from playwright.sync_api import sync_playwright

    server, port = start_server()
    base = f"http://127.0.0.1:{port}"
    SHOTS.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            run_checks(browser, base)
        finally:
            browser.close()
    server.shutdown()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall green")
    return 0


def wait_page_dom(page, selector: str, timeout_ms: int = 9000) -> None:
    page.wait_for_function(
        """(sel) => {
          const p = document.getElementById('hub');
          if (!p || !p.shadowRoot) return false;
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector(sel)) return true;
          }
          return false;
        }""",
        arg=selector,
        timeout=timeout_ms,
    )


def page_text(page, selector: str) -> str:
    return page.evaluate(
        """(sel) => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector(sel);
            if (card) return card.textContent;
          }
          return '';
        }""",
        selector,
    )


def run_checks(browser, base: str) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(base + "/")

    tabs = page.evaluate(
        """() => [...document.getElementById('hub').shadowRoot.querySelectorAll('.fx-tab')]
                 .map(t => ({badge: t.querySelector('.fx-badge').textContent, sel: t.getAttribute('aria-selected')}))"""
    )
    check(
        "two tabs with provider badges",
        [t["badge"] for t in tabs] == ["luxbalance", "anl"],
        str(tabs),
    )
    check("first tab auto-selected", tabs[0]["sel"] == "true", str(tabs))

    # ---- lux page (default) ------------------------------------------------
    wait_page_dom(page, '[data-zone="lux1"]')
    lux = page_text(page, '[data-zone="lux1"]')
    check(
        "lux: curve chips render",
        "最大增量 186.3 lx" in lux and "斜率 2.41" in lux,
        lux[:120],
    )
    check("lux: decisions render", "22:41:03" in lux, "")
    check("lux: target metric", "150 lx" in lux, "")
    # V1.3.0 联动组 chip：组标签+成员名单（page-lux.js _zoneCard）
    check(
        "lux: link group chip renders label and peers",
        "联动组「卫生间」" in lux and "与 干区 交叉扣除" in lux,
        lux[:200],
    )

    # V1.7.1 联动组面板内编辑：chip 点击 → 编辑弹窗（组员名单+三键现值）→
    # 保存 POST → chip 乐观更新（ANL-F-150：保存后无立即 _refresh，5s 轮询
    # 是唯一真相）
    page.evaluate(LUX_CHIP_CLICK_JS)
    page.wait_for_function(WAIT_LINK_DIALOG_JS, timeout=4000)
    link_dlg = page.evaluate(READ_LINK_DIALOG_JS)
    check(
        "lux V1.7.1: link dialog prefilled with members and current values",
        bool(link_dlg) and link_dlg["title"] == "卫生间 — 联动组"
        and "卫生间（本区）" in link_dlg["members"] and "干区" in link_dlg["members"]
        and link_dlg["group"] == "卫生间" and link_dlg["fade"] == "0.5"
        and link_dlg["step"] == "20",
        str(link_dlg),
    )
    # ANL-F-148 计算样式钉（Hub 壳路径）+ 名单脚注（裁定②）：10px 基础
    # 注记与 0.25 遮罩必须来自跨仓同步后的 FumihaHub tokens.css，断言防
    # 两仓再漂移（dev harness 走 Hub 路径，断言会真实校验跨仓同步生效）。
    # ANL-F-157 矩阵：基础 note 半径 8px + accent 竖条缺席（borderLeftWidth
    # 0px）——F-148 五项同步属性此前只钉 2 项，漂移可静默
    check(
        "lux V1.7.1: hub-path computed styles (note 10px / radius 8px / no accent"
        " bar / backdrop 0.25) + footnote",
        bool(link_dlg) and link_dlg["noteFs"] == "10px"
        and link_dlg["noteRadius"] == "8px"
        and link_dlg["noteBorderLeftW"] == "0px"
        and link_dlg["backdrop"] == "rgba(0, 0, 0, 0.25)"
        and link_dlg["footnote"] is True,
        str(link_dlg),
    )
    # 改组标签保存 → POST 三键 + 关弹窗 + chip 即时更新
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-lux-link-members]')) {
              const inp = [...d.querySelectorAll('input')].find(
                (x) => x.getAttribute('aria-label') === '联动组标签');
              inp.value = '卫生间2';
              [...d.querySelectorAll('button')].find(
                (b) => b.textContent === '保存').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some((p) => {
          if (p.path !== '/api/fumiha_lighting/lux/link_group') return false;
          const b = JSON.parse(p.body);
          return b.link_group === '卫生间2' && b.entry_id === 'lux1'
            && typeof b.cross_fade === 'number' && typeof b.max_step_pct === 'number';
        })""",
        timeout=4000,
    )
    check("lux V1.7.1: link dialog save POSTs three keys", True)
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-zone="lux1"]');
            if (card) {
              const chip = card.querySelector('[data-action="link-group"]');
              // 精确全文案（ANL-F-151）：新组无同组成员 → 后缀「（暂无同
              // 成员）」，乐观更新自保存响应（link_peers=null）
              return !!chip
                && chip.textContent === '联动组「卫生间2」（暂无同组成员）';
            }
          }
          return false;
        }""",
        timeout=9000,
    )
    check("lux V1.7.1: chip updates after save (exact, optimistic)", True)

    # ANL-F-151：保存后重开弹窗——名单必须按新组派生（只剩本区，不含
    # 干区）、三键预填新值；夹具名单硬编码时此处假绿
    page.wait_for_function(NO_DIALOG_JS, timeout=4000)
    page.evaluate(LUX_CHIP_CLICK_JS)
    page.wait_for_function(WAIT_LINK_DIALOG_JS, timeout=4000)
    reopen = page.evaluate(READ_LINK_DIALOG_JS)
    check(
        "lux V1.7.1: reopen dialog shows members derived from new group",
        bool(reopen) and reopen["title"] == "卫生间 — 联动组"
        and reopen["group"] == "卫生间2"
        and reopen["members"] == "组内成员：卫生间（本区）"
        and "干区" not in reopen["members"],
        str(reopen),
    )
    page.evaluate(CANCEL_DIALOG_JS)
    page.wait_for_function(NO_DIALOG_JS, timeout=4000)

    # target REST write
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-zone="lux1"]');
            if (card) {
              const input = card.querySelector('input[type="number"]');
              input.value = '200';
              [...card.querySelectorAll('button')].find(b => b.textContent === '应用目标').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some(p => p.path === '/api/fumiha_lighting/lux/target' && JSON.parse(p.body).value === 200)""",
        timeout=4000,
    )
    check("lux target POSTs to own REST", True)

    # calibrate REST write
    page.evaluate(
        """async () => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-zone="lux1"]');
            if (card) {
              const btn = [...card.querySelectorAll('button')].find(b => b.dataset.action === 'calibrate');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog')) return true;
          }
          return false;
        }""",
        timeout=4000,
    )
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) { [...d.querySelectorAll('button')].find(b => b.textContent === '开始校准').click(); return; }
          }
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some(p => p.path === '/api/fumiha_lighting/lux/calibrate')""",
        timeout=4000,
    )
    check("lux calibrate POSTs to own REST", True)

    # calibrate cancel (FL-IMP-26): ?cal=1 renders the calibrating card;
    # the danger button must post to the cancel endpoint via the dialog.
    page.evaluate("() => { location.search = '?cal=1'; }")
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector('[data-action="cancel-calibrate"]')) return true;
          }
          return false;
        }""",
        timeout=4000,
    )
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const btn = el.shadowRoot && el.shadowRoot.querySelector('[data-action="cancel-calibrate"]');
            if (btn) { btn.click(); return true; }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog')) return true;
          }
          return false;
        }""",
        timeout=4000,
    )
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) { [...d.querySelectorAll('button')].find(b => b.textContent === '中止').click(); return; }
          }
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some(p => p.path === '/api/fumiha_lighting/lux/calibrate/cancel')""",
        timeout=4000,
    )
    check("lux calibrate cancel POSTs to own REST", True)

    # ANL-F-149：?cal=1 下打开联动组弹窗必须有「保存会中止校准」警示行
    # （软提示路线：校准中保存经 reload 取消 _cal_task 且新 runtime 无痕）。
    # ANL-F-157 矩阵第 5 项（Hub 路径）：warn 变体 11.5px 不随基础 10px 缩小
    page.evaluate(LUX_CHIP_CLICK_JS)
    page.wait_for_function(WAIT_LINK_DIALOG_JS, timeout=4000)
    cal_warn = page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-lux-link-members]')) {
              const warn = d.querySelector('.cx-note.warn');
              return {
                text: d.textContent.includes(
                  '校准进行中：保存会重载条目并中止本次校准，已采集数据弃用'),
                warnFs: warn ? getComputedStyle(warn).fontSize : null,
              };
            }
          }
          return null;
        }"""
    )
    check(
        "lux ?cal=1: link dialog warns saving aborts calibration",
        bool(cal_warn) and cal_warn["text"],
        str(cal_warn),
    )
    check(
        "lux ?cal=1: hub-path warn note 11.5px (style matrix, ANL-F-157)",
        bool(cal_warn) and cal_warn["warnFs"] == "11.5px",
        str(cal_warn),
    )
    page.evaluate(CANCEL_DIALOG_JS)
    page.wait_for_function(NO_DIALOG_JS, timeout=4000)

    # ---- anl page (deep link) -----------------------------------------------
    page.evaluate("() => { location.hash = 'pn=anl.main'; }")
    wait_page_dom(page, '[data-manager="anl1"]')
    anl = page_text(page, '[data-manager="anl1"]')
    check(
        "anl: current mode + next anchor", "夜间" in anl and "23:30" in anl, anl[:120]
    )
    hallway = page_text(page, '[data-light="light.hallway"]')
    check(
        "anl: override badge + mismatch steps",
        "手动接管" in hallway and "连续偏离 2 步" in hallway,
        hallway[:100],
    )
    # ANL-F-046：夹具两字段分叉（transition_enabled=true / transitions_active=
    # false）——chip 必须读生效总闸，旧实现读 transition_enabled 会渲染 → 必红
    check(
        "anl: 起效总闸为假时不得宣称过渡带开启",
        "过渡带开启" not in page_text(page, '[data-manager="anl1"]'),
    )
    # F-061 钉（默认态）：CI 全关（day_ci_active=false）→ 摘要无恒照度 tag
    check("anl default: CI off → no 日间恒照度 tag", "日间恒照度" not in anl)

    # trigger REST write（FL-IMP-02: 新增确认框——先等对话框再点「执行」）
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const btn = el.shadowRoot && el.shadowRoot.querySelector('[data-action="trigger"]');
            if (btn) { btn.click(); return true; }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog')) return true;
          }
          return false;
        }""",
        timeout=4000,
    )
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) { [...d.querySelectorAll('button')].find(b => b.textContent === '执行').click(); return; }
          }
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some(p => p.path === '/api/fumiha_lighting/anl/trigger')""",
        timeout=4000,
    )
    check("anl trigger POSTs to own REST (with confirm dialog)", True)

    # ---- anl settings dialog (V1.4.0 config editor) --------------------------
    # 全局设置弹窗：⚙设置 → 改夜间亮度 → 保存 → POST /anl/config（patch.params）
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-manager="anl1"]');
            if (card) {
              const btn = card.querySelector('[data-action="settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    check("anl settings dialog opens with extras section", True)
    # ANL-F-031 钉：夹具 start_mode 故意是枚举外值 "noon"——必须追加展示且
    # 选中，不得静默回落首项「固定时间」
    noon_ok = page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) {
              const opt = d.querySelector('select option[value="noon"]');
              return !!opt && opt.selected;
            }
          }
          return false;
        }"""
    )
    check("anl settings dialog keeps out-of-enum anchor visible+selected", noon_ok)
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) {
              // 先把枚举外的 start_mode 修回合法值（用户视角：看见 noon 后修正）
              const sel = [...d.querySelectorAll('select')][0];
              sel.value = 'sunset';
              // 夜间亮度 = 值为 25 的 number 输入（fixture brightness）
              const inputs = [...d.querySelectorAll('input[type="number"]')];
              const b = inputs.find(i => i.value === '25');
              b.value = '18';
              [...d.querySelectorAll('button')].find(x => x.textContent === '保存设置').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const post = (window.__servicePosts || []).find(p => p.path === '/api/fumiha_lighting/anl/config');
          if (!post) return false;
          const patch = JSON.parse(post.body).patch;
          if (!(patch.params && patch.params.brightness === 18 && Array.isArray(patch.extras))) return false;
          // ANL-F-031 钉（后半）：保存载荷里的 start_mode 必须是用户修后的
          // sunset，而不是回落出来的 fixed
          return patch.times && patch.times.start_mode === 'sunset';
        }""",
        timeout=4000,
    )
    check("anl settings save POSTs config with params+extras", True)

    # ---- anl per-light dialog ------------------------------------------------
    # 逐灯弹窗：默认跟随全局（夹具 overrides={}）→ 取消勾选改亮度 → 保存 →
    # POST overrides upsert
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-light="light.bedroom"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    check("anl per-light dialog opens (follow-global default)", True)
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const cb = d.querySelector('[data-anl-follow-toggle]');
              cb.checked = false;
              cb.dispatchEvent(new Event('change'));
              const inputs = [...d.querySelectorAll('input[type="number"]')];
              inputs.find(i => i.value === '25').value = '12';
              [...d.querySelectorAll('button')].find(x => x.textContent === '保存').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const posts = (window.__servicePosts || []).filter(p => p.path === '/api/fumiha_lighting/anl/config');
          return posts.some(p => {
            const ovr = JSON.parse(p.body).patch.overrides
              && JSON.parse(p.body).patch.overrides['light.bedroom'];
            if (!ovr || ovr.brightness !== 12) return false;
            // ANL-F-033/F-027 钉：值=全局的行不写覆盖——夹具日间开启且
            // 日间输入=全局值、extras 行=全局值，覆盖里不得出现这两类键
            // ANL-F-047：夜间色温保持预填=全局值 → 也不得写入覆盖
            return ovr.color_temp_kelvin === undefined
              && ovr.day_brightness === undefined
              && ovr.day_color_temp_kelvin === undefined
              && ovr.extras === undefined;
          });
        }""",
        timeout=4000,
    )
    check("anl per-light override POSTs upsert (global-equal keys omitted)", True)

    # ---- ANL-F-034 钉：设置弹窗空值显式报错，不出 POST ----
    posts_before = page.evaluate("() => (window.__servicePosts || []).length")
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-manager="anl1"]');
            if (card) {
              const btn = card.querySelector('[data-action="settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) {
              // 清空夜间亮度（fixture 25）→ 保存 → 必须在 err 行报错且无 POST
              const b = [...d.querySelectorAll('input[type="number"]')]
                .find(i => i.value === '25');
              b.value = '';
              [...d.querySelectorAll('button')].find(x => x.textContent === '保存设置').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) {
              const err = d.querySelector('.fx-error');
              if (err && err.style.display !== 'none'
                  && err.textContent.includes('夜间亮度 不能为空')) return true;
            }
          }
          return false;
        }""",
        timeout=4000,
    )
    posts_after = page.evaluate("() => (window.__servicePosts || []).length")
    check("anl settings empty value errors loudly, no POST",
          posts_after == posts_before)
    page.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) {
              const cancel = [...d.querySelectorAll('button')].find(x => x.textContent === '取消');
              if (cancel) { cancel.click(); return; }
            }
          }
        }"""
    )
    page.wait_for_timeout(200)

    # ---- V1.5.0 条件显隐：行注册表 + 单一 _syncVisibility ---------------------
    page.evaluate(OPEN_SETTINGS_JS)
    page.wait_for_function(WAIT_SETTINGS_JS, timeout=9000)
    page.evaluate(INSTALL_ROW_STATE_JS)
    vis1 = page.evaluate("() => window.__anlRowState()")
    check(
        "anl visibility: non-fixed start hides trigger_time, shows start_offset",
        bool(vis1) and not vis1["trigger_time"] and vis1["start_offset"],
        str(vis1),
    )
    check(
        "anl visibility: non-fixed end hides end_time, shows end_offset",
        bool(vis1) and not vis1["end_time"] and vis1["end_offset"],
        str(vis1),
    )
    check(
        "anl visibility: any non-fixed anchor shows sun_entity row",
        bool(vis1) and vis1["sun_entity"],
        str(vis1),
    )
    # V1.7.0 反查推导：虚拟灯行整体删除（CI 面清零），日间参数行恒显
    vl_row_gone = page.evaluate(
        """() => {
          const d = window.__anlSettingsDialog;
          return !d.querySelector('[data-anl-row="day_ci_virtual_light"]');
        }"""
    )
    check(
        "anl visibility: day on shows day rows, virtual light row removed (V1.7.0)",
        bool(vis1) and vis1["day_brightness"]
        and vis1["day_color_temp_kelvin"] and vl_row_gone
        and vis1["day_brightness_display"] == "flex",
        str(vis1) + " / vl_row_gone=" + str(vl_row_gone),
    )
    check(
        "anl visibility: zero transitions hides transition_interval row",
        bool(vis1) and not vis1["transition_interval"],
        str(vis1),
    )

    # start_mode → fixed：换出固定时间行，藏偏移行
    page.evaluate(
        """() => {
          const sel = [...window.__anlSettingsDialog.querySelectorAll('select')][0];
          sel.value = 'fixed';
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }"""
    )
    vis2 = page.evaluate("() => window.__anlRowState()")
    check(
        "anl visibility: start fixed swaps in trigger_time, hides start_offset",
        bool(vis2) and vis2["trigger_time"] and not vis2["start_offset"]
        and vis2["trigger_time_display"] == "flex",
        str(vis2),
    )
    check(
        "anl visibility: end still non-fixed keeps sun_entity row",
        bool(vis2) and vis2["sun_entity"],
        str(vis2),
    )

    # end_mode → fixed：两锚点全 fixed → sun_entity 行隐藏
    page.evaluate(
        """() => {
          const sel = [...window.__anlSettingsDialog.querySelectorAll('select')][1];
          sel.value = 'fixed';
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }"""
    )
    vis3 = page.evaluate("() => window.__anlRowState()")
    check(
        "anl visibility: end fixed swaps in end_time, hides end_offset",
        bool(vis3) and vis3["end_time"] and not vis3["end_offset"],
        str(vis3),
    )
    check(
        "anl visibility: both anchors fixed hides sun_entity row",
        bool(vis3) and not vis3["sun_entity"],
        str(vis3),
    )

    # start_mode → sunset：反转回偏移行，sun_entity 复现
    page.evaluate(
        """() => {
          const sel = [...window.__anlSettingsDialog.querySelectorAll('select')][0];
          sel.value = 'sunset';
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }"""
    )
    vis4 = page.evaluate("() => window.__anlRowState()")
    check(
        "anl visibility: start sunset swaps back to offset row + sun_entity",
        bool(vis4) and not vis4["trigger_time"] and vis4["start_offset"]
        and vis4["sun_entity"],
        str(vis4),
    )

    # day_enabled 关：CI 行与日间参数行全藏，end_transition 置灰（ANL-F-041）
    page.evaluate(
        """() => {
          const d = window.__anlSettingsDialog;
          const cb = d.querySelector('[data-anl-row="day_enabled"] input[type="checkbox"]');
          cb.checked = false;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }"""
    )
    vis7 = page.evaluate("() => window.__anlRowState()")
    end_trans = page.evaluate(
        """() => {
          const row = window.__anlSettingsDialog.querySelector(
            '[data-anl-row="end_transition"]');
          return {
            shown: !!row && row.style.display !== 'none',
            disabled: !!row && row.querySelector('input').disabled,
          };
        }"""
    )
    check(
        "anl visibility: day off hides day rows, end_transition stays greyed",
        bool(vis7) and not vis7["day_brightness"]
        and end_trans["shown"] and end_trans["disabled"],
        str(vis7) + " / " + str(end_trans),
    )
    # 恢复 day_enabled（后续保存测试回到日间开启态）
    page.evaluate(
        """() => {
          const d = window.__anlSettingsDialog;
          const cb = d.querySelector('[data-anl-row="day_enabled"] input[type="checkbox"]');
          cb.checked = true;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }"""
    )

    # transition_interval 行：入夜过渡改 >0 后出现
    page.evaluate(
        """() => {
          const d = window.__anlSettingsDialog;
          const st = d.querySelector('[data-anl-row="start_transition"] input[type="number"]');
          st.value = '30';
          st.dispatchEvent(new Event('input', { bubbles: true }));
          return true;
        }"""
    )
    vis8 = page.evaluate("() => window.__anlRowState()")
    check(
        "anl visibility: start_transition>0 reveals transition_interval row",
        bool(vis8) and vis8["transition_interval"],
        str(vis8),
    )

    # 保存发键断言（V1.7.0 反查推导）：全局弹窗零 CI 键——day_ci_lights
    # 缺省（编辑面在逐灯弹窗，patch 语义=不动存量清单）、虚拟灯键废除
    page.evaluate(
        """() => {
          const d = window.__anlSettingsDialog;
          [...d.querySelectorAll('button')].find((x) => x.textContent === '保存设置').click();
          return true;
        }"""
    )
    page.wait_for_function(
        """() => (window.__servicePosts || []).some((p) => {
          if (p.path !== '/api/fumiha_lighting/anl/config') return false;
          const params = JSON.parse(p.body).patch.params;
          return params && !("day_ci_lights" in params)
            && !("day_ci_virtual_light" in params);
        })""",
        timeout=4000,
    )
    check("anl settings save POSTs zero CI keys (V1.7.0)", True)

    page.evaluate(CANCEL_DIALOG_JS)
    page.wait_for_timeout(200)

    # ---- module failure isolation: one bad page must not kill the other ----
    page2 = browser.new_page(viewport={"width": 1280, "height": 900})
    page2.goto(base + "/?badanl=1")
    page2.evaluate("() => { location.hash = 'pn=anl.main'; }")
    page2.wait_for_timeout(900)
    iso = page2.evaluate(
        """() => {
          const p = document.getElementById('hub');
          const err = p.shadowRoot.querySelector('.fx-error');
          const luxTab = [...p.shadowRoot.querySelectorAll('.fx-tab')].find(t => t.dataset.pageId === 'luxbalance.main');
          return {err: !!err, luxTab: !!luxTab};
        }"""
    )
    check(
        "bad anl module: error card + lux tab intact",
        iso["err"] and iso["luxTab"],
        str(iso),
    )
    page2.close()

    # ---- ANL-F-035 钉：badconfig 下弹窗构建期异常必须落成 ack toast ----
    page4 = browser.new_page(viewport={"width": 1280, "height": 900})
    page4.goto(base + "/?badconfig=1")
    page4.evaluate("() => { location.hash = 'pn=anl.main'; }")
    wait_page_dom(page4, '[data-manager="anl1"]')
    page4.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-manager="anl1"]');
            if (card) {
              const btn = card.querySelector('[data-action="settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page4.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const toast = el.shadowRoot && el.shadowRoot.querySelector('.fx-toast');
            if (toast && toast.textContent.includes('打开设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    check("anl badconfig: dialog build failure surfaces as ack toast", True)
    page4.close()

    # ---- V1.5.0 ?ci=1 夹具：manager 摘要 tag + 逐灯弹窗日间段接管说明 ----------
    page5 = browser.new_page(viewport={"width": 1280, "height": 900})
    page5.goto(base + "/?ci=1")
    page5.evaluate("() => { location.hash = 'pn=anl.main'; }")
    wait_page_dom(page5, '[data-manager="anl1"]')
    anl5 = page_text(page5, '[data-manager="anl1"]')
    check(
        "anl ?ci=1: manager summary shows 日间恒照度 tag",
        "日间恒照度" in anl5,
        anl5[:120],
    )

    # 逐灯弹窗：CI 接管 → 日间参数段不渲染，改显恒照度说明 note
    page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-light="light.bedroom"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page5.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    ci_note = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              return {
                headers: [...d.querySelectorAll('h3')].map((h) => h.textContent),
                hasNote: d.textContent.includes('恒照度闭环接管'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl ?ci=1: per-light dialog hides day section, shows CI takeover note",
        bool(ci_note) and "日间参数" not in ci_note["headers"]
        and "夜间参数" in ci_note["headers"] and ci_note["hasNote"],
        str(ci_note),
    )
    page5.evaluate(CANCEL_DIALOG_JS)
    page5.wait_for_timeout(200)

    # V1.6.0 逐灯分辨钉：同组非接管灯（hallway 不在清单）→ 日间段照常渲染
    page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-light="light.hallway"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page5.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    hallway = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              return {
                headers: [...d.querySelectorAll('h3')].map((h) => h.textContent),
                hasNote: d.textContent.includes('恒照度闭环接管'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl ?ci=1: non-CI light dialog keeps day section, no takeover note",
        bool(hallway) and "日间参数" in hallway["headers"] and not hallway["hasNote"],
        str(hallway),
    )

    # V1.7.0 zone 归属钉：hallway ∉恒照度区域（ci_zone_name=null）→ 开关
    # 禁用 + 归属提示；旧「勾选保存 POST 整表」路径对无区域灯不再可达
    # （禁用态挡新勾选，服务端 zone 归属 400 兜底）
    toggle_state = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              return {
                exists: !!t,
                checked: !!t && t.checked,
                disabled: !!t && t.disabled,
                hint: d.textContent.includes('此灯不属于任何恒照度区域'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl V1.7.0: hallway toggle disabled with no-zone hint (zone membership)",
        bool(toggle_state) and toggle_state["exists"] and not toggle_state["checked"]
        and toggle_state["disabled"] and toggle_state["hint"],
        str(toggle_state),
    )
    # V1.7.0：hallway 弹窗不再经保存关闭（禁用态无处可存）——显式取消
    page5.evaluate(CANCEL_DIALOG_JS)
    page5.wait_for_timeout(200)

    # ANL-F-132 三态钉：balcony ∈恒照度区域但未勾选 → 开关可勾选（接管
    # 唯一新开入口；旧码 ci_zone_name 只发清单内灯，此格恒禁用=新开接管
    # 在 UI 上不存在）+「勾选后由该区闭环接管」提示
    page5.wait_for_function(NO_DIALOG_JS, timeout=4000)
    page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-light="light.balcony"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page5.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    balcony_toggle = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              return {
                exists: !!t,
                checked: !!t && t.checked,
                disabled: !!t && t.disabled,
                hint: d.textContent.includes('属于恒照度区域「卫生间」')
                  && d.textContent.includes('勾选后由该区闭环接管'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl V1.7.0 F-132: in-zone unchecked light enabled with opt-in hint",
        bool(balcony_toggle) and balcony_toggle["exists"]
        and not balcony_toggle["checked"] and not balcony_toggle["disabled"]
        and balcony_toggle["hint"],
        str(balcony_toggle),
    )
    # V1.7.1 换行钉：三态说明挂行下块级 cx-note——不在开关所在的 flex 行内
    # （V1.7.0 错用 dlgRow 第三参数位与开关同级并排的用户反馈回归钉）
    ci_hint_layout = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              const hint = d.querySelector('[data-anl-ci-hint]');
              if (!t || !hint) return { toggle: !!t, hint: !!hint };
              const rowEl = t.parentElement;
              return {
                toggle: true,
                hint: true,
                inlineWithToggle: hint.parentElement === rowEl,
                belowToggle: hint.parentElement === rowEl.parentElement
                  && hint.previousElementSibling === rowEl,
                block: getComputedStyle(hint).display === 'block',
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl V1.7.1: CI hint is a block note below the toggle row (not inline)",
        bool(ci_hint_layout) and ci_hint_layout.get("toggle")
        and ci_hint_layout.get("hint") and ci_hint_layout["inlineWithToggle"] is False
        and ci_hint_layout["belowToggle"] is True and ci_hint_layout["block"] is True,
        str(ci_hint_layout),
    )
    page5.evaluate(CANCEL_DIALOG_JS)
    page5.wait_for_timeout(200)

    # bedroom（已接管）弹窗：开关预勾；取消保存 → POST 空清单 + overrides_remove
    page5.wait_for_function(NO_DIALOG_JS, timeout=4000)
    page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-light="light.bedroom"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page5.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    bedroom_toggle = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              return !!t && t.checked;
            }
          }
          return null;
        }"""
    )
    bedroom_zone = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              return {
                checked: !!t && t.checked,
                enabled: !!t && !t.disabled,
                hint: d.textContent.includes('由恒照度区域「卫生间」接管'),
              };
            }
          }
          return null;
        }"""
    )
    check("anl V1.6.1: bedroom per-light toggle prefilled checked", bedroom_toggle is True)
    check(
        "anl V1.7.0: bedroom toggle enabled with zone hint (B2 annotation)",
        bool(bedroom_zone) and bedroom_zone["checked"] and bedroom_zone["enabled"]
        and bedroom_zone["hint"],
        str(bedroom_zone),
    )
    page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              const t = d.querySelector('[data-anl-ci-toggle]');
              t.checked = false;
              [...d.querySelectorAll('button')].find((x) => x.textContent === '保存').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page5.wait_for_function(
        """() => (window.__servicePosts || []).some((p) => {
          if (p.path !== '/api/fumiha_lighting/anl/config') return false;
          const patch = JSON.parse(p.body).patch;
          return patch.params && Array.isArray(patch.params.day_ci_lights)
            && patch.params.day_ci_lights.length === 0
            && Array.isArray(patch.overrides_remove)
            && patch.overrides_remove.includes('light.bedroom');
        })""",
        timeout=4000,
    )
    check("anl V1.6.1: bedroom toggle-off POSTs empty list + overrides_remove", True)
    page5.evaluate(CANCEL_DIALOG_JS)
    page5.wait_for_timeout(200)

    # 全局设置弹窗（?ci=1）：无清单行（V1.6.1 移逐灯）；存量接管下日间行
    # 恒显（F-104）且虚拟灯行可见并预填
    page5.evaluate(OPEN_SETTINGS_JS)
    page5.wait_for_function(WAIT_SETTINGS_JS, timeout=9000)
    ci_state = page5.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-extra-row]')) {
              const row = (k) => d.querySelector('[data-anl-row="' + k + '"]');
              const vis = (k) => { const r = row(k); return !!r && r.style.display !== 'none'; };
              return {
                noListRow: !row('day_ci_lights'),
                dayShown: vis('day_brightness') && vis('day_color_temp_kelvin'),
                virtualGone: !row('day_ci_virtual_light'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl ?ci=1: settings dialog keeps day rows (F-104), virtual light row removed (V1.7.0)",
        bool(ci_state) and ci_state["noListRow"] and ci_state["dayShown"]
        and ci_state["virtualGone"],
        str(ci_state),
    )

    # F-062 钉：CI 开 → 两个过渡输入挂「日侧过渡带被跳过」悬停提示
    trans_titles = page5.evaluate(READ_TRANS_TITLES_JS)
    ci_skip_note = "日间恒照度开启时，日侧过渡带对被接管的灯跳过，此值暂不生效"
    check(
        "anl ?ci=1: CI on puts skip tooltip on both transition inputs",
        bool(trans_titles) and trans_titles["start"] == ci_skip_note
        and trans_titles["end"] == ci_skip_note,
        str(trans_titles),
    )
    check(
        "anl ?ci=1: CI tooltips steady while takeover list persists (V1.6.1)",
        bool(trans_titles) and trans_titles["start"] == ci_skip_note,
        str(trans_titles),
    )
    page5.evaluate(CANCEL_DIALOG_JS)
    page5.wait_for_timeout(200)
    page5.close()

    # ---- V1.5.0 F-061 钉：?cibroken=1 存储开但未配灯 → 不得谎报接管 ----------
    page6 = browser.new_page(viewport={"width": 1280, "height": 900})
    page6.goto(base + "/?cibroken=1")
    page6.evaluate("() => { location.hash = 'pn=anl.main'; }")
    wait_page_dom(page6, '[data-manager="anl1"]')
    anl6 = page_text(page6, '[data-manager="anl1"]')
    check(
        "anl ?cibroken=1: manager summary has no 日间恒照度 tag",
        "日间恒照度" not in anl6,
        anl6[:120],
    )

    # 逐灯弹窗：生效值 false → 日间段照常渲染，无恒照度接管说明
    page6.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot
              && el.shadowRoot.querySelector('[data-light="light.bedroom"]');
            if (card) {
              const btn = card.querySelector('[data-action="light-settings"]');
              if (btn) { btn.click(); return true; }
            }
          }
          return false;
        }"""
    )
    page6.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    broken_note = page6.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.textContent.includes('跟随全局设置')) {
              return {
                headers: [...d.querySelectorAll('h3')].map((h) => h.textContent),
                hasNote: d.textContent.includes('恒照度闭环接管'),
              };
            }
          }
          return null;
        }"""
    )
    check(
        "anl ?cibroken=1: per-light dialog keeps day section, no CI note",
        bool(broken_note) and "日间参数" in broken_note["headers"]
        and "夜间参数" in broken_note["headers"] and not broken_note["hasNote"],
        str(broken_note),
    )
    page6.evaluate(CANCEL_DIALOG_JS)
    page6.wait_for_timeout(200)
    page6.close()

    # ---- R1 发现 11：?caldone=1 桩返回 cancelled:false → FL-IMP-24 分支
    # （「校准已自行结束」文案）在 L2 可达（真实取消端点契约对齐）----------
    page7 = browser.new_page(viewport={"width": 1280, "height": 900})
    page7.goto(base + "/?cal=1&caldone=1")
    page7.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            if (el.shadowRoot && el.shadowRoot.querySelector('[data-action="cancel-calibrate"]')) return true;
          }
          return false;
        }""",
        timeout=9000,
    )
    page7.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const btn = el.shadowRoot && el.shadowRoot.querySelector('[data-action="cancel-calibrate"]');
            if (btn) { btn.click(); return true; }
          }
          return false;
        }"""
    )
    page7.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d) { const b = [...d.querySelectorAll('button')].find(x => x.textContent === '中止'); if (b) { b.click(); return true; } }
          }
          return false;
        }""",
        timeout=4000,
    )
    page7.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const toast = el.shadowRoot && el.shadowRoot.querySelector('.fx-toast');
            if (toast && toast.textContent.includes('校准已自行结束')) return true;
          }
          return false;
        }""",
        timeout=4000,
    )
    check("lux caldone=1: cancelled:false surfaces FL-IMP-24 branch (R1 #11)", True)
    page7.close()

    # ---- ANL-F-150：?reloadgap=1 — POST 后第一次 overview 返回空 zones
    # （reload 出桶窗口）。修后保存无立即 _refresh()：断言窗口内不闪空态、
    # chip 由保存响应乐观更新；旧码立即 GET 会当场吃下空态+旧 chip
    page8 = browser.new_page(viewport={"width": 1280, "height": 900})
    page8.goto(base + "/?reloadgap=1")
    wait_page_dom(page8, '[data-zone="lux1"]')
    page8.evaluate(LUX_CHIP_CLICK_JS)
    page8.wait_for_function(WAIT_LINK_DIALOG_JS, timeout=4000)
    page8.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const d = el.shadowRoot && el.shadowRoot.querySelector('dialog.fx-dialog');
            if (d && d.querySelector('[data-lux-link-members]')) {
              const inp = [...d.querySelectorAll('input')].find(
                (x) => x.getAttribute('aria-label') === '联动组标签');
              inp.value = '卫生间3';
              [...d.querySelectorAll('button')].find(
                (b) => b.textContent === '保存').click();
              return true;
            }
          }
          return false;
        }"""
    )
    page8.wait_for_function(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot && el.shadowRoot.querySelector('[data-zone="lux1"]');
            if (card) {
              const chip = card.querySelector('[data-action="link-group"]');
              return !!chip
                && chip.textContent === '联动组「卫生间3」（暂无同组成员）';
            }
          }
          return false;
        }""",
        timeout=4000,
    )
    page8.wait_for_timeout(5500)
    # ANL-F-155：等待跨过一次 5s 轮询（夹具空窗 7s，断言确定性落在窗内）。
    # 旧钉 400ms 在轮询发生前就收口，空分支从未被消费=只证明「无立即 GET」；
    # 现断言卡片仍在、chip 仍为新组——grace 落地前此断言红（正是 reload
    # 窗口残留的证明），落地后转绿
    reloadgap_ok = page8.evaluate(
        """() => {
          const p = document.getElementById('hub');
          for (const el of p.shadowRoot.querySelectorAll('*')) {
            const card = el.shadowRoot
              && el.shadowRoot.querySelector('[data-zone="lux1"]');
            if (card) {
              const chip = card.querySelector('[data-action="link-group"]');
              return !el.shadowRoot.textContent.includes('尚无区域')
                && !!chip
                && chip.textContent === '联动组「卫生间3」（暂无同组成员）';
            }
          }
          return false;
        }"""
    )
    check(
        "lux reloadgap: card+chip survive poll inside reload window (grace)",
        reloadgap_ok,
    )
    page8.close()

    # ---- standalone fallback (pages boot without the shell) ------------------
    page3 = browser.new_page(viewport={"width": 1280, "height": 900})
    # &cal=1（ANL-F-154 钉载体）：standalone 路径链接本仓 tokens.css，
    # 卡片带 .cx-note.warn（校准进行中）供计算样式断言
    page3.goto(base + "/?standalone=1&cal=1")
    page3.evaluate("""async () => {
      for (const m of ['page-lux.js', 'page-anl.js']) {
        await import('/fumiha_lighting/' + m + '?ver=1.0.0');
      }
      const luxEl = document.createElement('lux-balance-page');
      luxEl.id = 'luxpage';
      document.body.appendChild(luxEl);
      const anlEl = document.createElement('anl-page');
      anlEl.id = 'anlpage';
      document.body.appendChild(anlEl);
      await new Promise(r => setTimeout(r, 100));
      document.getElementById('luxpage').hass = window.__mockHass;
      document.getElementById('anlpage').hass = window.__mockHass;
    }""")
    page3.wait_for_function(
        """() => {
          const l = document.getElementById('luxpage');
          const a = document.getElementById('anlpage');
          return l && l.shadowRoot && l.shadowRoot.querySelector('[data-zone="lux1"]')
            && a && a.shadowRoot && a.shadowRoot.querySelector('[data-manager="anl1"]');
        }""",
        timeout=9000,
    )
    check("standalone fallback: both pages boot from vendored kit", True)
    # ANL-F-154 计算样式钉（standalone 路径链接本仓 tokens.css）：warn
    # 注记 11.5px 不随基础 10px 缩小（告警不得比正文更小）、竖条删除后
    # 基础圆角归一 8px（Hub 半边由主会话二次跨仓同步后同绿）
    warn_style = page3.evaluate(
        """() => {
          const l = document.getElementById('luxpage');
          const n = l && l.shadowRoot
            && l.shadowRoot.querySelector('.cx-note.warn');
          if (!n) return null;
          const cs = getComputedStyle(n);
          return {
            fs: cs.fontSize,
            radius: cs.borderTopLeftRadius + '/' + cs.borderTopRightRadius,
          };
        }"""
    )
    check(
        "standalone tokens: warn note 11.5px + radius 8px (ANL-F-154)",
        bool(warn_style) and warn_style["fs"] == "11.5px"
        and warn_style["radius"] == "8px/8px",
        str(warn_style),
    )
    # ANL-F-157 矩阵（standalone 路径补全）：基础 .cx-note 10px + accent
    # 竖条缺席（borderLeftWidth 0px）+ 基础半径 8px + 遮罩 0.25——基础面
    # 此前零断言（Hub 半边的同项见 page1 弹窗读）。cal 态卡片只有 warn
    # 变体，基础 note 载体=联动组弹窗 membersNote
    page3.evaluate(
        """() => {
          const l = document.getElementById('luxpage');
          const chip = l.shadowRoot.querySelector(
            '[data-zone] [data-action="link-group"]');
          if (chip) chip.click();
        }"""
    )
    page3.wait_for_function(
        """() => {
          const l = document.getElementById('luxpage');
          return !!(l && l.shadowRoot
            && l.shadowRoot.querySelector('dialog.fx-dialog [data-lux-link-members]'));
        }""",
        timeout=4000,
    )
    base_style = page3.evaluate(
        """() => {
          const l = document.getElementById('luxpage');
          const d = l.shadowRoot.querySelector('dialog.fx-dialog');
          const note = d.querySelector('[data-lux-link-members]');
          const cs = getComputedStyle(note);
          return {
            fs: cs.fontSize,
            blw: cs.borderLeftWidth,
            radius: cs.borderRadius,
            backdrop: getComputedStyle(d, '::backdrop').backgroundColor,
          };
        }"""
    )
    check(
        "standalone tokens: base note 10px + no accent bar + backdrop 0.25"
        " (style matrix, ANL-F-157)",
        bool(base_style) and base_style["fs"] == "10px"
        and base_style["blw"] == "0px" and base_style["radius"] == "8px"
        and base_style["backdrop"] == "rgba(0, 0, 0, 0.25)",
        str(base_style),
    )
    page3.evaluate(
        """() => {
          const l = document.getElementById('luxpage');
          const dlg = l.shadowRoot.querySelector('dialog.fx-dialog');
          if (dlg) dlg.close();
        }"""
    )
    page3.close()

    check("no page errors so far", not errors, "; ".join(errors[:3]))

    shots = [
        ("/", "fl-light-desktop.png", 1280, 900),
        ("/?dark=1", "fl-dark-desktop.png", 1280, 900),
        ("/", "fl-light-narrow.png", 390, 844),
        ("/?dark=1", "fl-dark-narrow.png", 390, 844),
    ]
    for path, name, w, h in shots:
        sp = browser.new_page(viewport={"width": w, "height": h})
        sp.goto(base + path)
        sp.wait_for_timeout(900)
        sp.screenshot(path=str(SHOTS / name), full_page=False)
        sp.close()
        check(f"shot {name}", (SHOTS / name).is_file())

    page.close()


if __name__ == "__main__":
    raise SystemExit(main())
