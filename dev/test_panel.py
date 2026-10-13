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

    # trigger REST write
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
        """() => (window.__servicePosts || []).some(p => p.path === '/api/fumiha_lighting/anl/trigger')""",
        timeout=4000,
    )
    check("anl trigger POSTs to own REST", True)

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

    # ---- standalone fallback (pages boot without the shell) ------------------
    page3 = browser.new_page(viewport={"width": 1280, "height": 900})
    page3.goto(base + "/?standalone=1")
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
