#!/usr/bin/env python3
"""Offline dev server for the Fumiha Lighting pages (Fumiha Hub harness).

    /                      -> dev/index.html  (harness with mockHass)
    /fumiha_hub/*          -> ../FumihaHub/custom_components/fumiha_hub/static/*
    /fumiha_lighting/*     -> ../custom_components/fumiha_lighting/static/*

Usage: python dev/serve.py [port] (default 8773).
"""

from __future__ import annotations

import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEV_DIR.parent
STATIC_DIR = REPO_ROOT / "custom_components" / "fumiha_lighting" / "static"
HUB_STATIC = (
    REPO_ROOT.parent / "FumihaHub" / "custom_components" / "fumiha_hub" / "static"
)

DEFAULT_PORT = 8773


class DevHandler(SimpleHTTPRequestHandler):
    def _safe(self, root: Path, rel: str) -> str:
        resolved = (root / rel).resolve()
        if resolved != root and root not in resolved.parents:
            return str((DEV_DIR / "__escaped__").resolve())
        return str(resolved)

    def list_directory(self, path: str):
        self.send_error(404, "Directory listing not supported")

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        path = urllib.parse.unquote(path)
        if "\x00" in path:
            return str((DEV_DIR / "__escaped__").resolve())
        if path.startswith("/fumiha_hub/"):
            return self._safe(HUB_STATIC, path[len("/fumiha_hub/") :])
        if path.startswith("/fumiha_lighting/"):
            return self._safe(STATIC_DIR, path[len("/fumiha_lighting/") :])
        if path in ("", "/"):
            return str(DEV_DIR / "index.html")
        return self._safe(DEV_DIR, path.lstrip("/"))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("[dev] %s\n" % (format % args))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), DevHandler)
    print(
        f"[dev] Fumiha Lighting harness on http://127.0.0.1:{port}/  (Ctrl+C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
