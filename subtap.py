#!/usr/bin/env python
"""Subtap -- a tiny, dependency-free local web editor for a song's .srt caption timings.

Launches a browser app for one song: a waveform you can click/drag to scrub, audio
playback with a moving playhead and the active line highlighted, and tools to retime
each caption -- nudge start/end, snap them to the playhead, tap-sync a section by ear
(hold a key per line, like DistroKid but for just part of a song), drag the edges on the
waveform, split a line, and edit the text. Per-line deltas show exactly what moved and a
one-click revert undoes any line; overlaps are flagged in red. Save writes straight back
to the song's .srt (keeping numbered <name>.srt.bak.NNN backups). Pure Python standard
library -- no pip install.

Songs resolve by a path or a bare folder name found in the current directory or one level
deep (e.g. an "Artist/Song" layout).

Usage:
    python subtap.py "Hello, World"
    python subtap.py "Artist/Song" --port 8756
    python subtap.py "Rizzonomics" --no-browser
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Song / file discovery (mirrors make_captions.py / make_video.py)
# ---------------------------------------------------------------------------

def find_song_dir(name: str) -> Path:
    p = Path(name)
    project = Path(__file__).resolve().parent
    for c in (p, Path.cwd() / p, project / p):
        if c.is_dir():
            return c.resolve()
    leaf = p.name
    for root in (Path.cwd(), project):
        nested = sorted(m for m in root.glob(f"*/{leaf}") if m.is_dir())
        if len(nested) == 1:
            return nested[0].resolve()
        if len(nested) > 1:
            arts = ", ".join(m.parent.name for m in nested)
            sys.exit(f"error: {leaf!r} exists under multiple artists ({arts}); "
                     f"pass 'Artist/Song' to disambiguate")
    sys.exit(f"error: could not find a song folder named {name!r}")


def find_one(directory: Path, suffix: str) -> Path:
    matches = sorted(directory.glob(f"*{suffix}"))
    if not matches:
        sys.exit(f"error: no {suffix} file found in {directory}")
    if len(matches) > 1:
        # prefer the one named like the folder
        preferred = directory / f"{directory.name}{suffix}"
        if preferred in matches:
            return preferred
        print(f"warning: multiple {suffix} files in {directory}; using {matches[0].name}")
    return matches[0]


# ---------------------------------------------------------------------------
# SRT parse / write
# ---------------------------------------------------------------------------

_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def ts_to_seconds(t: str) -> float:
    m = _TS.search(t)
    if not m:
        return 0.0
    h, mn, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + s + ms / 1000.0


def seconds_to_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    mn, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{mn:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # optional index line
        idx = 0
        if lines[0].strip().isdigit() and "-->" in (lines[1] if len(lines) > 1 else ""):
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        left, right = lines[idx].split("-->")
        start = ts_to_seconds(left)
        end = ts_to_seconds(right)
        body = "\n".join(lines[idx + 1:]).strip()
        cues.append({"start": start, "end": end, "text": body})
    return cues


def write_srt(cues) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{seconds_to_ts(c['start'])} --> {seconds_to_ts(c['end'])}")
        out.append(c["text"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


MAX_BACKUPS = 30   # keep this many numbered backups per .srt; older ones are pruned


def _make_backup(srt: Path):
    """Copy srt to the next numbered backup (srt.bak.001, .002, ...) before it's overwritten.

    Numbered so a save can never clobber the only safety net. Pruned to the newest
    MAX_BACKUPS. Returns the backup Path, or None if there was nothing to back up.
    """
    if not srt.exists():
        return None
    existing = sorted(
        (p for p in srt.parent.glob(srt.name + ".bak.*") if p.suffix[1:].isdigit()),
        key=lambda p: int(p.suffix[1:]),
    )
    nxt = (int(existing[-1].suffix[1:]) + 1) if existing else 1
    bak = srt.with_name(f"{srt.name}.bak.{nxt:03d}")
    shutil.copy2(srt, bak)
    keep = MAX_BACKUPS - 1   # existing ones to keep alongside the new one
    if keep > 0 and len(existing) > keep:
        for old in existing[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return bak


def _clean_cues(raw):
    """Coerce the posted cue list into well-formed {start,end,text}, dropping junk.

    Defends the .srt on disk from a malformed payload: non-numeric times are skipped,
    starts are clamped to >= 0, and end is kept >= start (no negative-duration output).
    """
    cues = []
    for c in raw or []:
        try:
            start = max(0.0, float(c["start"]))
            end = max(float(c["end"]), start)
        except (KeyError, TypeError, ValueError):
            continue
        cues.append({"start": start, "end": end, "text": str(c.get("text", ""))})
    return cues


# ---------------------------------------------------------------------------
# The web app (static HTML/JS; all data comes from /api/data)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Subtap</title>
<style>
  :root { --bg:#12141a; --panel:#1b1e27; --panel2:#232734; --line:#2c3040;
          --txt:#e6e8ee; --dim:#9aa0b0; --accent:#4fd1ff; --active:#ffd24f;
          --danger:#ff6b6b; --ok:#4fe08a; }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.4 system-ui,Segoe UI,Roboto,Arial,sans-serif;
         display:flex; flex-direction:column; height:100vh; overflow:hidden; }
  header { display:flex; align-items:center; gap:12px; padding:10px 16px; flex:0 0 auto;
           background:var(--panel); border-bottom:1px solid var(--line); z-index:5;}
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .sub { color:var(--dim); font-size:12px; }
  header .grow { flex:1; }
  button { background:var(--panel2); color:var(--txt); border:1px solid var(--line);
           border-radius:6px; padding:5px 9px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#06131a; border-color:var(--accent); font-weight:600; }
  button.warn { border-color:var(--danger); color:var(--danger); }
  #top { padding:12px 16px 6px; flex:0 0 auto; }
  #tablewrap { flex:1 1 auto; overflow-y:auto; padding:0 16px 10px; }
  #wavebox { position:relative; background:var(--panel); border:1px solid var(--line);
             border-radius:8px; overflow:hidden; }
  #wave { display:block; width:100%; height:150px; cursor:pointer; }
  #phead { position:absolute; top:0; left:0; width:100%; height:150px; pointer-events:none; }
  #transport { display:flex; align-items:center; gap:10px; margin:10px 0; }
  #time { font-variant-numeric:tabular-nums; color:var(--dim); }
  table { width:100%; border-collapse:collapse; margin-top:6px; }
  th,td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); vertical-align:middle; }
  th { color:var(--dim); font-weight:500; font-size:12px; position:sticky; top:0; background:var(--bg); z-index:2;}
  tr.cue.now td { background:rgba(255,255,255,.055); }          /* line currently on screen */
  tr.cue.now .txt { background:#2c3242; }
  tr.cue.active td { background:rgba(255,210,79,.10); }
  tr.cue.sel td { background:rgba(79,209,255,.13); }
  tr.cue:hover td { background:rgba(255,255,255,.03); }
  td.idx { color:var(--dim); width:34px; text-align:right; font-variant-numeric:tabular-nums;}
  input.t { width:78px; background:var(--panel2); color:var(--txt); border:1px solid var(--line);
            border-radius:5px; padding:3px 5px; font-variant-numeric:tabular-nums; }
  input.t.bad { background:rgba(255,107,107,.20); border-color:var(--danger); }
  input.txt { width:100%; background:var(--panel2); color:var(--txt); border:1px solid var(--line);
              border-radius:5px; padding:3px 6px; }
  td.dur { color:var(--dim); font-variant-numeric:tabular-nums; font-size:12px; }
  td.startcell, td.endcell, td.dur { white-space:nowrap; padding-right:16px; }  /* little gap between groups */
  .delta { display:inline-block; margin-left:5px; min-width:38px; font-size:11px;
           font-variant-numeric:tabular-nums; }
  .delta.up { color:var(--ok); }      /* moved later  (+) */
  .delta.down { color:var(--active); }/* moved earlier(−) */
  .rowbtn { padding:2px 6px; font-size:12px; }
  td.revcell { white-space:nowrap; }
  td.spacer { width:100%; }              /* eats the slack -> the big gap before seek + lyric */
  td.rowacts { width:34px; }
  td.textcell { width:520px; }           /* lyric box on the right, comfortable fixed width */
  .rowbtn.rev.live { border-color:var(--active); color:var(--active); }
  .rowbtn.rev:disabled { opacity:.25; cursor:default; border-color:var(--line); color:var(--dim); }
  .rowbtn.rev:disabled:hover { border-color:var(--line); }
  #dock { flex:0 0 auto; background:var(--panel);
          border-top:1px solid var(--line); padding:8px 16px; display:flex; flex-wrap:wrap;
          gap:6px; align-items:center; z-index:6; }
  #dock .grp { display:flex; gap:4px; align-items:center; padding:0 8px; border-right:1px solid var(--line);}
  #dock .grp:last-child { border-right:none; }
  #dock .lbl { color:var(--dim); font-size:12px; margin-right:2px; }
  #sel { color:var(--accent); font-weight:600; }
  #help { color:var(--dim); font-size:12px; margin-left:auto; }
  kbd { background:var(--panel2); border:1px solid var(--line); border-bottom-width:2px;
        border-radius:4px; padding:0 5px; font-size:11px; }
  #status { font-size:12px; color:var(--dim); }
  .warnflag { color:var(--danger); }
  #preview { margin-top:8px; min-height:64px; display:flex; align-items:center; justify-content:center;
             background:#0c0d12; border:1px solid var(--line); border-radius:8px; padding:8px 16px; }
  #pvtext { font-size:26px; font-weight:700; text-align:center; color:#fff;
            text-shadow:0 2px 4px rgba(0,0,0,.85); }
  #tapstarts.on,#tapboth.on { background:var(--active); color:#1a1400; border-color:var(--active); font-weight:600; }
  #offwrap { color:var(--dim); font-size:12px; display:inline-flex; align-items:center; gap:3px; }
  #tapoffset { width:78px; background:var(--panel2); color:var(--txt); border:1px solid var(--line);
               border-radius:5px; padding:4px 6px; font-variant-numeric:tabular-nums; }
  #tapbanner { display:none; align-items:center; gap:16px; margin-top:10px; padding:10px 14px;
               background:rgba(255,210,79,.08); border:1px solid var(--active); border-radius:8px; }
  #tapbanner .tapinfo { flex:1; }
  #tapbanner .taplabel { color:var(--dim); font-size:12px; margin-bottom:4px; }
  #tapbanner .tapnow { font-size:20px; font-weight:700; color:var(--active); min-height:24px; }
  #tapbanner .tapnext { color:var(--dim); font-size:12px; margin-top:2px; }
  #holdbtn { font-size:20px; font-weight:800; padding:16px 34px; background:var(--panel2);
             border:2px solid var(--active); color:var(--active); border-radius:10px; user-select:none;
             cursor:pointer; }
  #holdbtn.held { background:var(--active); color:#1a1400; transform:scale(0.97); }
</style></head>
<body>
<header>
  <h1 id="title">Subtap</h1>
  <span class="sub" id="srtname"></span>
  <span class="grow"></span>
  <span id="status"></span>
  <button id="revert">Revert</button>
  <button class="primary" id="save">Save .srt</button>
</header>

<div id="top">
  <div id="wavebox"><canvas id="wave"></canvas><canvas id="phead"></canvas></div>
  <div id="preview"><span id="pvtext"></span></div>
  <div id="transport">
    <button id="play">▶ Play</button>
    <button id="tapstarts" title="tap once at each line's START; the whole line moves there (duration kept), overlaps left flagged red">⇥ Tap: starts</button>
    <button id="tapboth" title="hold through each line: press=start, release=end (for real gaps)">⇥ Tap: start+stop</button>
    <span id="offwrap" title="latency comp: each tap is recorded this many ms earlier than the click">old man reaction time −<input id="tapoffset" type="number" value="100" step="10" min="0">ms</span>
    <span id="time">0:00.00 / 0:00.00</span>
    <span class="grow"></span>
    <span id="help">
      <kbd>Space</kbd> play &nbsp; <kbd>I</kbd>/<kbd>O</kbd> set start/end=playhead &nbsp;
      <kbd>,</kbd>/<kbd>.</kbd> nudge start &nbsp; <kbd>[</kbd>/<kbd>]</kbd> nudge end &nbsp;
      <kbd>P</kbd> play line &nbsp; <kbd>↑</kbd>/<kbd>↓</kbd> select &nbsp; <kbd>Ctrl+S</kbd> save
    </span>
  </div>

  <div id="tapbanner">
    <div class="tapinfo">
      <div class="taplabel" id="taplabel"></div>
      <div class="tapnow" id="tapnow"></div>
      <div class="tapnext">next: <span id="tapnext"></span></div>
    </div>
    <button id="holdbtn">HOLD</button>
    <button id="tapdone">Done</button>
  </div>
</div>

<div id="tablewrap">
  <table>
    <thead><tr><th class="idx">#</th><th>Start</th><th>End</th><th>Dur</th><th></th><th></th><th></th><th>Text</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>

<div id="dock">
  <div class="grp"><span class="lbl">Line</span><span id="sel">—</span></div>
  <div class="grp"><span class="lbl">Start</span>
    <button data-nudge="start" data-d="-0.25">−.25</button>
    <button data-nudge="start" data-d="-0.05">−.05</button>
    <button id="startPlay" title="set start = playhead (I)">⇤ playhead</button>
    <button data-nudge="start" data-d="0.05">+.05</button>
    <button data-nudge="start" data-d="0.25">+.25</button>
  </div>
  <div class="grp"><span class="lbl">End</span>
    <button data-nudge="end" data-d="-0.25">−.25</button>
    <button data-nudge="end" data-d="-0.05">−.05</button>
    <button id="endPlay" title="set end = playhead (O)">playhead ⇥</button>
    <button data-nudge="end" data-d="0.05">+.05</button>
    <button data-nudge="end" data-d="0.25">+.25</button>
  </div>
  <div class="grp">
    <button id="playcue" title="play this line (P)">▶ line</button>
    <button id="split" title="split at playhead">✂ split</button>
    <button id="add">+ add</button>
    <button class="warn" id="del">🗑 delete</button>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);

// State values kept in one place so a typo is a ReferenceError, not a silently-dead branch.
// These strings are load-bearing: DRAG.START/END double as cue keys (CUES[i][DRAG.START]),
// and MODE.* are used as keys into TAP_HELP -- so the values must stay exactly these.
const MODE = { STARTS:"starts", STARTSTOP:"startstop" };
const DRAG = { START:"start", END:"end", SCRUB:"scrub" };
const API  = { DATA:"/api/data", SAVE:"/api/save" };   // must match the Python routes below

// Canvas colors in ONE place: the solids mirror the CSS custom properties (single source of
// truth for the theme), the alpha/canvas-only shades are noted inline.
const _cssv = getComputedStyle(document.documentElement);
const COLOR = {
  bg:      _cssv.getPropertyValue("--bg").trim()     || "#12141a",
  accent:  _cssv.getPropertyValue("--accent").trim() || "#4fd1ff",
  active:  _cssv.getPropertyValue("--active").trim() || "#ffd24f",
  wave:    "#3a6b82",                 // waveform stroke (canvas only)
  tick:    "rgba(255,255,255,.10)",   // faint cue-start ticks
  selFill: "rgba(79,209,255,.16)",    // accent @ ~16% -- selected-region wash
};

let CUES = [], ORIG = [], sel = -1, dur = 0, peaks = null, playCueEnd = null;
let tap=false, tapPtr=-1, tapHolding=false, keyHold=false, mouseHold=false, lastPv="";
let tapMode=MODE.STARTSTOP, lastStarted=-1, tapOffset=0.1;
let audioCtx=null, avSync=0.10;   // A/V-sync auto-set from output latency on first play
let cssW=0, cssH=0, dprCur=1, waveCache=null;
const audio = new Audio();
audio.preload = "auto";

// The audible position: audio.currentTime runs slightly AHEAD of what reaches the
// speakers (output latency), so drive all visuals + taps off this compensated clock.
function nowT(){ return Math.max(0, audio.currentTime - avSync); }

function fmt(t){ if(t==null||isNaN(t))t=0; const m=Math.floor(t/60), s=t-60*m;
  return m+":"+(s<10?"0":"")+s.toFixed(2); }

// escape lyric text before it goes into an HTML attribute (handles & < > " -- order matters)
function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

async function load(){
  const d = await (await fetch(API.DATA)).json();
  $("#title").textContent = d.title;
  $("#srtname").textContent = d.srt_name;
  // o0/oe = this cue's ORIGINAL start/end, carried on the object itself so deltas
  // track a line's identity, not its row index (add/split/delete shift indices).
  CUES = d.cues.map(c => ({start:c.start, end:c.end, text:c.text, o0:c.start, oe:c.end}));
  ORIG = JSON.parse(JSON.stringify(CUES));
  audio.src = d.audio_url;
  // decode for the waveform (separate fetch of the same bytes, cached by browser)
  try {
    audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    const buf = await (await fetch(d.audio_url)).arrayBuffer();
    const ab = await audioCtx.decodeAudioData(buf);
    dur = ab.duration; durStr = fmt(dur);
    computePeaks(ab);
  } catch(e){ console.warn("waveform decode failed", e); }
  // on first playback, learn the real output latency and use it as the default A/V sync
  audio.addEventListener("playing", ()=>{
    if(audioCtx){ audioCtx.resume && audioCtx.resume();
      const ol = audioCtx.outputLatency || audioCtx.baseLatency || 0;
      if(ol>0.005){ avSync=ol; }
    }
  });
  audio.addEventListener("loadedmetadata", ()=>{ if(!dur){ dur = audio.duration; durStr = fmt(dur); } drawWave(); });
  render(); selectCue(0); drawWave(); frame();
}

function computePeaks(ab){
  const ch = ab.getChannelData(0);
  const N = 1600; peaks = new Float32Array(N*2);
  const step = Math.floor(ch.length/N)||1;
  for(let i=0;i<N;i++){
    let mn=1, mx=-1; const a=i*step, b=Math.min(a+step, ch.length);
    for(let j=a;j<b;j++){ const v=ch[j]; if(v<mn)mn=v; if(v>mx)mx=v; }
    peaks[i*2]=mn; peaks[i*2+1]=mx;
  }
  renderWaveCache();
}

const wave = $("#wave"), wctx = wave.getContext("2d");
const phead = $("#phead"), pctx = phead.getContext("2d");   // overlay: just the moving playhead
const timeEl=$("#time"), playEl=$("#play"), pvEl=$("#pvtext");  // hot-loop refs, resolved once
let durStr="0:00.00", lastPx=-1;   // cached total-time string + last playhead pixel (skip repaint)
function sizeCanvas(){
  const r = wave.getBoundingClientRect(), dpr = devicePixelRatio||1;
  cssW = r.width; cssH = r.height; dprCur = dpr;
  for(const cv of [wave,phead]){ cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr); }
  wctx.setTransform(dpr,0,0,dpr,0,0); pctx.setTransform(dpr,0,0,dpr,0,0);
  lastPx=-1;                    // canvas was cleared by the resize -> force a playhead repaint
  renderWaveCache();            // rebuild the static waveform bitmap for the new size / zoom
  drawWave();                   // repaint the static layer
}
// Render the (expensive) peaks ONCE into an offscreen bitmap; each frame just blits it.
function renderWaveCache(){
  if(!cssW || !cssH) return;
  const c = document.createElement("canvas");
  c.width = Math.round(cssW*dprCur); c.height = Math.round(cssH*dprCur);
  const g = c.getContext("2d"); g.setTransform(dprCur,0,0,dprCur,0,0);
  g.fillStyle=COLOR.bg; g.fillRect(0,0,cssW,cssH);
  if(peaks){ const mid=cssH/2, N=peaks.length/2;
    g.strokeStyle=COLOR.wave; g.beginPath();
    for(let x=0;x<cssW;x++){ const i=Math.floor(x/cssW*N)*2;
      g.moveTo(x, mid + peaks[i]*mid*0.95);
      g.lineTo(x, mid + peaks[i+1]*mid*0.95); }
    g.stroke();
  }
  waveCache = c;
}
function drawWave(){
  const W=cssW, H=cssH; if(!W) return;
  wctx.clearRect(0,0,W,H);
  if(waveCache) wctx.drawImage(waveCache,0,0,W,H);      // cheap: stamp the cached waveform
  else { wctx.fillStyle=COLOR.bg; wctx.fillRect(0,0,W,H); }
  if(!dur) return;
  // all cue starts as faint ticks (one batched path)
  wctx.strokeStyle=COLOR.tick; wctx.beginPath();
  for(const c of CUES){ const x=c.start/dur*W; wctx.moveTo(x,0); wctx.lineTo(x,H); }
  wctx.stroke();
  // selected cue region + handles
  if(sel>=0 && CUES[sel]){ const c=CUES[sel];
    const xs=c.start/dur*W, xe=c.end/dur*W;
    wctx.fillStyle=COLOR.selFill; wctx.fillRect(xs,0,Math.max(1,xe-xs),H);
    wctx.strokeStyle=COLOR.accent; wctx.lineWidth=2;
    wctx.beginPath(); wctx.moveTo(xs,0); wctx.lineTo(xs,H); wctx.moveTo(xe,0); wctx.lineTo(xe,H); wctx.stroke();
    wctx.fillStyle=COLOR.accent; wctx.fillRect(xs-3,H/2-9,6,18); wctx.fillRect(xe-3,H/2-9,6,18);
    wctx.lineWidth=1;
  }
}
// the ONLY thing that moves during playback -- redraw one line on the transparent overlay.
// skips entirely when the playhead maps to the same pixel (e.g. paused), avoiding canvas churn.
function drawPlayhead(t){
  const W=cssW, H=cssH; if(!W||!dur) return;
  const px = Math.round((t==null?nowT():t)/dur*W);
  if(px===lastPx) return;
  lastPx=px;
  pctx.clearRect(0,0,W,H);
  pctx.strokeStyle=COLOR.active; pctx.lineWidth=2; pctx.beginPath();
  pctx.moveTo(px,0); pctx.lineTo(px,H); pctx.stroke();
}

function activeIdx(t){ if(t==null) t=nowT(); let best=-1, bs=-Infinity;
  // among all cues covering t, show the one that STARTED most recently -- like a real caption
  // track -- so an earlier line whose (old) end overlaps into this one can't shadow it
  for(let i=0;i<CUES.length;i++){ const c=CUES[i]; if(t>=c.start && t<c.end && c.start>=bs){ bs=c.start; best=i; } }
  return best; }

let lastActive=-1, lastNow=-1, lastTimeStr="", lastPaused=null;
function frame(){
  const t = nowT();                           // one read per frame; reused everywhere below
  drawPlayhead(t);
  const ts = fmt(t)+" / "+durStr;
  if(ts!==lastTimeStr){ timeEl.textContent=ts; lastTimeStr=ts; }
  const pi = activeIdx(t);                     // the line currently on screen (matches preview)
  // yellow highlight: in tap mode the NEXT line to tap (the pointer); else the playing line
  const hi = tap ? tapPtr : pi;
  if(hi!==lastActive){
    document.querySelectorAll("tr.cue.active").forEach(r=>r.classList.remove("active"));
    if(hi>=0){ const r=document.querySelector('tr.cue[data-i="'+hi+'"]'); if(r)r.classList.add("active"); }
    lastActive=hi;
  }
  // grey "now showing" tint on the row that's currently displayed (even while tapping ahead)
  if(pi!==lastNow){
    document.querySelectorAll("tr.cue.now").forEach(r=>r.classList.remove("now"));
    if(pi>=0){ const r=document.querySelector('tr.cue[data-i="'+pi+'"]'); if(r)r.classList.add("now"); }
    lastNow=pi;
  }
  // preview strip always mimics the real video -- whatever the CURRENT timings show at the playhead
  const pv = (pi>=0 && CUES[pi]) ? CUES[pi].text : "";
  if(pv!==lastPv){ pvEl.textContent=pv; lastPv=pv; }
  if(playCueEnd!=null && t>=playCueEnd){ audio.pause(); playCueEnd=null; }
  const paused = audio.paused;
  if(paused!==lastPaused){ playEl.textContent = paused ? "▶ Play" : "⏸ Pause"; lastPaused=paused; }
  requestAnimationFrame(frame);
}

function render(){
  const tb=$("#rows"); tb.innerHTML="";
  CUES.forEach((c,i)=>{
    const tr=document.createElement("tr"); tr.className="cue"; tr.dataset.i=i;
    if(i===sel) tr.classList.add("sel");
    tr.innerHTML =
      '<td class="idx">'+(i+1)+'</td>'+
      '<td class="startcell"><input class="t s" value="'+c.start.toFixed(2)+'"><span class="delta ds"></span></td>'+
      '<td class="endcell"><input class="t e" value="'+c.end.toFixed(2)+'"><span class="delta de"></span></td>'+
      '<td class="dur'+(isInverted(i)?' warnflag':'')+'"><span class="durval">'+(c.end-c.start).toFixed(2)+'</span><span class="delta dd"></span></td>'+
      '<td class="revcell"><button class="rowbtn rev" title="revert this line to its original time">↺</button></td>'+
      '<td class="spacer"></td>'+
      '<td class="rowacts"><button class="rowbtn seek" title="select + seek here">↦</button></td>'+
      '<td class="textcell"><input class="txt" value="'+esc(c.text.replace(/\n/g,"  "))+'"></td>';
    tr.querySelector(".s").addEventListener("change",e=>{ CUES[i].start=parseFloat(e.target.value)||0; touch(); drawWave(); });
    tr.querySelector(".e").addEventListener("change",e=>{ CUES[i].end=parseFloat(e.target.value)||0; touch(); drawWave(); });
    tr.querySelector(".txt").addEventListener("input",e=>{ CUES[i].text=e.target.value; touch(); });
    tr.querySelector(".seek").addEventListener("click",()=>{ selectCue(i); audio.currentTime=CUES[i].start; if(tap) retarget(i); });
    tr.querySelector(".rev").addEventListener("click",e=>{ e.stopPropagation(); revertLine(i); });
    tr.addEventListener("mousedown",e=>{ if(e.target.tagName!=="INPUT"){ selectCue(i); if(tap) retarget(i); } });
    tb.appendChild(tr);
    setDeltas(tr,i);
  });
  CUES.forEach((c,i)=>refreshRowFlags(i));   // paint overlaps once all rows exist
}

// snap ONE line back to the timing it had when loaded (or last saved). Only lines that
// actually moved have a live button; new/split lines (no o0) can't revert.
function revertLine(i){
  const c=CUES[i]; if(!c||c.o0==null) return;
  c.start=c.o0; c.end=c.oe; edited(i);
}

// show how far each start/end has moved from the originally-loaded timing (ORIG)
function fmtDelta(el,d){
  if(!el) return;                       // keep the .ds/.de hook -- only toggle up/down
  el.classList.remove("up","down");
  if(Math.abs(d)<0.005){ el.textContent=""; return; }
  el.textContent=(d>0?"+":"−")+Math.abs(d).toFixed(2);
  el.classList.add(d>0?"up":"down");
}
function setDeltas(r,i){
  const c=CUES[i]; if(!r||!c) return;   // new lines (add/split) have no o0/oe -> blank
  const ds = c.o0==null?0:c.start-c.o0, de = c.oe==null?0:c.end-c.oe;
  const dd = (c.o0==null||c.oe==null)?0:(c.end-c.start)-(c.oe-c.o0);   // duration change
  fmtDelta(r.querySelector(".ds"), ds);
  fmtDelta(r.querySelector(".de"), de);
  fmtDelta(r.querySelector(".dd"), dd);
  // revert button lives only when this line has actually moved from its original
  const rev=r.querySelector(".rev");
  if(rev){ const changed = c.o0!=null && (Math.abs(ds)>=0.005 || Math.abs(de)>=0.005);
    rev.disabled=!changed; rev.classList.toggle("live",changed); }
}

function selectCue(i){
  sel=i;
  document.querySelectorAll("tr.cue.sel").forEach(r=>r.classList.remove("sel"));
  const r=document.querySelector('tr.cue[data-i="'+i+'"]'); if(r){ r.classList.add("sel"); r.scrollIntoView({block:"nearest"}); }
  $("#sel").textContent = i>=0 ? "#"+(i+1) : "—";
  drawWave();
}
function updateRow(i){
  const r=document.querySelector('tr.cue[data-i="'+i+'"]'); const c=CUES[i]; if(!r||!c)return;
  r.querySelector(".s").value=c.start.toFixed(2);
  r.querySelector(".e").value=c.end.toFixed(2);
  r.querySelector(".durval").textContent=(c.end-c.start).toFixed(2);
  r.querySelector(".dur").classList.toggle("warnflag",isInverted(i));
  setDeltas(r,i);
  refreshRowFlags(i-1); refreshRowFlags(i); refreshRowFlags(i+1);  // overlap can touch neighbors
}
// One source of truth for cue validity, shared by the duration flag and the box highlights.
function isInverted(i){ const c=CUES[i]; return !!c && c.end<=c.start; }
function startOverlaps(i){ const c=CUES[i]; return isInverted(i) || (i>0 && CUES[i-1] && c.start < CUES[i-1].end); }
function endOverlaps(i){ const c=CUES[i]; return isInverted(i) || (i<CUES.length-1 && CUES[i+1] && c.end > CUES[i+1].start); }
// muted-red the box(es) that overlap a neighbor or invert the line. save trims these; this just
// makes them visible.
function refreshRowFlags(i){
  const r=document.querySelector('tr.cue[data-i="'+i+'"]'); if(!r||!CUES[i]) return;
  const s=r.querySelector(".s"), e=r.querySelector(".e");
  if(s) s.classList.toggle("bad", startOverlaps(i));
  if(e) e.classList.toggle("bad", endOverlaps(i));
}
let dirty=false;
function touch(){ dirty=true; $("#status").textContent="unsaved changes"; }
function edited(i){ touch(); updateRow(i); drawWave(); }   // standard "cue i changed" refresh

// dock actions
document.querySelectorAll("#dock [data-nudge]").forEach(b=>b.addEventListener("click",()=>{
  if(sel<0)return; const k=b.dataset.nudge, d=parseFloat(b.dataset.d);
  CUES[sel][k]=Math.max(0,CUES[sel][k]+d); edited(sel);
}));
function playCue(){ if(sel<0)return; audio.currentTime=CUES[sel].start; playCueEnd=CUES[sel].end; audio.play(); }
$("#startPlay").addEventListener("click",()=>{ if(sel<0)return; CUES[sel].start=nowT(); edited(sel); });
$("#endPlay").addEventListener("click",()=>{ if(sel<0)return; CUES[sel].end=nowT(); edited(sel); });
$("#playcue").addEventListener("click",playCue);
$("#split").addEventListener("click",()=>{
  if(sel<0)return; const t=nowT(), c=CUES[sel];
  if(t<=c.start||t>=c.end){ flash("playhead must be inside the line to split"); return; }
  const parts=c.text.split(/\s+/), half=Math.ceil(parts.length/2);
  const a={start:c.start,end:t,text:parts.slice(0,half).join(" ")};
  const b={start:t,end:c.end,text:parts.slice(half).join(" ")};
  CUES.splice(sel,1,a,b); touch(); render(); selectCue(sel);
});
$("#add").addEventListener("click",()=>{
  const t=nowT(); const c={start:t,end:Math.min(dur,t+2),text:"new line"};
  let i=CUES.findIndex(x=>x.start>t); if(i<0)i=CUES.length; CUES.splice(i,0,c); touch(); render(); selectCue(i);
});
$("#del").addEventListener("click",()=>{ if(sel<0)return; CUES.splice(sel,1); touch(); render(); selectCue(Math.min(sel,CUES.length-1)); });

// ---- tap-sync: two modes ----
//  MODE.STARTS     = one press per line at its START; shifts the WHOLE line there (duration
//                    kept). Neighbors untouched; overlaps left as-is (flagged red).
//  MODE.STARTSTOP  = hold through each line (press=start, release=end) to mark real gaps.
const TAP_HELP = {
  starts: 'TAP: STARTS — press <kbd>Ctrl</kbd> (or the button) once, right when the highlighted line STARTS. The whole line moves there (duration kept); neighbors untouched, any overlap left flagged in red. Advances every press.',
  startstop: 'TAP: START+STOP — hold <kbd>Ctrl</kbd> (or the button) while the highlighted line is sung, release at its END. Use where a line should disappear before the next (real gaps).'
};
function tapBtns(){ $("#tapstarts").classList.toggle("on", tap&&tapMode===MODE.STARTS);
                    $("#tapboth").classList.toggle("on", tap&&tapMode===MODE.STARTSTOP); }
function enterTap(mode){
  if(!CUES.length) return;
  tapMode=mode;
  // arm on the first line that STARTS after the playhead -- the next line you'll hear.
  // Already-shown lines (start <= playhead) are skipped; the stale blue selection is ignored.
  let i = CUES.findIndex(c => c.start > nowT());
  if(i<0) i = CUES.length-1;
  tap=true; tapPtr=i; tapHolding=false; lastStarted=-1; lastActive=-2; selectCue(i);
  $("#holdbtn").textContent = mode===MODE.STARTS ? "TAP" : "HOLD";
  $("#taplabel").innerHTML = TAP_HELP[mode] + ' Seek to just before your section first. <kbd>◀</kbd>/<kbd>▶</kbd> re-aim the next line. <kbd>Esc</kbd>/Done to finish — only lines you tap change.';
  $("#tapbanner").style.display="flex"; tapBtns(); updateTapBanner();
}
function exitTap(){
  tap=false; tapHolding=false; keyHold=false; mouseHold=false; lastActive=-2;
  $("#tapbanner").style.display="none"; $("#holdbtn").classList.remove("held"); tapBtns();
}
function retarget(i){ tapPtr=i; tapHolding=false; lastStarted=-1; lastActive=-2; updateTapBanner(); }
function updateTapBanner(){
  const cur=CUES[tapPtr], nxt=CUES[tapPtr+1];
  if(!cur){ $("#tapnow").textContent="— reached the end —"; $("#tapnext").textContent=""; setTimeout(exitTap,700); return; }
  $("#tapnow").textContent=cur.text;
  $("#tapnext").textContent = nxt ? nxt.text : "(last line)";
}
function tapDown(){
  if(!tap||tapPtr<0||tapPtr>=CUES.length) return;
  const t=Math.max(0, nowT() - tapOffset);  // audible position, minus reaction-time comp
  if(tapMode===MODE.STARTS){
    // shift the WHOLE line to where you tapped: start goes to t, end moves by the same delta
    // so the duration is preserved. Neighbors are never touched; any resulting overlap is
    // left as-is and flagged red for you to fix.
    const c=CUES[tapPtr], delta=t-c.start;
    c.start=t; c.end+=delta;
    updateRow(tapPtr);
    lastStarted=tapPtr; tapPtr++; touch(); drawWave();
    if(tapPtr<CUES.length) selectCue(tapPtr); updateTapBanner();
    $("#holdbtn").classList.add("held");
  } else {
    if(tapHolding) return;
    tapHolding=true; CUES[tapPtr].start=t; touch(); selectCue(tapPtr); updateRow(tapPtr); drawWave();
    $("#holdbtn").classList.add("held");
  }
}
function tapUp(){
  if(!tap){ return; }
  if(tapMode===MODE.STARTS){ $("#holdbtn").classList.remove("held"); return; }
  if(!tapHolding) return;
  tapHolding=false; const c=CUES[tapPtr]; c.end=Math.max(0, nowT() - tapOffset);
  if(c.end<=c.start) c.end=c.start+0.1;
  touch(); updateRow(tapPtr); drawWave(); $("#holdbtn").classList.remove("held");
  tapPtr++; if(tapPtr<CUES.length) selectCue(tapPtr); updateTapBanner();
}
$("#tapoffset").addEventListener("input",e=>{ tapOffset=Math.max(0,(parseFloat(e.target.value)||0)/1000); });
$("#tapstarts").addEventListener("click",()=>{ (tap&&tapMode===MODE.STARTS)?exitTap():enterTap(MODE.STARTS); });
$("#tapboth").addEventListener("click",()=>{ (tap&&tapMode===MODE.STARTSTOP)?exitTap():enterTap(MODE.STARTSTOP); });
$("#tapdone").addEventListener("click",exitTap);
$("#holdbtn").addEventListener("mousedown",e=>{ e.preventDefault(); mouseHold=true; tapDown(); });
window.addEventListener("mouseup",()=>{ if(mouseHold){ mouseHold=false; tapUp(); } });

// waveform: click to seek, drag handles of selected cue
let dragging=null;
wave.addEventListener("mousedown",e=>{
  const W=wave.getBoundingClientRect().width, x=e.offsetX, t=x/W*dur;
  if(sel>=0){ const c=CUES[sel], xs=c.start/dur*W, xe=c.end/dur*W;
    if(Math.abs(x-xs)<6){ dragging=DRAG.START; return; }
    if(Math.abs(x-xe)<6){ dragging=DRAG.END; return; } }
  dragging=DRAG.SCRUB;                                 // hold + drag to move the play position
  audio.currentTime=Math.max(0,Math.min(dur,t));
});
window.addEventListener("mousemove",e=>{
  if(!dragging)return;
  const rect=wave.getBoundingClientRect(); const x=e.clientX-rect.left;
  let t=Math.max(0,Math.min(dur,x/rect.width*dur));
  if(dragging===DRAG.SCRUB){ audio.currentTime=t; return; } // just move playback, don't edit a cue
  if(sel<0)return;
  CUES[sel][dragging]=t; edited(sel);
});
window.addEventListener("mouseup",()=>{ dragging=null; });

function togglePlay(){ if(audioCtx&&audioCtx.resume) audioCtx.resume();
  if(audio.paused){ playCueEnd=null; audio.play(); } else audio.pause(); }
playEl.addEventListener("click",togglePlay);

async function save(){
  // sort by start so a dragged/nudged line lands in order, keep sel on same object
  const cur = sel>=0?CUES[sel]:null;
  CUES.sort((a,b)=>a.start-b.start);
  if(cur) sel=CUES.indexOf(cur);
  render(); if(sel>=0) selectCue(sel);
  const r=await fetch(API.SAVE,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({cues:CUES})});
  const j=await r.json();
  if(j.ok){ dirty=false;
    CUES.forEach(c=>{ c.o0=c.start; c.oe=c.end; });   // saved timings become the new baseline
    ORIG=JSON.parse(JSON.stringify(CUES));
    render(); if(sel>=0) selectCue(sel);              // deltas reset to blank
    $("#status").textContent="saved "+j.cues+" cues → "+j.name
      +(j.backup?"  (bak: "+j.backup+")":"")+"  ·  now rerun make_video.py"; }
  else $("#status").textContent="save failed: "+(j.error||"?");
}
$("#save").addEventListener("click",save);
$("#revert").addEventListener("click",()=>{ CUES=JSON.parse(JSON.stringify(ORIG)); dirty=false; $("#status").textContent="reverted";
  render(); selectCue(Math.min(sel,CUES.length-1)); drawWave(); });

function flash(m){ $("#status").textContent=m; }

// keyboard
window.addEventListener("keydown",e=>{
  const typing = e.target.tagName==="INPUT";
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); save(); return; }
  if(typing) return;
  const c=sel>=0?CUES[sel]:null;
  if(e.key===" "){ e.preventDefault(); togglePlay(); }
  else if(e.key==="i"||e.key==="I"){ if(c){c.start=nowT(); edited(sel);} }
  else if(e.key==="o"||e.key==="O"){ if(c){c.end=nowT(); edited(sel);} }
  else if(e.key===","){ if(c){c.start=Math.max(0,c.start-0.05); edited(sel);} }
  else if(e.key==="."){ if(c){c.start+=0.05; edited(sel);} }
  else if(e.key==="["){ if(c){c.end=Math.max(0,c.end-0.05); edited(sel);} }
  else if(e.key==="]"){ if(c){c.end+=0.05; edited(sel);} }
  else if(e.key==="p"||e.key==="P"){ playCue(); }
  else if(tap && (e.key==="ArrowLeft"||e.key==="ArrowRight")){ e.preventDefault();
    // in tap-sync: move the yellow "next to tap" pointer to the prev/next line
    const t=Math.max(0,Math.min(CUES.length-1, tapPtr+(e.key==="ArrowRight"?1:-1)));
    selectCue(t); retarget(t); }
  else if(e.key==="ArrowDown"){ e.preventDefault(); selectCue(Math.min(CUES.length-1,sel+1)); }
  else if(e.key==="ArrowUp"){ e.preventDefault(); selectCue(Math.max(0,sel-1)); }
  else if(e.key==="Escape"){ if(tap) exitTap(); }
});
// tap-sync hold key = Ctrl (fires before the "typing" guard so it works with a row focused too)
window.addEventListener("keydown",e=>{
  if(e.key==="Control"&&tap&&!e.repeat&&!keyHold){ keyHold=true; tapDown(); }
},true);
window.addEventListener("keyup",e=>{ if(e.key==="Control"&&keyHold){ keyHold=false; tapUp(); } });
window.addEventListener("beforeunload",e=>{ if(dirty){ e.preventDefault(); e.returnValue=""; } });
window.addEventListener("resize", sizeCanvas);
sizeCanvas(); load();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

# HTTP routes -- keep in sync with the JS `API` object and `audio_url` in PAGE above.
ROUTE_DATA = "/api/data"
ROUTE_SAVE = "/api/save"
MEDIA_PREFIX = "/media/audio"
AUDIO_URL = "/media/audio.mp3"

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


class Editor:
    mp3: Path = None
    srt: Path = None
    title: str = ""


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_media(self):
            path = Editor.mp3
            try:
                size = path.stat().st_size
            except OSError as e:
                self._send(404, json.dumps({"error": f"audio unavailable: {e}"}))
                return
            rng = self.headers.get("Range")
            m = _RANGE_RE.match(rng) if rng else None   # None if header is absent OR malformed
            if m:
                start = min(int(m.group(1)), size - 1)
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
            else:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(path, "rb") as f:
                    self.wfile.write(f.read())

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path.startswith(MEDIA_PREFIX):
                self._serve_media()
            elif self.path == ROUTE_DATA:
                try:
                    cues = parse_srt(Editor.srt.read_text(encoding="utf-8"))
                except OSError as e:
                    self._send(500, json.dumps({"error": f"cannot read srt: {e}"}))
                    return
                self._send(200, json.dumps({
                    "title": Editor.title,
                    "srt_name": Editor.srt.name,
                    "audio_url": AUDIO_URL,
                    "cues": cues,
                }))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path != ROUTE_SAVE:
                self._send(404, json.dumps({"error": "not found"}))
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                cues = _clean_cues(data.get("cues"))
                cues.sort(key=lambda c: c["start"])
                # overlaps are left exactly as authored (the editor flags them in red) --
                # we intentionally do NOT trim ends here.
                bak = _make_backup(Editor.srt)   # numbered rolling backup before overwrite
                Editor.srt.write_text(write_srt(cues), encoding="utf-8")
                self._send(200, json.dumps({"ok": True, "cues": len(cues),
                                            "name": Editor.srt.name,
                                            "backup": bak.name if bak else None}))
                print(f"saved {len(cues)} cues -> {Editor.srt.name}"
                      + (f"  (backup: {bak.name})" if bak else ""))
            except Exception as e:  # noqa
                self._send(200, json.dumps({"ok": False, "error": str(e)}))
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("song_dir", help="Folder with the .mp3 and .srt (path or bare name)")
    ap.add_argument("--srt", default=None, help="Specific .srt to edit (default: the one named like the song)")
    ap.add_argument("--port", type=int, default=8756, help="Port (default: 8756; auto-bumps if busy)")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args()

    song_dir = find_song_dir(args.song_dir)
    Editor.mp3 = find_one(song_dir, ".mp3")
    if args.srt:
        s = Path(args.srt)
        Editor.srt = s if s.is_absolute() else song_dir / s
        if not Editor.srt.exists():
            sys.exit(f"error: {Editor.srt} not found")
    else:
        Editor.srt = find_one(song_dir, ".srt")
    Editor.title = Editor.mp3.with_suffix("").name

    port = args.port
    for _ in range(20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler())
            break
        except OSError:
            port += 1
    else:
        sys.exit("error: could not find a free port")

    url = f"http://127.0.0.1:{port}/"
    print(f"song   : {song_dir}")
    print(f"audio  : {Editor.mp3.name}")
    print(f"srt    : {Editor.srt.name}")
    print(f"editor : {url}")
    print("Save in the browser writes back to the .srt (keeps a .srt.bak). Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
