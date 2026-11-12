#!/usr/bin/env python3
"""Generate dashboard/dist/index.js — the web-dashboard plugin bundle — from desktop/plugin.js.

The desktop panel (desktop/plugin.js, Hermes Desktop `@hermes/plugin-sdk`) is the single source of truth for
the Mesa design and all the component logic. The web dashboard uses a different, IIFE-based SDK
(window.__HERMES_PLUGIN_SDK__). Rather than keep two copies in sync by hand, we reuse the desktop file's body
(everything between `const BOARD_KEY` and `export default`) verbatim and wrap it with a small adapter that
maps the desktop SDK surface (jsx/jsxs, useQuery/queryClient, ctx.rest, ScrollArea, SandboxedFrame, Badge,
Button, cn, relativeTime) onto the web SDK. Run after editing desktop/plugin.js:  python dashboard/build_web.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
body = src[src.index("const BOARD_KEY"): src.index("export default")].rstrip()

PREAMBLE = r"""var SDK = window.__HERMES_PLUGIN_SDK__;
if (!SDK) return;
var React = SDK.React;
var _H = SDK.hooks || {};
var useState = _H.useState, useEffect = _H.useEffect, useMemo = _H.useMemo, useRef = _H.useRef;
var _C = SDK.components || {};
var cn = (SDK.utils && SDK.utils.cn) || function () { return Array.prototype.filter.call(arguments, Boolean).join(" "); };
var relativeTime = (SDK.utils && SDK.utils.timeAgo) || function () { return ""; };
var API = "/api/plugins/hermes-proactive-radar";

/* jsx runtime shim (the desktop body is authored against react/jsx-runtime) */
function jsx(type, props, key) { return React.createElement(type, key === undefined ? props : Object.assign({}, props, { key: key })); }
var jsxs = jsx;
var Fragment = React.Fragment;

/* UI primitives: use the host DS where present, else a plain fallback that the Mesa CSS styles */
var Button = _C.Button || function (p) { var q = Object.assign({ type: "button", className: "rd-ghost" }, p); delete q.variant; delete q.size; return React.createElement("button", q, p.children); };
var Badge = _C.Badge || function (p) { return React.createElement("span", { className: "rd-chip" }, p.children); };
function ScrollArea(p) { return React.createElement("div", { className: cn("rd-scroll", p.className), style: { overflow: "auto", WebkitOverflowScrolling: "touch" } }, p.children); }
function Skeleton(p) { return React.createElement("div", { className: cn(p.className), style: { background: "var(--rd-panel)", borderRadius: "8px", height: "1.2em" } }); }
function SandboxedFrame(p) { return React.createElement("iframe", { src: p.src, title: p.title, className: p.className, sandbox: "allow-same-origin allow-popups", style: { border: 0, width: "100%", height: "100%" } }); }

/* react-query shim: a single active board query, refetch on interval and on invalidate */
var _activeRefetch = function () {};
function useQuery(opts) {
  var pair = useState({ data: null, loading: true, error: null });
  var st = pair[0], set = pair[1];
  var load = function () {
    return opts.queryFn().then(function (d) { set({ data: d, loading: false, error: null }); })
      .catch(function (e) { set(function (prev) { return { data: prev.data, loading: false, error: e }; }); });
  };
  useEffect(function () {
    _activeRefetch = load; load();
    var id = opts.refetchInterval ? setInterval(load, opts.refetchInterval) : 0;
    return function () { if (id) clearInterval(id); if (_activeRefetch === load) _activeRefetch = function () {}; };
  }, []);
  return { data: st.data, isLoading: st.loading, error: st.error, refetch: load };
}
var queryClient = { invalidateQueries: function () { try { _activeRefetch(); } catch (e) {} } };

/* ctx.rest shim over the web SDK's authenticated fetchJSON */
function _restImpl(path, init) {
  var url = API + path;
  if (!init) return SDK.fetchJSON(url);
  return SDK.fetchJSON(url, { method: init.method || "GET", headers: { "Content-Type": "application/json" }, body: init.body !== undefined ? JSON.stringify(init.body) : undefined });
}
"""

EPILOGUE = r"""
/* wire the shimmed data layer */
rest = _restImpl;

/* inject the Mesa stylesheet (same CSS as the desktop panel) */
try {
  if (!document.querySelector("style[data-radar-web]")) {
    var _s = document.createElement("style");
    _s.setAttribute("data-radar-web", "1");
    _s.textContent = STYLE;
    document.head.appendChild(_s);
  }
} catch (e) {}

if (window.__HERMES_PLUGINS__) window.__HERMES_PLUGINS__.register("hermes-proactive-radar", RadarPage);
"""

out = "/* GENERATED from desktop/plugin.js by dashboard/build_web.py — do not edit by hand. */\n(function () {\n\"use strict\";\n" + PREAMBLE + "\n/* ===== shared body (verbatim from desktop/plugin.js) ===== */\n" + body + "\n" + EPILOGUE + "\n})();\n"

dist = ROOT / "dashboard" / "dist"
dist.mkdir(parents=True, exist_ok=True)
(dist / "index.js").write_text(out, encoding="utf-8")
print(f"wrote {dist / 'index.js'} ({len(out)} bytes)")
