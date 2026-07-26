"""The HUD document, returned as one self-contained HTML string.

Everything — markup, CSS, SVG and script — lives in this single string. It is
handed straight to ``webview.create_window(html=...)``, so there is no asset
directory to resolve, no ``file://`` path to get wrong inside a PyInstaller
bundle, and nothing is fetched over the network. Open the packaged .exe on a
machine with no internet and the interface still looks exactly like this.

Only two things are injected from Python: the accent colour and a small boot
payload's worth of nothing — the rest of the state arrives at runtime over the
js_api bridge in :mod:`jarvis.ui.window`.
"""

from __future__ import annotations

from jarvis.config import config

_FALLBACK_ACCENT = (77, 208, 225)  # #4dd0e1, arc-reactor cyan


def _parse_hex(colour: str) -> tuple[int, int, int]:
    """Read ``#rgb`` / ``#rrggbb`` into a triplet, falling back on nonsense.

    Parsing rather than interpolating the raw config string also means a
    malformed ``theme_accent`` cannot inject CSS into the document.
    """
    value = (colour or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return _FALLBACK_ACCENT
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return _FALLBACK_ACCENT


def hud_html(accent: str | None = None) -> str:
    """Build the HUD document. ``accent`` defaults to ``config.ui.theme_accent``."""
    r, g, b = _parse_hex(accent if accent is not None else config.ui.theme_accent)
    return (
        _TEMPLATE
        .replace("__ACCENT__", f"#{r:02x}{g:02x}{b:02x}")
        .replace("__ACCENT_RGB__", f"{r}, {g}, {b}")
    )


# --------------------------------------------------------------------------
# The document. Placeholders are __ACCENT__ and __ACCENT_RGB__.
# --------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-state="idle" data-voice="off" data-assistant="jarvis">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>J.A.R.V.I.S.</title>
<style>
:root{
  --accent:__ACCENT__;
  --accent-rgb:__ACCENT_RGB__;
  --bg:#080c10;
  --bg-rail:#070a0e;
  --text:#c3d0d6;
  --text-dim:#728790;
  --text-faint:#4a5b63;
  --danger:#ff705f;
  --line:rgba(var(--accent-rgb),0.15);
  --line-soft:rgba(255,255,255,0.055);
  --mono:"Cascadia Mono",Consolas,"Segoe UI Mono","Lucida Console",monospace;
  --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);
  color:var(--text);
  font:14px/1.6 var(--sans);
  overflow:hidden;
  display:grid;
  grid-template-rows:38px 1fr auto;
  -webkit-font-smoothing:antialiased;
}
/* Ambient light + a very faint technical grid. Restrained on purpose:
   it should read as depth, not as decoration. */
body::before{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(760px 520px at 14% 86%,rgba(var(--accent-rgb),0.055),transparent 62%),
    radial-gradient(620px 460px at 92% 4%,rgba(var(--accent-rgb),0.028),transparent 60%),
    linear-gradient(rgba(255,255,255,0.013) 1px,transparent 1px) 0 0/48px 48px,
    linear-gradient(90deg,rgba(255,255,255,0.013) 1px,transparent 1px) 0 0/48px 48px;
}
body>*{position:relative;z-index:1}

/* ---------------- titlebar ---------------- */
.titlebar{
  display:flex;align-items:stretch;
  border-bottom:1px solid var(--line-soft);
  background:rgba(255,255,255,0.012);
  user-select:none;
}
.brand{
  flex:1;display:flex;align-items:center;gap:10px;padding:0 14px;
  -webkit-app-region:drag;cursor:default;
}
.mark{width:9px;height:9px;flex:none;transform:rotate(45deg);
  border:1px solid var(--accent);
  box-shadow:0 0 7px rgba(var(--accent-rgb),0.55);
  animation:markPulse 4s ease-in-out infinite;
}
.wordmark{font:600 11px/1 var(--mono);letter-spacing:.34em;color:var(--text-dim)}
.sub{font:10px/1 var(--mono);letter-spacing:.16em;color:var(--text-faint);
  margin-left:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* assistant switcher — flip between local JARVIS and cloud Claude */
.aiswitch{display:flex;align-items:center;gap:0;padding:0 8px;
  -webkit-app-region:no-drag}
.seg{
  border:1px solid var(--line-soft);background:none;color:var(--text-faint);
  font:9px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;
  padding:6px 11px;cursor:default;transition:color .15s,border-color .15s,background .15s;
}
.seg:first-child{border-radius:3px 0 0 3px;border-right:0}
.seg:last-child{border-radius:0 3px 3px 0}
.seg:hover{color:var(--text-dim)}
.seg.active{color:var(--accent);border-color:rgba(var(--accent-rgb),0.5);
  background:rgba(var(--accent-rgb),0.1)}

.winbtns{display:flex}
.winbtn{
  width:44px;border:0;background:none;color:var(--text-faint);
  font:12px/1 var(--mono);cursor:default;transition:background .14s,color .14s;
}
.winbtn:hover{background:rgba(255,255,255,0.06);color:var(--text)}
.winbtn.close:hover{background:rgba(255,80,64,0.82);color:#fff}

/* ---------------- main ---------------- */
.main{display:grid;grid-template-columns:186px 1fr;min-height:0}

.rail{
  display:flex;flex-direction:column;align-items:center;
  padding:24px 14px 16px;gap:16px;
  border-right:1px solid var(--line-soft);
  background:var(--bg-rail);user-select:none;
}
.reactor{width:126px;height:126px;flex:none;overflow:visible}
.status{
  font:11px/1 var(--mono);letter-spacing:.26em;text-transform:uppercase;
  color:var(--accent);text-shadow:0 0 12px rgba(var(--accent-rgb),0.45);
}
[data-state="idle"] .status{color:var(--text-dim);text-shadow:none}
.readouts{width:100%;margin-top:auto;display:flex;flex-direction:column;gap:5px}
.readout{display:flex;justify-content:space-between;
  font:10px/1.5 var(--mono);letter-spacing:.14em;color:var(--text-faint)}
.readout b{font-weight:400;color:var(--text-dim)}
.rail hr{width:100%;border:0;border-top:1px solid var(--line-soft);margin:2px 0}
.linkbtn{
  border:0;background:none;color:var(--text-faint);
  font:10px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;
  padding:6px 0;cursor:default;transition:color .15s;
}
.linkbtn:hover{color:var(--accent)}

/* ---------------- transcript ---------------- */
.stage{position:relative;min-height:0;display:flex}
.stage::before,.stage::after{
  content:"";position:absolute;width:11px;height:11px;pointer-events:none;
  border-color:rgba(var(--accent-rgb),0.4);border-style:solid;
}
.stage::before{top:10px;left:10px;border-width:1px 0 0 1px}
.stage::after{bottom:10px;right:10px;border-width:0 1px 1px 0}
#transcript{
  flex:1;overflow-y:auto;overflow-x:hidden;padding:26px 30px 18px;
  display:flex;flex-direction:column;gap:14px;scroll-behavior:smooth;
}
#transcript::-webkit-scrollbar{width:8px}
#transcript::-webkit-scrollbar-thumb{
  background:rgba(var(--accent-rgb),0.18);border-radius:4px}
#transcript::-webkit-scrollbar-thumb:hover{background:rgba(var(--accent-rgb),0.32)}

.msg{max-width:82%;animation:rise .22s ease-out both}
.label{
  font:9.5px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;
  color:var(--text-faint);margin-bottom:6px;user-select:none;
}
.body{white-space:pre-wrap;word-break:break-word}

.msg.user{align-self:flex-end;text-align:right}
.msg.user .body{
  display:inline-block;text-align:left;padding:9px 14px;
  background:rgba(255,255,255,0.035);
  border-right:1px solid rgba(var(--accent-rgb),0.4);
  color:#dbe5ea;
}
.msg.jarvis .body{
  padding:2px 0 2px 14px;
  border-left:1px solid rgba(var(--accent-rgb),0.5);
  box-shadow:-9px 0 22px -18px rgba(var(--accent-rgb),0.9);
}
.msg.error{max-width:100%}
.msg.error .body{
  padding:2px 0 2px 14px;border-left:1px solid var(--danger);color:#ffb3aa;
  font:12.5px/1.6 var(--mono);
}

/* Tool activity: one dim line, no JSON dumped at the user. */
.msg.tool{
  max-width:100%;align-self:stretch;display:flex;align-items:baseline;gap:9px;
  font:11px/1.7 var(--mono);letter-spacing:.05em;color:var(--text-faint);
}
.msg.tool .tk{color:rgba(var(--accent-rgb),0.55)}
.msg.tool .tn{color:rgba(var(--accent-rgb),0.78)}
.msg.tool .tc{
  padding:1px 5px;border:1px solid var(--line);border-radius:2px;
  font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--text-faint);
}
.msg.tool .ta{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.msg.tool .ts{flex:none;opacity:.85}
.msg.tool .ts.err{color:var(--danger)}

.msg.note{max-width:100%;font:11px/1.7 var(--mono);letter-spacing:.05em;
  color:var(--text-faint)}

/* Confirmation card: the one thing in the transcript that stops and waits.
   Loud by the standards of this interface, because ignoring it is the failure
   mode — an unanswered card refuses the action when it times out. */
.msg.confirm{
  max-width:100%;align-self:stretch;padding:11px 13px;
  border:1px solid rgba(255,112,95,0.42);border-left-width:2px;border-radius:3px;
  background:rgba(255,112,95,0.05);
}
.msg.confirm .ch{
  font:9px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;
  color:var(--danger);margin-bottom:8px;
}
.msg.confirm .cd{font:13px/1.55 var(--sans);color:var(--text)}
.msg.confirm .cx{
  font:11px/1.6 var(--mono);color:var(--text-faint);margin-top:5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.msg.confirm .cb{display:flex;gap:8px;margin-top:11px}
.msg.confirm button{
  font:10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  padding:7px 15px;border-radius:2px;cursor:pointer;color:var(--text-dim);
  border:1px solid var(--line-soft);background:transparent;
  transition:color .12s,border-color .12s,background .12s;
}
.msg.confirm button:hover{color:var(--text);border-color:rgba(255,255,255,0.2)}
.msg.confirm button.yes{color:var(--danger);border-color:rgba(255,112,95,0.45)}
.msg.confirm button.yes:hover{
  background:rgba(255,112,95,0.12);border-color:var(--danger)}
.msg.confirm.done{opacity:.5;border-color:var(--line-soft);background:transparent}
.msg.confirm.done .cb{display:none}
.msg.confirm .cr{
  display:none;margin-top:9px;font:10px/1 var(--mono);letter-spacing:.14em;
  text-transform:uppercase;color:var(--text-faint);
}
.msg.confirm.done .cr{display:block}

.msg.pending .body{color:var(--text-faint)}
.dots i{
  display:inline-block;width:4px;height:4px;margin-right:4px;border-radius:50%;
  background:var(--accent);opacity:.25;animation:blink 1.25s ease-in-out infinite}
.dots i:nth-child(2){animation-delay:.18s}
.dots i:nth-child(3){animation-delay:.36s}

/* ---------------- composer ---------------- */
.composer{border-top:1px solid var(--line-soft);background:rgba(255,255,255,0.014)}
#partial{
  display:none;padding:8px 22px 0;font:12px/1.5 var(--mono);
  color:var(--text-faint);letter-spacing:.04em;
}
#partial.on{display:block}
.row{display:flex;align-items:flex-end;gap:12px;padding:12px 18px 14px}
.mic{
  flex:none;width:36px;height:36px;border-radius:50%;cursor:default;
  border:1px solid var(--line);background:rgba(var(--accent-rgb),0.05);
  color:rgba(var(--accent-rgb),0.75);display:grid;place-items:center;
  transition:border-color .18s,box-shadow .18s,color .18s,background .18s;
}
.mic:hover{border-color:rgba(var(--accent-rgb),0.5);color:var(--accent)}
.mic svg{width:15px;height:15px;fill:currentColor}
.mic.armed{
  border-color:var(--accent);color:var(--accent);
  background:rgba(var(--accent-rgb),0.14);
  box-shadow:0 0 0 0 rgba(var(--accent-rgb),0.42);
  animation:micPulse 1.2s ease-out infinite;
}
.field{
  flex:1;display:flex;align-items:flex-end;gap:10px;
  border-bottom:1px solid var(--line-soft);padding:0 2px 7px;
  transition:border-color .2s,box-shadow .2s;
}
.field:focus-within{
  border-color:rgba(var(--accent-rgb),0.55);
  box-shadow:0 8px 20px -20px rgba(var(--accent-rgb),1);
}
.caret{font:12px/1.5 var(--mono);color:rgba(var(--accent-rgb),0.6);
  user-select:none;padding-bottom:1px}
#input{
  flex:1;border:0;outline:0;resize:none;background:none;color:var(--text);
  font:14px/1.5 var(--sans);max-height:118px;overflow-y:auto;
}
#input::placeholder{color:var(--text-faint)}
.hint{font:9.5px/1 var(--mono);letter-spacing:.2em;color:var(--text-faint);
  user-select:none;padding-bottom:4px}

/* ---------------- arc reactor ---------------- */
.reactor .ring{fill:none;stroke:var(--accent)}
.reactor .ring-outer{stroke-opacity:.22;stroke-width:1}
.reactor .ring-mid{stroke-opacity:.4;stroke-width:1}
.reactor .dash{stroke-opacity:.5;stroke-width:1;
  transform-origin:60px 60px;animation:spin 46s linear infinite}
.reactor .spokes line{stroke:var(--accent);stroke-opacity:.35;stroke-width:1}
.reactor .spokes{transform-origin:60px 60px}
.reactor .core{filter:drop-shadow(0 0 7px rgba(var(--accent-rgb),.55));
  transform-origin:60px 60px}
.reactor .hot{fill:#f2feff;fill-opacity:.9;
  filter:drop-shadow(0 0 6px rgba(var(--accent-rgb),.9));
  transform-origin:60px 60px}
.reactor .wave{fill:none;stroke:var(--accent);stroke-width:1;
  transform-origin:60px 60px;opacity:0}

[data-state="idle"] .core,[data-state="idle"] .hot{animation:breathe 4.6s ease-in-out infinite}
[data-state="listening"] .core,[data-state="listening"] .hot{animation:breathe 1.05s ease-in-out infinite}
[data-state="listening"] .dash{animation:spin 7s linear infinite}
[data-state="listening"] .ring-mid{stroke-opacity:.75}
[data-state="thinking"] .spokes{animation:spin 3.4s linear infinite}
[data-state="thinking"] .dash{animation:spin 2.6s linear reverse infinite}
[data-state="thinking"] .core,[data-state="thinking"] .hot{animation:breathe 1.9s ease-in-out infinite}
[data-state="speaking"] .core,[data-state="speaking"] .hot{animation:breathe 1.5s ease-in-out infinite}
[data-state="speaking"] .ring-mid{stroke-opacity:.8}
[data-state="speaking"] .spokes line{stroke-opacity:.55}
[data-state="speaking"] .wave{animation:emit 1.9s ease-out infinite}
[data-state="speaking"] .wave:nth-child(2){animation-delay:.63s}
[data-state="speaking"] .wave:nth-child(3){animation-delay:1.26s}

@keyframes spin{to{transform:rotate(360deg)}}
@keyframes breathe{0%,100%{opacity:.55;transform:scale(.97)}50%{opacity:1;transform:scale(1.03)}}
@keyframes emit{0%{opacity:.65;transform:scale(.62)}100%{opacity:0;transform:scale(1.62)}}
@keyframes markPulse{0%,100%{opacity:.5}50%{opacity:1}}
@keyframes micPulse{0%{box-shadow:0 0 0 0 rgba(var(--accent-rgb),.4)}
  100%{box-shadow:0 0 0 11px rgba(var(--accent-rgb),0)}}
@keyframes blink{0%,100%{opacity:.2}50%{opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

/* ---------------- voice mode ---------------- */
/* A full-screen takeover: the chat is hidden and it is just you and JARVIS.
   --lvl (0..1, set from live mic level) drives the ring, the bar and the glow,
   so the interface visibly breathes with your voice — and sits dead still when
   the microphone is delivering nothing, which is the point. */
.voice{
  position:fixed;inset:0;z-index:50;
  display:none;flex-direction:column;align-items:center;justify-content:center;
  gap:30px;padding:7vh 6vw;
  background:
    radial-gradient(circle at 50% 44%,rgba(var(--accent-rgb),0.09),transparent 58%),
    var(--bg);
  animation:voiceIn .32s ease-out;
}
[data-voice="on"] .voice{display:flex}
[data-voice="on"] .main,[data-voice="on"] .composer{visibility:hidden}
[data-voice="on"] .titlebar{z-index:60}

.voice-core{position:relative;width:min(44vh,320px);height:min(44vh,320px)}
.voice-core .reactor{width:100%;height:100%}
.voice-ring{
  position:absolute;inset:0;border-radius:50%;pointer-events:none;
  border:2px solid rgba(var(--accent-rgb),calc(0.14 + var(--lvl,0)*0.72));
  box-shadow:0 0 calc(24px + var(--lvl,0)*70px) rgba(var(--accent-rgb),calc(var(--lvl,0)*0.65));
  transform:scale(calc(1 + var(--lvl,0)*0.07));
  transition:transform .09s linear,box-shadow .12s linear,border-color .12s linear;
}
.voice-status{
  font:600 13px/1 var(--mono);letter-spacing:.42em;text-transform:uppercase;
  color:var(--accent);text-shadow:0 0 16px rgba(var(--accent-rgb),.5);min-height:15px;
}
[data-voice="on"][data-state="idle"] .voice-status{color:var(--text-dim);text-shadow:none}
.voice-caption{max-width:780px;text-align:center;min-height:3.4em;
  display:flex;flex-direction:column;gap:13px}
.voice-said{font:13px/1.5 var(--mono);letter-spacing:.05em;color:var(--text-dim)}
.voice-said:empty{display:none}
.voice-reply{font:300 25px/1.42 var(--sans);color:var(--text);
  text-shadow:0 0 30px rgba(var(--accent-rgb),.18)}
.voice-reply:empty{display:none}
.voice-meter{width:min(58vw,320px);display:flex;flex-direction:column;
  gap:9px;align-items:center}
.voice-bar{width:100%;height:4px;border-radius:2px;
  background:rgba(255,255,255,0.06);overflow:hidden}
.voice-bar i{display:block;height:100%;width:calc(var(--lvl,0)*100%);
  background:linear-gradient(90deg,rgba(var(--accent-rgb),.55),var(--accent));
  transition:width .08s linear}
.voice-warn{font:10px/1.5 var(--mono);letter-spacing:.13em;text-transform:uppercase;
  color:var(--danger);opacity:0;transition:opacity .3s;text-align:center}
.voice-warn.on{opacity:.92}
.voice-exit{
  position:absolute;top:50px;right:26px;
  border:1px solid var(--line);background:none;color:var(--text-dim);
  font:10px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;
  padding:9px 13px;border-radius:3px;cursor:default;transition:border-color .15s,color .15s;
}
.voice-exit:hover{border-color:var(--accent);color:var(--accent)}

@keyframes voiceIn{from{opacity:0;transform:scale(.99)}to{opacity:1;transform:none}}

/* ---------------- Claude theme (warm grey + terracotta) ---------------- */
/* JS also sets --accent / --accent-rgb live; these shift the surrounding
   palette from JARVIS's cool blue-black to Claude's warm grey so the whole
   window changes character, not just the accent. */
[data-assistant="claude"]{
  --bg:#1a1815;
  --bg-rail:#171512;
  --text:#e8e2d9;
  --text-dim:#9a9186;
  --text-faint:#6b6459;
  --line-soft:rgba(255,255,255,0.06);
}
[data-assistant="claude"] body::before{
  background:
    radial-gradient(760px 520px at 14% 86%,rgba(var(--accent-rgb),0.07),transparent 62%),
    radial-gradient(620px 460px at 92% 4%,rgba(var(--accent-rgb),0.035),transparent 60%),
    linear-gradient(rgba(255,255,255,0.016) 1px,transparent 1px) 0 0/48px 48px,
    linear-gradient(90deg,rgba(255,255,255,0.016) 1px,transparent 1px) 0 0/48px 48px;
}

@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
</head>
<body>

<header class="titlebar">
  <div class="brand pywebview-drag-region">
    <span class="mark"></span>
    <span class="wordmark">J.A.R.V.I.S.</span>
    <span class="sub" id="subtitle">local</span>
  </div>
  <div class="aiswitch" id="aiswitch">
    <button class="seg active" data-ai="jarvis" title="Local JARVIS">JARVIS</button>
    <button class="seg" data-ai="claude" title="Claude (cloud)">Claude</button>
  </div>
  <div class="winbtns">
    <button class="winbtn" id="btn-min" title="Minimise">&#8212;</button>
    <button class="winbtn close" id="btn-close" title="Close">&#10005;</button>
  </div>
</header>

<div class="main">
  <aside class="rail">
    <svg class="reactor" viewBox="0 0 120 120" aria-hidden="true">
      <defs>
        <radialGradient id="coreGrad">
          <stop offset="0%"   stop-color="#eafdff" stop-opacity=".95"/>
          <stop offset="42%"  stop-color="var(--accent)" stop-opacity=".7"/>
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <g class="waves">
        <circle class="wave" cx="60" cy="60" r="34"/>
        <circle class="wave" cx="60" cy="60" r="34"/>
        <circle class="wave" cx="60" cy="60" r="34"/>
      </g>
      <circle class="ring ring-outer" cx="60" cy="60" r="54"/>
      <circle class="ring dash" cx="60" cy="60" r="47" stroke-dasharray="3 9"/>
      <g class="spokes">
        <line x1="91"   y1="60"   x2="103"  y2="60"/>
        <line x1="81.9" y1="81.9" x2="90.4" y2="90.4"/>
        <line x1="60"   y1="91"   x2="60"   y2="103"/>
        <line x1="38.1" y1="81.9" x2="29.6" y2="90.4"/>
        <line x1="29"   y1="60"   x2="17"   y2="60"/>
        <line x1="38.1" y1="38.1" x2="29.6" y2="29.6"/>
        <line x1="60"   y1="29"   x2="60"   y2="17"/>
        <line x1="81.9" y1="38.1" x2="90.4" y2="29.6"/>
      </g>
      <circle class="ring ring-mid" cx="60" cy="60" r="28"/>
      <circle class="core" cx="60" cy="60" r="16" fill="url(#coreGrad)"/>
      <circle class="hot"  cx="60" cy="60" r="6"/>
    </svg>

    <div class="status" id="status">standby</div>

    <div class="readouts">
      <hr>
      <div class="readout"><span>TURNS</span><b id="ro-turns">000</b></div>
      <div class="readout"><span>TOOLS</span><b id="ro-tools">000</b></div>
      <div class="readout"><span>LINK</span><b id="ro-link">…</b></div>
      <hr>
      <button class="linkbtn" id="btn-reset">New session</button>
    </div>
  </aside>

  <section class="stage">
    <div id="transcript" role="log" aria-live="polite"></div>
  </section>
</div>

<div class="voice" id="voice">
  <button class="voice-exit" id="voice-exit">Esc&nbsp;&nbsp;exit</button>
  <div class="voice-core">
    <div class="voice-ring" id="voice-ring"></div>
    <svg class="reactor" viewBox="0 0 120 120" aria-hidden="true">
      <defs>
        <radialGradient id="coreGradV">
          <stop offset="0%"   stop-color="#eafdff" stop-opacity=".95"/>
          <stop offset="42%"  stop-color="var(--accent)" stop-opacity=".7"/>
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <g class="waves">
        <circle class="wave" cx="60" cy="60" r="34"/>
        <circle class="wave" cx="60" cy="60" r="34"/>
        <circle class="wave" cx="60" cy="60" r="34"/>
      </g>
      <circle class="ring ring-outer" cx="60" cy="60" r="54"/>
      <circle class="ring dash" cx="60" cy="60" r="47" stroke-dasharray="3 9"/>
      <g class="spokes">
        <line x1="91"   y1="60"   x2="103"  y2="60"/>
        <line x1="81.9" y1="81.9" x2="90.4" y2="90.4"/>
        <line x1="60"   y1="91"   x2="60"   y2="103"/>
        <line x1="38.1" y1="81.9" x2="29.6" y2="90.4"/>
        <line x1="29"   y1="60"   x2="17"   y2="60"/>
        <line x1="38.1" y1="38.1" x2="29.6" y2="29.6"/>
        <line x1="60"   y1="29"   x2="60"   y2="17"/>
        <line x1="81.9" y1="38.1" x2="90.4" y2="29.6"/>
      </g>
      <circle class="ring ring-mid" cx="60" cy="60" r="28"/>
      <circle class="core" cx="60" cy="60" r="16" fill="url(#coreGradV)"/>
      <circle class="hot"  cx="60" cy="60" r="6"/>
    </svg>
  </div>
  <div class="voice-status" id="voice-status">standby</div>
  <div class="voice-caption">
    <div class="voice-said" id="voice-said"></div>
    <div class="voice-reply" id="voice-reply"></div>
  </div>
  <div class="voice-meter">
    <div class="voice-bar"><i id="voice-fill"></i></div>
    <div class="voice-warn" id="voice-warn">No microphone signal — check the mic is on and unmuted</div>
  </div>
</div>

<div class="composer">
  <div id="partial"></div>
  <div class="row">
    <button class="mic" id="mic" title="Voice mode (Ctrl+Space)">
      <svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3zm5-3a1 1 0 0 1 2 0 7 7 0 0 1-6 6.93V21a1 1 0 0 1-2 0v-3.07A7 7 0 0 1 5 11a1 1 0 1 1 2 0 5 5 0 0 0 10 0z"/></svg>
    </button>
    <div class="field">
      <span class="caret">&gt;</span>
      <textarea id="input" rows="1" spellcheck="false" autocomplete="off"
                placeholder="Type a command…"></textarea>
      <span class="hint">ENTER</span>
    </div>
  </div>
</div>

<script>
(function(){
"use strict";

var root       = document.documentElement;
var transcript = document.getElementById("transcript");
var input      = document.getElementById("input");
var micBtn     = document.getElementById("mic");
var statusEl   = document.getElementById("status");
var partialEl  = document.getElementById("partial");
var subtitleEl = document.getElementById("subtitle");

var voiceEl     = document.getElementById("voice");
var voiceStatus = document.getElementById("voice-status");
var voiceSaid   = document.getElementById("voice-said");
var voiceReply  = document.getElementById("voice-reply");
var voiceWarn   = document.getElementById("voice-warn");
var voiceRing   = document.getElementById("voice-ring");
var voiceFill   = document.getElementById("voice-fill");
var wordmarkEl  = document.querySelector(".wordmark");
var ACCENT_RGB  = "__ACCENT_RGB__";

/* ---------- assistant / theme ---------- */

function hexToRgb(hex){
  hex = String(hex || "").replace("#","");
  if (hex.length === 3) hex = hex.split("").map(function(c){return c+c;}).join("");
  if (hex.length !== 6) return ACCENT_RGB;
  var n = parseInt(hex, 16);
  return ((n>>16)&255) + ", " + ((n>>8)&255) + ", " + (n&255);
}

function applyAssistant(active, name, accent){
  if (active) root.dataset.assistant = active;
  if (accent) {
    root.style.setProperty("--accent", accent);
    var rgb = hexToRgb(accent);
    root.style.setProperty("--accent-rgb", rgb);
    ACCENT_RGB = rgb;  // keep the meter/ring colours in sync
  }
  if (name && wordmarkEl) wordmarkEl.textContent = name;
  // Reflect the active segment in the switcher.
  var segs = document.querySelectorAll("#aiswitch .seg");
  for (var i = 0; i < segs.length; i++) {
    segs[i].classList.toggle("active", segs[i].dataset.ai === active);
  }
}

var LABEL = {idle:"standby", listening:"listening", thinking:"processing",
             speaking:"responding"};
var VLABEL = {idle:"standby", listening:"listening",
              thinking:"thinking", speaking:"speaking"};

var voiceOn   = false;
var lvlPeak   = 0;      // running peak for dead-mic detection
var lvlPeakAt = 0;

var state    = "idle";
var sticky   = true;      // auto-scroll unless the user has scrolled up
var pending  = null;      // the "…" placeholder awaiting a reply
var turns    = 0;
var toolRuns = 0;
var booted   = false;
var linked   = false;

/* ---------- bridge ---------- */

function api(){
  return (window.pywebview && window.pywebview.api) || null;
}

// Every call resolves; a missing bridge degrades to a local echo so the page
// still renders (and can be eyeballed) outside of pywebview.
function call(name){
  var args = Array.prototype.slice.call(arguments, 1);
  var a = api();
  if (a && typeof a[name] === "function") {
    try { return Promise.resolve(a[name].apply(a, args)); }
    catch (e) { return Promise.resolve({ok:false, error:String(e)}); }
  }
  return Promise.resolve(offline(name, args));
}

function offline(name, args){
  if (name === "ready") return {greeting:null, offline:true};
  if (name === "send_message") {
    setTimeout(function(){
      dispatch({type:"reply", text:"Bridge unavailable — nothing was sent, Sir."});
    }, 260);
    return {ok:true};
  }
  return {ok:false, error:"bridge unavailable"};
}

/* ---------- rendering ---------- */

function pad3(n){ return ("00" + n).slice(-3); }

function readouts(){
  document.getElementById("ro-turns").textContent = pad3(turns);
  document.getElementById("ro-tools").textContent = pad3(toolRuns);
  document.getElementById("ro-link").textContent  = linked ? "ok" : "—";
}

function scrollDown(){
  transcript.scrollTop = transcript.scrollHeight;
}

transcript.addEventListener("scroll", function(){
  var slack = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
  sticky = slack < 60;
});

function place(node){
  transcript.appendChild(node);
  if (sticky) scrollDown();
  return node;
}

function message(role, text, label){
  var wrap = document.createElement("div");
  wrap.className = "msg " + role;
  if (label) {
    var l = document.createElement("div");
    l.className = "label";
    l.textContent = label;
    wrap.appendChild(l);
  }
  var b = document.createElement("div");
  b.className = "body";
  b.textContent = text;
  wrap.appendChild(b);
  return place(wrap);
}

function say(text){    return message("jarvis", text, "J.A.R.V.I.S."); }
function heard(text){  return message("user", text, "You"); }
function fault(text){  return message("error", text, "Fault"); }
function note(text){
  var n = document.createElement("div");
  n.className = "msg note";
  n.textContent = text;
  return place(n);
}

function preview(args){
  if (!args) return "";
  var keys = Object.keys(args);
  if (!keys.length) return "";
  var v = args[keys[0]];
  if (v === null || v === undefined) v = "";
  if (typeof v === "object") { try { v = JSON.stringify(v); } catch (e) { v = "…"; } }
  v = String(v).replace(/\s+/g, " ");
  if (v.length > 52) v = v.slice(0, 52) + "…";
  return v ? keys[0] + "=" + v : "";
}

/* Approve/Deny card. The buttons are the ONLY way an action gets approved —
   there is no tool the model can call to do it for you. An unanswered card is
   a refusal, so it is drawn to be noticed. */
var confirmCards = {};

function confirmCard(ev){
  var wrap = document.createElement("div");
  wrap.className = "msg confirm";
  wrap.dataset.token = ev.token || "";

  function part(cls, text){
    var d = document.createElement("div");
    d.className = cls; d.textContent = text; return d;
  }
  wrap.appendChild(part("ch", "Confirmation required"));
  wrap.appendChild(part("cd", "About to " + (ev.description || ev.tool || "act") + "."));
  if (ev.detail) wrap.appendChild(part("cx", ev.detail));

  var bar = document.createElement("div");
  bar.className = "cb";
  var yes = document.createElement("button");
  yes.className = "yes"; yes.textContent = "Approve";
  var no = document.createElement("button");
  no.textContent = "Deny";
  bar.appendChild(yes); bar.appendChild(no);
  wrap.appendChild(bar);
  wrap.appendChild(part("cr", ""));

  function answer(granted){
    if (wrap.classList.contains("done")) return;
    settleCard(wrap, granted ? "Approved" : "Denied");
    call("resolve_confirmation", ev.token, granted);
  }
  yes.addEventListener("click", function(){ answer(true); });
  no.addEventListener("click", function(){ answer(false); });

  if (ev.token) confirmCards[ev.token] = wrap;
  place(wrap);
  yes.focus();
  return wrap;
}

function settleCard(wrap, verdict){
  wrap.classList.add("done");
  var r = wrap.querySelector(".cr");
  if (r) r.textContent = verdict;
}

// Python's own resolution — a timeout, or the window closing under us.
function confirmDone(ev){
  var wrap = confirmCards[ev.token];
  if (!wrap) return;
  delete confirmCards[ev.token];
  if (!wrap.classList.contains("done")) {
    settleCard(wrap, ev.timed_out ? "Timed out — not run"
                                  : (ev.granted ? "Approved" : "Denied"));
  }
}

function toolLine(ev){
  toolRuns += 1; readouts();
  var row = document.createElement("div");
  row.className = "msg tool";
  row.dataset.tool = ev.tool || "";
  row.dataset.open = "1";

  function span(cls, text){
    var s = document.createElement("span");
    s.className = cls; s.textContent = text; return s;
  }
  row.appendChild(span("tk", "▸"));
  row.appendChild(span("tn", ev.tool || "tool"));
  if (ev.category) row.appendChild(span("tc", ev.category));
  row.appendChild(span("ta", preview(ev.args)));
  row.appendChild(span("ts", ""));
  return place(row);
}

function toolResult(ev){
  var rows = transcript.querySelectorAll('.msg.tool[data-open="1"]');
  var row = null;
  for (var i = rows.length - 1; i >= 0; i--) {
    if (!ev.tool || rows[i].dataset.tool === ev.tool) { row = rows[i]; break; }
  }
  if (!row) return;
  row.dataset.open = "0";
  var mark = row.querySelector(".ts");
  var res  = ev.result;
  var err  = res && typeof res === "object" ? (res.error || null) : null;
  if (err) {
    mark.className = "ts err";
    mark.textContent = "! " + String(err).slice(0, 70);
  } else {
    mark.textContent = "✓";
  }
}

function showPending(){
  if (pending) return;
  var wrap = document.createElement("div");
  wrap.className = "msg jarvis pending";
  var l = document.createElement("div");
  l.className = "label"; l.textContent = "J.A.R.V.I.S.";
  var b = document.createElement("div");
  b.className = "body dots";
  b.innerHTML = "<i></i><i></i><i></i>";
  wrap.appendChild(l); wrap.appendChild(b);
  pending = place(wrap);
}

function clearPending(){
  if (pending && pending.parentNode) pending.parentNode.removeChild(pending);
  pending = null;
}

function setState(next){
  if (!LABEL[next]) return;
  state = next;
  root.dataset.state = next;
  statusEl.textContent = LABEL[next];
  voiceStatus.textContent = VLABEL[next];
  micBtn.classList.toggle("armed", next === "listening");
  if (next !== "listening") setPartial("");
  // Leaving the listening state clears any stale dead-mic warning.
  if (next !== "listening") voiceWarn.classList.remove("on");
}

/* ---------- voice mode ---------- */

function setLevel(v){
  v = Math.max(0, Math.min(1, v || 0));
  // Set the reactive styles directly rather than via calc(var(--lvl)) — some
  // WebView2 builds do not recompute calc() that references a custom property
  // updated from script, leaving the meter frozen.
  voiceFill.style.width = (v * 100).toFixed(1) + "%";
  voiceRing.style.transform = "scale(" + (1 + v * 0.07).toFixed(3) + ")";
  voiceRing.style.borderColor = "rgba(" + ACCENT_RGB + "," + (0.14 + v * 0.72).toFixed(3) + ")";
  voiceRing.style.boxShadow = "0 0 " + (24 + v * 70).toFixed(0) +
    "px rgba(" + ACCENT_RGB + "," + (v * 0.65).toFixed(3) + ")";
  if (!voiceOn) return;

  var now = Date.now();
  if (v > lvlPeak) { lvlPeak = v; lvlPeakAt = now; }

  // Dead-mic tell: while genuinely listening, if nothing above the noise
  // floor has arrived for a few seconds, say so plainly. This is the whole
  // reason the meter exists — a silent mic should never be a mystery.
  if (state === "listening") {
    if (now - lvlPeakAt > 3500 && lvlPeak < 0.05) {
      voiceWarn.classList.add("on");
    } else if (v > 0.07) {
      voiceWarn.classList.remove("on");
    }
    if (now - lvlPeakAt > 3500) lvlPeak = 0;  // let the window slide
  }
}

function enterVoice(){
  if (voiceOn) return;
  voiceOn = true;
  root.dataset.voice = "on";
  voiceSaid.textContent = "";
  voiceReply.textContent = "";
  voiceWarn.classList.remove("on");
  lvlPeak = 0; lvlPeakAt = Date.now();
  call("enter_voice_mode");
}

function exitVoice(){
  if (!voiceOn) return;
  voiceOn = false;
  root.dataset.voice = "off";
  root.style.setProperty("--lvl", 0);
  voiceWarn.classList.remove("on");
  call("exit_voice_mode");
  input.focus();
}

function setPartial(text){
  if (text) { partialEl.textContent = text; partialEl.classList.add("on"); }
  else { partialEl.textContent = ""; partialEl.classList.remove("on"); }
  if (sticky) scrollDown();
}

/* ---------- inbound events (Python -> page) ---------- */

function dispatch(ev){
  if (!ev || !ev.type) return;
  switch (ev.type) {
    case "state":       setState(ev.state); break;
    case "user":        clearPending(); heard(ev.text || ""); turns += 1; readouts(); break;
    case "partial":     setPartial(ev.text || ""); break;
    case "reply":       clearPending(); say(ev.text || ""); break;
    case "tool_call":   toolLine(ev); break;
    case "tool_result": toolResult(ev); break;
    case "error":       clearPending(); fault(ev.message || ev.error || "Unknown fault."); break;
    case "note":        note(ev.text || ""); break;
    case "level":       setLevel(ev.value); break;
    case "voice_user":  voiceSaid.textContent = ev.text || "";
                        voiceReply.textContent = "";
                        heard(ev.text || ""); turns += 1; readouts(); break;
    case "voice_reply": voiceReply.textContent = ev.text || "";
                        say(ev.text || ""); break;
    case "voice_closed": exitVoice(); break;
    case "assistant":   applyAssistant(ev.active, ev.name, ev.accent);
                        if (ev.label) subtitleEl.textContent = ev.label;
                        note((ev.name || "Assistant") + " is now active"); break;
    case "link":        linked = !!ev.ok; readouts();
                        if (ev.label) subtitleEl.textContent = ev.label; break;
    case "confirm":     confirmCard(ev); break;
    case "confirm_done": confirmDone(ev); break;
    case "clear":       transcript.innerHTML = ""; clearPending();
                        confirmCards = {};
                        turns = 0; toolRuns = 0; readouts(); break;
    default: break;
  }
}

// The single entry point window.evaluate_js() targets from Python.
window.__jarvis_dispatch = function(ev){
  try { dispatch(ev); } catch (e) { console.error("dispatch", e, ev); }
};

/* ---------- outbound (page -> Python) ---------- */

function send(){
  var text = input.value.trim();
  if (!text) return;
  input.value = ""; autosize();
  heard(text); turns += 1; readouts();
  setState("thinking"); showPending();
  call("send_message", text).then(function(r){
    if (r && r.ok === false) {
      clearPending();
      fault(r.error || "The message could not be delivered.");
      setState("idle");
    }
  });
}

function autosize(){
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 118) + "px";
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", function(e){
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  else if (e.key === "Escape") { input.value = ""; autosize(); }
});

// The mic button is the way into hands-free voice mode.
micBtn.addEventListener("click", function(){
  if (voiceOn) exitVoice(); else enterVoice();
});
document.getElementById("voice-exit").addEventListener("click", exitVoice);

// Assistant switcher: clicking a segment flips the active brain + theme.
document.getElementById("aiswitch").addEventListener("click", function(e){
  var seg = e.target.closest(".seg");
  if (!seg || seg.classList.contains("active")) return;
  var ai = seg.dataset.ai;
  applyAssistant(ai, ai === "claude" ? "Claude" : "J.A.R.V.I.S.",
                 ai === "claude" ? "#d97757" : "#4dd0e1");  // optimistic
  call("switch_assistant", ai);
});

document.getElementById("btn-min").addEventListener("click", function(){ call("minimise"); });
document.getElementById("btn-close").addEventListener("click", function(){ call("close"); });
document.getElementById("btn-reset").addEventListener("click", function(){
  call("reset_conversation");
});

document.addEventListener("keydown", function(e){
  if (e.ctrlKey && e.code === "Space") {
    e.preventDefault();
    if (voiceOn) exitVoice(); else enterVoice();
  } else if (e.key === "Escape" && voiceOn) {
    e.preventDefault(); exitVoice();
  }
});

// Clicking anywhere idle puts the caret back in the composer.
document.addEventListener("mouseup", function(e){
  if (e.target.closest("button, textarea, .msg")) return;
  if (window.getSelection && String(window.getSelection())) return;
  input.focus();
});

/* ---------- boot ---------- */

function boot(){
  if (booted) return;
  booted = true;
  readouts();
  call("ready").then(function(info){
    info = info || {};
    if (info.assistant) applyAssistant(info.assistant, info.assistant_name, info.accent);
    if (info.subtitle) subtitleEl.textContent = info.subtitle;
    if (info.state) setState(info.state);
    if (info.greeting) say(info.greeting);
    if (info.offline) note("bridge unavailable — preview mode");
    input.focus();
  });
}

if (window.pywebview && window.pywebview.api) boot();
window.addEventListener("pywebviewready", boot);
setTimeout(boot, 2600);   // last resort: render something regardless

})();
</script>
</body>
</html>
"""
