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

Two ways to run it:
  * Bare -- `python subtap.py` -- just hosts the editor; open your own audio + caption files
    from the browser (File System Access API writes edits back to the file on Chrome/Edge, or
    falls back to downloading the .srt). No assumptions about what's on your disk.
  * Pre-loaded -- `python subtap.py "<folder>"` -- a folder holding one .mp3 + one .srt is loaded
    on launch and Save writes back in place, keeping numbered `<name>.srt.bak.NNN` backups.

Usage:
    python subtap.py                       # open files from the browser
    python subtap.py "Hello, World"        # pre-load a song folder (save in place)
    python subtap.py "Artist/Song" --port 8756 --no-browser
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Bump __version__ by hand for real releases; the "build" number auto-increments with every
# git commit (no build step needed), so the displayed version bumps whenever you commit.
__version__ = "1.2.0"
__copyright__ = "© 2026 RelentlessOldMan"


def _version_string() -> str:
    base = f"v{__version__}"
    repo = Path(__file__).resolve().parent

    def _git(*args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    try:
        n = _git("rev-list", "--count", "HEAD")
        h = _git("rev-parse", "--short", "HEAD")
        dirty = "*" if _git("status", "--porcelain") else ""
        if n and h:
            return f"{base} · build {n} · {h}{dirty}"
    except Exception:  # git missing / not a repo -> just show the semantic version
        pass
    return base

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


# A section header in a lyric file, e.g. "**[Verse 1]**" or "[Chorus]" -- a stanza boundary,
# not a sung line.
_HEADER_RE = re.compile(r"^\s*(?:\*\*)?\[[^\]]*\](?:\*\*)?\s*$")


def parse_stanza(text: str):
    """Parse a lyric file into (sung lines, blank-line structure) for overlaying stanza breaks.

    Returns {"lines": [...], "break_after": [n, ...], "lead": n}: the non-blank sung lines in
    order, how many blank lines follow each (a stanza break is >= 1), and blank lines before the
    first line. Section headers are dropped but count as a break after the previous line, so an
    .orig.txt (headed sections) and a .plain.txt (bare, blank-separated) yield the same breaks.
    """
    lines, break_after, lead, pending = [], [], 0, 0
    for ln in text.replace("\r", "").split("\n"):
        if ln.strip() == "":
            if lines:
                pending += 1
            else:
                lead += 1
        elif _HEADER_RE.match(ln):
            if lines:                    # header ends the current stanza (>= one blank of spacing)
                pending = max(pending, 1)
        else:
            if lines:
                break_after[-1] = pending
            pending = 0
            lines.append(ln.strip())
            break_after.append(0)
    return {"lines": lines, "break_after": break_after, "lead": lead}


def find_stanza_ref(srt: Path):
    """The sibling lyric file's stanza structure for `srt`, or None. Prefers the bare .plain.txt,
    then the headed .orig.txt / .txt (whichever exists and has lines)."""
    if srt is None:
        return None
    base = srt.name[:-4] if srt.name.lower().endswith(".srt") else srt.stem
    for suffix in (".plain.txt", ".orig.txt", ".txt"):
        cand = srt.with_name(base + suffix)
        if not cand.exists():
            continue
        try:
            data = parse_stanza(cand.read_text(encoding="utf-8"))
        except OSError:
            continue
        if data["lines"]:
            data["source"] = cand.name
            return data
    return None


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
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root { --bg:#12141a; --panel:#1b1e27; --panel2:#232734; --line:#2c3040;
          --txt:#e6e8ee; --dim:#9aa0b0; --accent:#4fd1ff; --active:#ffd24f;
          --danger:#ff6b6b; --ok:#4fe08a; }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.4 system-ui,Segoe UI,Roboto,Arial,sans-serif;
         display:flex; flex-direction:column; height:100vh; overflow:hidden; }
  header { display:flex; justify-content:center; flex:0 0 auto;
           background:var(--panel); border-bottom:1px solid var(--line); z-index:5;}
  header .bar { display:flex; align-items:center; gap:12px; padding:10px 16px;
                width:100%; max-width:1112px; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  #ver { font-weight:600; font-size:13px; color:var(--txt); white-space:nowrap; }
  #copyr { color:var(--dim); font-size:12px; white-space:nowrap; }
  header .bar .brand { display:flex; flex-direction:column; align-items:flex-start;
                       line-height:1.3; white-space:nowrap; }
  header .bar .grow { flex:1; }
  header .bar .right { display:flex; gap:8px; align-items:center; }
  #toast { position:fixed; left:50%; bottom:16px; transform:translateX(-50%); z-index:50;
           background:var(--panel2); border:1px solid var(--line); color:var(--txt);
           padding:8px 14px; border-radius:8px; font-size:13px; white-space:nowrap;
           box-shadow:0 4px 16px rgba(0,0,0,.45); opacity:0; transition:opacity .2s;
           pointer-events:none; }
  #toast.show { opacity:1; }
  header .bar .right button { min-width:120px; text-align:center; }   /* ALL top-bar buttons same size */
  button.primary:disabled { background:rgba(79,209,255,.16); color:var(--dim); opacity:1;
                            border-color:rgba(79,209,255,.30); cursor:default; font-weight:600; }
  #save:disabled:hover { border-color:rgba(79,209,255,.30); }
  header .grow { flex:1; }
  button { background:var(--panel2); color:var(--txt); border:1px solid var(--line);
           border-radius:6px; padding:5px 9px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#06131a; border-color:var(--accent); font-weight:600; }
  button.warn { border-color:var(--danger); color:var(--danger); }
  #top { padding:12px 16px 6px; flex:0 0 auto; width:100%; max-width:1112px; align-self:center; }
  #tablewrap { flex:1 1 auto; overflow-y:auto; padding:0 16px 10px; }
  #wavebox { display:flex; align-items:stretch; background:var(--panel); border:1px solid var(--line);
             border-radius:8px; overflow:hidden; }
  #wavearea { position:relative; flex:1; min-width:0; }
  #wave { display:block; width:100%; height:150px; cursor:pointer; }
  #phead { position:absolute; top:0; left:0; width:100%; height:150px; pointer-events:none; }
  #transport { display:flex; align-items:center; justify-content:space-between; gap:10px; margin:10px 0; }
  #transport .tgroup { display:flex; align-items:center; gap:10px; }
  #restore { display:none; text-align:center; margin:8px 0; }
  #restore.show { display:block; }
  #restore button { background:var(--panel2); border:1px solid var(--accent); color:var(--accent);
                    padding:8px 16px; border-radius:8px; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.4; cursor:default; }
  button:disabled:hover { border-color:var(--line); }
  #time { font-variant-numeric:tabular-nums; color:var(--dim); }
  table { width:100%; max-width:1080px; border-collapse:collapse; margin:6px auto 0; }
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
  td.spacer { width:76px; }              /* breather between revert and seek+lyric, clearly wider than the delta gaps */
  td.rowacts { width:34px; }
  td.textcell { width:100%; min-width:360px; }   /* lyric fills the rest of the (capped) table */
  td.textcell .txt { min-width:0; }              /* let the input fill its cell, not force it wider */
  .rowbtn.rev.live { border-color:var(--active); color:var(--active); }
  .rowbtn.rev:disabled { opacity:.25; cursor:default; border-color:var(--line); color:var(--dim); }
  .rowbtn.rev:disabled:hover { border-color:var(--line); }
  #dock { flex:0 0 auto; background:var(--panel);
          border-top:1px solid var(--line); padding:8px 16px; display:flex; flex-wrap:wrap;
          gap:6px; align-items:center; justify-content:center; z-index:6; }
  #dock .grp { display:flex; gap:4px; align-items:center; padding:0 8px; border-right:1px solid var(--line);}
  #dock .grp:last-child { border-right:none; }
  #dock .lbl { color:var(--dim); font-size:12px; margin-right:2px; }
  #sel { color:var(--accent); font-weight:600; }
  #help { color:var(--dim); font-size:12px; flex-basis:100%; text-align:center; margin-top:2px; }
  kbd { background:var(--panel2); border:1px solid var(--line); border-bottom-width:2px;
        border-radius:4px; padding:0 5px; font-size:11px; }
  .warnflag { color:var(--danger); }
  #preview { margin-top:8px; min-height:64px; display:flex; align-items:center; justify-content:center;
             background:#0c0d12; border:1px solid var(--line); border-radius:8px; padding:8px 16px; }
  #pvtext { font-size:26px; font-weight:700; text-align:center; color:#fff;
            text-shadow:0 2px 4px rgba(0,0,0,.85); }
  #tapstarts.on,#tapboth.on { background:var(--active); color:#1a1400; border-color:var(--active); font-weight:600; }
  #play,#tapboth,#tapstarts { min-width:150px; text-align:center; }   /* equal-size transport buttons */
  #volwrap { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px;
    flex:0 0 auto; padding:6px 10px; border-left:1px solid var(--line); background:var(--panel2); }
  #volwrap input[type=range] { writing-mode:vertical-lr; direction:rtl; -webkit-appearance:none; appearance:none;
    width:6px; height:96px; background:var(--line); border-radius:3px; cursor:pointer; }
  #volwrap input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; appearance:none;
    width:11px; height:11px; border-radius:50%; background:var(--txt); border:2px solid var(--bg);
    box-shadow:0 0 0 1px var(--accent); }
  #volwrap input[type=range]::-moz-range-thumb { width:11px; height:11px; border-radius:50%; background:var(--txt);
    border:2px solid var(--bg); box-shadow:0 0 0 1px var(--accent); }
  #volwrap input[type=range]::-moz-range-track { width:6px; background:var(--line); border-radius:3px; }
  #volwrap input[type=range]::-moz-range-progress { width:6px; background:var(--accent); border-radius:3px; }
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
  #tapdone { font-size:20px; font-weight:800; padding:16px 34px; border-radius:10px; }
  #diag { flex:0 0 auto; background:var(--panel); border-bottom:1px solid var(--line);
          padding:3px 12px; text-align:center; overflow-x:auto;
          font:11px/1.3 ui-monospace,Consolas,monospace; color:var(--dim); white-space:nowrap; }
</style></head>
<body>
<header>
  <div class="bar">
    <div class="brand">
      <span id="ver">Subtap</span>
      <span id="copyr"></span>
    </div>
    <span class="grow"></span>
    <div class="right">
      <button id="loadaudio" title="open an audio file (wav / mp3 / m4a / ogg / flac)">Load Audio</button>
      <button id="load" title="open a caption file (.srt, or a plain-lyrics .txt to tap from scratch)">Load Captions</button>
      <button class="primary" id="save" disabled>Save SRT</button>
      <button id="savetxt" title="download the caption text as plain lyrics (one line per cue, no timings; stanza breaks restored from the song's lyric sheet when launched with a folder)" disabled>Save TXT</button>
      <button id="revert">Revert</button>
    </div>
    <input type="file" id="audiofile" accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac,.opus" style="display:none">
    <input type="file" id="loadfile" accept=".srt,.txt" style="display:none">
  </div>
</header>

<div id="diag"></div>
<div id="toast"></div>

<div id="top">
  <div id="wavebox"><div id="wavearea"><canvas id="wave"></canvas><canvas id="phead"></canvas></div><span id="volwrap" title="Subtap playback volume -- affects this app only, not your system volume"><input id="vol" type="range" min="0" max="100" value="100">🔊</span></div>
  <div id="preview"><span id="pvtext"></span></div>
  <div id="restore"></div>
  <div id="transport">
    <div class="tgroup">
      <button id="play">▶ Play</button>
      <button id="tapboth" title="hold through each line: press=start, release=end (for real gaps)">⇥ Tap: start+stop</button>
      <button id="tapstarts" title="tap once at each line's START; the whole line moves there (duration kept), overlaps left flagged red">⇥ Tap: starts</button>
    </div>
    <span id="offwrap" title="latency comp: each tap is recorded this many ms earlier than the click">old man reaction offset −<input id="tapoffset" type="number" value="100" step="10" min="0">ms</span>
    <span id="time">0:00.00 / 0:00.00</span>
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
  <span id="help">
    <kbd>Space</kbd> play &nbsp; <kbd>I</kbd>/<kbd>O</kbd> set start/end=playhead &nbsp;
    <kbd>,</kbd>/<kbd>.</kbd> nudge start &nbsp; <kbd>[</kbd>/<kbd>]</kbd> nudge end &nbsp;
    <kbd>P</kbd> play line &nbsp; <kbd>↑</kbd>/<kbd>↓</kbd> select &nbsp; <kbd>Ctrl+S</kbd> save
  </span>
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

let CUES = [], ORIG = [], sel = -1, dur = 0, peaks = null, playCueEnd = null, demoFrozen = false;
let tap=false, tapPtr=-1, tapHolding=false, keyHold=false, mouseHold=false, lastPv="";
let tapMode=MODE.STARTSTOP, lastStarted=-1, tapOffset=0.1;
let audioCtx=null, avSync=0.10;   // A/V-sync auto-set from output latency on first play
let cssW=0, cssH=0, dprCur=1, waveCache=null;
// Playback via the Web Audio API from the decoded buffer -- NOT an <audio> element. This gives
// sample-accurate seeking (the <audio> element seeks MP3s approximately, which desynced audio
// from the clock after a scrub AND baked bad times into tapped cues), and makes outputLatency a
// real number we can trust for A/V sync.
const player = {
  ctx:null, buf:null, node:null, playing:false, startAt:0, offset:0, syncSet:false, onended:null,
  gain:null, vol:1,                                 // Subtap-only volume, applied via a persistent GainNode
  init(ctx, buf){ this.ctx=ctx; this.buf=buf;
    if(ctx && ctx.createGain && !this.gain){ this.gain=ctx.createGain(); this.gain.connect(ctx.destination); }
    if(this.gain) this.gain.gain.value=this.vol; },
  setVolume(v){ this.vol=Math.max(0,Math.min(1,v)); if(this.gain) this.gain.gain.value=this.vol; },
  get duration(){ return this.buf ? this.buf.duration : 0; },
  get paused(){ return !this.playing; },
  position(){ const D=this.duration; let p = this.playing ? (this.ctx.currentTime-this.startAt)+this.offset : this.offset;
    if(p<0)p=0; if(D && p>D)p=D; return p; },
  _start(off){ const n=this.ctx.createBufferSource(); n.buffer=this.buf; n.connect(this.gain||this.ctx.destination);
    n.onended=()=>{ if(this.node===n){ this.playing=false; this.offset=this.duration; this.node=null; if(this.onended)this.onended(); } };
    n.start(0, off); this.node=n; this.startAt=this.ctx.currentTime; this.offset=off; this.playing=true; },
  _stop(){ if(this.node){ const n=this.node; this.node=null; try{ n.onended=null; n.stop(); }catch(e){} } },
  play(){ if(!this.buf || !this.ctx || this.playing) return;
    if(this.ctx.resume) this.ctx.resume();
    let off=this.offset; if(this.duration && off>=this.duration-0.01) off=0;
    this._start(off);
    if(!this.syncSet){ this.syncSet=true;                       // learn real output latency once
      const ol=this.ctx.outputLatency||this.ctx.baseLatency||0;
      avSync=(ol>=0.02 && ol<=0.40)?ol:0.10; } },
  pause(){ if(!this.playing) return; this.offset=this.position(); this._stop(); this.playing=false; },
  seek(t){ const D=this.duration; t=Math.max(0, Math.min(D||t, t));
    if(this.playing){ this._stop(); this._start(t); } else { this.offset=t; } },
};

// The audible position: the buffer clock runs slightly AHEAD of what reaches the speakers
// (output latency), so drive all visuals + taps off this compensated clock.
function nowT(){ return Math.max(0, player.position() - avSync); }

function fmt(t){ if(t==null||isNaN(t))t=0; const m=Math.floor(t/60), s=t-60*m;
  return m+":"+(s<10?"0":"")+s.toFixed(2); }

// escape lyric text before it goes into an HTML attribute (handles & < > " -- order matters)
function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

// Load a set of cues as a fresh starting point: rebuild the table and RESET every delta
// baseline (o0/oe = the loaded values), then set the dirty/saved state. Used by the initial
// load, by Save (re-reading exactly what's on disk), and by the Load button.
function applyCues(arr, opts){
  opts = opts || {};
  CUES = (arr||[]).map(c => { const s=+c.start||0, e=+c.end||0;
    const o={start:s, end:e, text:(c.text!=null?c.text:""), o0:s, oe:e};
    if(c.blanksAfter) o.blanksAfter = c.blanksAfter|0;   // remembered blank lines (stanza breaks)
    return o; });
  ORIG = JSON.parse(JSON.stringify(CUES));
  dirty = !!opts.dirty; justSaved = !!opts.justSaved;
  const keep = (sel>=0 && sel<CUES.length) ? sel : 0;
  render(); selectCue(CUES.length ? Math.min(keep, CUES.length-1) : -1);   // -1 when there are no cues
  drawWave(); refreshSave();
}

let codeHash="";
async function load(){
  const d = await (await fetch(API.DATA)).json();
  $("#ver").textContent = "Subtap " + (d.version || "");
  $("#copyr").textContent = d.copyright || "";
  codeHash = d.code || "";
  player.onended = ()=>{ if(tap) exitTap(); };   // leave tap mode when the audio finishes
  if(d.audio_url){
    // a song was pre-loaded at launch: decode its audio, edit in place (Save writes on server)
    saveMode="server"; audioName=d.title||""; capName=d.srt_name||"";
    stanzaRef = d.stanza || null;   // sibling .plain/.orig stanza breaks for Save TXT (may be null)
    applyCues(d.cues);
    const DEMO = /[?&]demo\b/.test(location.search);
    try {
      audioCtx = new (window.AudioContext||window.webkitAudioContext)();
      const bytes = await (await fetch(d.audio_url)).arrayBuffer();
      // real decode -> real waveform. Race a timeout so headless Chrome (which won't decode audio)
      // falls back to a synthetic waveform instead of hanging forever.
      const ab = await Promise.race([ audioCtx.decodeAudioData(bytes),
        new Promise((_,rej)=>setTimeout(()=>rej(new Error("decode timeout")), 4000)) ]);
      dur=ab.duration; durStr=fmt(dur); durSec=dur.toFixed(2);
      player.init(audioCtx, ab); computePeaks(ab); drawWave();
    } catch(e){ console.warn("audio decode failed/timeout", e); if(DEMO) demoWaveform(); }
    if(DEMO) applyDemoPose();   // fake edits + tap-sync pose on top of the real (or synthetic) waveform
  } else {
    applyCues([]);   // bare: open your own audio + caption files from the top-bar buttons
    tryRestore();    // ...or auto-reload the last session's files if we remember them
  }
  frame();
}

// Synthetic waveform for the demo screenshot (real decodeAudioData hangs in headless Chrome).
function demoWaveform(){
  dur = (CUES.length ? CUES[CUES.length-1].end : 200) + 6;
  durStr=fmt(dur); durSec=dur.toFixed(2);
  player.buf={duration:dur};                       // fake buffer -> controls look live, playhead works
  const N=1600; peaks=new Float32Array(N*2);
  for(let i=0;i<N;i++){
    const t=i/N, body=Math.pow(Math.sin(t*Math.PI), 0.45);   // louder in the middle, like a song
    let v=body*(0.35 + 0.4*Math.abs(Math.sin(i*0.13)) + 0.3*Math.abs(Math.sin(i*0.047)));
    v=Math.min(0.97, v); peaks[i*2]=-v; peaks[i*2+1]=v;
  }
  renderWaveCache();
}

// Pose a real, already-loaded song for a showcase screenshot: nudge a few cues so deltas show,
// force one overlap (red), drop the playhead mid-song, and open tap-sync. Purely cosmetic.
function applyDemoPose(){
  if(CUES.length < 5) return;
  const i = Math.min(3, CUES.length-3);
  CUES[i].start   = Math.max(0, CUES[i].start - 0.44);   // pulled earlier  -> −0.44 delta
  CUES[i+1].end   = CUES[i+1].end + 0.61;                // pushed later, overlaps next -> red
  CUES[i+2].start = CUES[i+2].start + 0.19;              // small +0.19 delta
  dirty=true;
  player.offset = CUES[i+2].start + 0.4;                 // playhead sitting mid-line
  render(); drawWave(); refreshSave();
  enterTap(MODE.STARTSTOP);                              // show the tap-sync banner
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
const timeEl=$("#time"), playEl=$("#play"), pvEl=$("#pvtext"), diagEl=$("#diag");  // hot-loop refs, resolved once
let durStr="0:00.00", durSec="0.00", lastPx=-1;   // cached total-time (m:ss + raw secs) + last playhead pixel
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

let lastActive=-1, lastNow=-1, lastTimeStr="", lastPaused=null, lastDiag="";
function frame(){
  const t = nowT();                           // one read per frame; reused everywhere below
  drawPlayhead(t);
  const ts = fmt(t)+" / "+durStr+"  ("+t.toFixed(2)+" / "+durSec+")";
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
  if(playCueEnd!=null && t>=playCueEnd){ player.pause(); playCueEnd=null; }
  const paused = player.paused;
  if(paused!==lastPaused){ playEl.textContent = paused ? "▶ Play" : "⏸ Pause"; lastPaused=paused; }
  // read-only diagnostics: what we actually read + what we applied (watch outLat for drift)
  if(diagEl){
    const ol = audioCtx ? (audioCtx.outputLatency||0) : 0, bl = audioCtx ? (audioCtx.baseLatency||0) : 0;
    const sr = audioCtx ? audioCtx.sampleRate : 0;
    const loaded = "code "+codeHash+"   ·   "
      + (audioName?("♪ "+audioName+"   ·   "):"")
      + (capName?("▤ "+capName+" ("+CUES.length+" lines)   ·   "):"")
      + (saveMode?("save:"+(saveMode==="handle"?"write-back":saveMode)+"   ·   "):"")
      + "fs:"+(window.showOpenFilePicker?"on":"off")+"   ·   ";
    const ds = loaded + "A/V sync "+Math.round(avSync*1000)+"ms applied   ·   outLat "+Math.round(ol*1000)
      +"ms  baseLat "+Math.round(bl*1000)+"ms   ·   tap −"+Math.round(tapOffset*1000)+"ms"
      +"   ·   "+sr+"Hz";
    if(ds!==lastDiag){ diagEl.textContent=ds; lastDiag=ds; }
  }
  if(!demoFrozen) requestAnimationFrame(frame);   // demo mode renders one static frame for a screenshot
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
    tr.querySelector(".seek").addEventListener("click",()=>{ selectCue(i); player.seek(CUES[i].start); if(tap) retarget(i); });
    tr.querySelector(".rev").addEventListener("click",e=>{ e.stopPropagation(); revertLine(i); });
    tr.addEventListener("mousedown",e=>{ selectCue(i); if(tap && e.target.tagName!=="INPUT") retarget(i); });
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
  drawWave(); refreshSave();   // enable/disable line-dependent controls
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
let dirty=false, justSaved=false;
// the Save button IS the status: muted+disabled when clean ("Saved!" right after a save),
// bright+enabled when there are unsaved edits. Fixed width so it never resizes.
// Enable a button only when clicking it is actually valid right now. Called on every state
// change (load, select, edit, save). refreshSave name kept since it's called from many places.
function refreshSave(){
  const hasAudio = !!player.buf, hasCues = CUES.length>0, hasSel = sel>=0 && sel<CUES.length;
  const b=$("#save"); b.disabled=!dirty; b.textContent=(!dirty&&justSaved)?"Saved!":"Save SRT";
  $("#savetxt").disabled = !hasCues;
  $("#revert").disabled = !dirty;
  $("#play").disabled = !hasAudio;
  $("#tapboth").disabled = !(hasAudio && hasCues);
  $("#tapstarts").disabled = !(hasAudio && hasCues);
  $("#add").disabled = !hasAudio;
  document.querySelectorAll("#dock [data-nudge], #startPlay, #endPlay, #playcue, #split, #del")
    .forEach(x=>{ x.disabled = !hasSel; });
}
function touch(){ dirty=true; justSaved=false; refreshSave(); }
function edited(i){ touch(); updateRow(i); drawWave(); }   // standard "cue i changed" refresh

// dock actions
document.querySelectorAll("#dock [data-nudge]").forEach(b=>b.addEventListener("click",()=>{
  if(sel<0)return; const k=b.dataset.nudge, d=parseFloat(b.dataset.d);
  CUES[sel][k]=Math.max(0,CUES[sel][k]+d); edited(sel);
}));
function playCue(){ if(sel<0)return; player.seek(CUES[sel].start); playCueEnd=CUES[sel].end; player.play(); }
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
  const at=sel>=0?sel+1:CUES.length;                       // insert right after the selected row
  const t=sel>=0?CUES[sel].end:nowT();                     // start where the selected line ends
  const next=CUES[at];                                     // the line we're inserting before (may be undefined)
  let end=t+2;                                             // aim for a 2s line...
  if(next) end=Math.min(end,next.start);                   // ...but don't run into the next line
  if(dur)  end=Math.min(end,dur);                          // ...or past the end of the song
  if(end-t<0.5) end=t+0.5;                                 // never create a zero/negative-duration line
  const c={start:t,end:end,text:"new line"};
  CUES.splice(at,0,c); touch(); render(); selectCue(at);
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
  // Past the last line: STAY in tap mode (don't auto-exit) so you can ◀ back to fix the last
  // line. Tap mode only ends on Esc/Done or when the song actually finishes ("ended").
  if(!cur){ $("#tapnow").textContent="— past the last line · ◀ to go back —"; $("#tapnext").textContent=""; return; }
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
function paintVol(el){ const v=(parseInt(el.value,10)||0);   // fill below the thumb blue to show the level (WebKit)
  el.style.background="linear-gradient(to top, var(--accent) "+v+"%, var(--line) "+v+"%)"; }
$("#vol").addEventListener("input",e=>{ player.setVolume((parseInt(e.target.value,10)||0)/100); paintVol(e.target); });
paintVol($("#vol"));
$("#tapstarts").addEventListener("click",()=>{ (tap&&tapMode===MODE.STARTS)?exitTap():enterTap(MODE.STARTS); });
$("#tapboth").addEventListener("click",()=>{ (tap&&tapMode===MODE.STARTSTOP)?exitTap():enterTap(MODE.STARTSTOP); });
$("#tapdone").addEventListener("click",exitTap);
$("#holdbtn").addEventListener("mousedown",e=>{ e.preventDefault(); mouseHold=true; tapDown(); });
window.addEventListener("mouseup",()=>{ if(mouseHold){ mouseHold=false; tapUp(); } });

// waveform: click to seek, drag handles of selected cue
let dragging=null, scrubResume=false;
wave.addEventListener("mousedown",e=>{
  const W=wave.getBoundingClientRect().width, x=e.offsetX, t=x/W*dur;
  if(sel>=0 && CUES[sel]){ const c=CUES[sel], xs=c.start/dur*W, xe=c.end/dur*W;
    if(Math.abs(x-xs)<6){ dragging=DRAG.START; return; }
    if(Math.abs(x-xe)<6){ dragging=DRAG.END; return; } }
  dragging=DRAG.SCRUB;                                 // hold + drag to move the play position
  // Pause while scrubbing: seeking an <audio> element mid-play makes it report the new
  // currentTime while the decoder keeps emitting the old position -> audio/visual desync.
  // Seek only while paused, then resume on release, so the sound always matches the clock.
  scrubResume = !player.paused; if(scrubResume) player.pause();
  player.seek(Math.max(0,Math.min(dur,t)));
});
window.addEventListener("mousemove",e=>{
  if(!dragging)return;
  const rect=wave.getBoundingClientRect(); const x=e.clientX-rect.left;
  let t=Math.max(0,Math.min(dur,x/rect.width*dur));
  if(dragging===DRAG.SCRUB){ player.seek(t); return; } // just move playback, don't edit a cue
  if(sel<0 || !CUES[sel])return;
  CUES[sel][dragging]=t; edited(sel);
});
window.addEventListener("mouseup",()=>{
  if(dragging===DRAG.SCRUB && scrubResume){ scrubResume=false; player.play(); }
  dragging=null;
});

function togglePlay(){ if(player.paused){ playCueEnd=null; player.play(); } else player.pause(); }
playEl.addEventListener("click",togglePlay);

// Save routes by mode: server (in-place, launched song), handle (File System Access write-back),
// or download (fallback). All three reset the deltas to the just-saved baseline.
async function save(){
  if(saveMode==="server") return saveServer();
  if(!saveMode){ flash("Load a caption file first."); return; }
  const text = buildSRT(CUES), name = capName || "captions.srt";
  try {
    if(saveMode==="handle" && capHandle){
      const w = await capHandle.createWritable(); await w.write(text); await w.close();
      applyCues(CUES, {justSaved:true});
    } else {
      _download(name, text); applyCues(CUES, {justSaved:true}); flash("downloaded "+name);
    }
  } catch(err){ flash("save failed: "+err.message); }
}
async function saveServer(){
  const cur = sel>=0?CUES[sel]:null;
  CUES.sort((a,b)=>a.start-b.start);
  if(cur) sel=CUES.indexOf(cur);
  render(); if(sel>=0) selectCue(sel);
  const r=await fetch(API.SAVE,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({cues:CUES})});
  const j=await r.json();
  if(j.ok){
    // reload exactly what's on disk (server sorted/cleaned it) -> guaranteed-clean new baseline
    try { const d=await (await fetch(API.DATA)).json(); applyCues(d.cues, {justSaved:true}); }
    catch(err){ applyCues(CUES, {justSaved:true}); }
  }
  else flash("save failed: "+(j.error||"?"));
}
$("#save").addEventListener("click",save);
// Save TXT: always a download (no write-back / backups) — the caption text as plain lyrics,
// with stanza breaks restored from the song's sibling lyric sheet when one was found.
function saveTXT(){
  if(!CUES.length){ flash("Load a caption file first."); return; }
  const base = (capName||"captions").replace(/\.[^.]+$/,"").replace(/\.plain$/i,"");
  const name = base+".plain.txt";
  _download(name, buildTXT(CUES));
  flash("downloaded "+name + (stanzaRef ? "  (stanza breaks from "+stanzaRef.source+")" : ""));
}
$("#savetxt").addEventListener("click",saveTXT);
$("#revert").addEventListener("click",()=>{ CUES=JSON.parse(JSON.stringify(ORIG)); dirty=false; justSaved=false; refreshSave();
  render(); selectCue(Math.min(sel,CUES.length-1)); drawWave(); });

// ===== client-side file loading + save (no assumptions about the user's disk) =================
// saveMode: "server" (a song was launched -> save in place) | "handle" (File System Access, write
// back to the opened file) | "download" (fallback: download the .srt). null until something loads.
let saveMode=null, capHandle=null, capName="", audioName="", capsPlaceholder=false;
// blank lines from a loaded .txt are dropped from the UI (no empty cues) but remembered so a
// re-save reproduces them: lyricLead = blank lines before the first line, and each cue carries
// a .blanksAfter count. .srt has no place for them, so it just ignores them (stays clean).
let lyricLead=0;
// stanza-break template for a launched song: the sibling .plain/.orig lyric file's blank-line
// structure, sent by the server. Overlaid onto the cues at Save TXT time (the .srt itself has
// nowhere to store blank lines). null for a bare/dropped .srt with no sibling lyric file.
let stanzaRef=null;

// ---- SRT / plain-lyrics parsing ----
function _tsToSec(t){ const m=t.match(/(\d+):(\d{2}):(\d{2})[,.](\d{3})/);
  return m ? (+m[1])*3600+(+m[2])*60+(+m[3])+(+m[4])/1000 : 0; }
function parseSRT(text){
  const cues=[];
  for(const block of text.replace(/\r/g,"").trim().split(/\n\s*\n/)){
    const lines = block.split("\n").filter(l=>l.trim()!=="");
    if(!lines.length) continue;
    const i = (/^\d+$/.test(lines[0].trim()) && lines[1] && lines[1].includes("-->")) ? 1 : 0;
    if(i>=lines.length || !lines[i].includes("-->")) continue;
    const parts = lines[i].split("-->");
    cues.push({start:_tsToSec(parts[0]), end:_tsToSec(parts[1]||""), text:lines.slice(i+1).join("\n").trim()});
  }
  return cues;
}
// Split plain lyrics into one entry per non-blank line, remembering blank lines: leading blanks
// (before the first line) and, on each entry, blanksAfter = blank lines that followed it. This is
// what lets Save reproduce stanza breaks that the UI itself doesn't show as cues.
function parseLyrics(text){
  const rows=[]; let pending=0, lead=0;
  for(const line of text.replace(/\r/g,"").split("\n")){
    if(line.trim()===""){ rows.length ? pending++ : lead++; continue; }
    if(rows.length) rows[rows.length-1].blanksAfter = pending;
    pending=0; rows.push({text:line.trim(), blanksAfter:0});
  }
  return {rows, lead};   // trailing blanks after the last line are dropped
}
function parseCaptions(text){
  lyricLead=0;                                           // reset; only a .txt sets it
  const srt = parseSRT(text);
  if(srt.length){ capsPlaceholder=false; return srt; }   // real timestamps -> leave them alone
  // no timestamps -> plain lyrics (.txt): one cue per non-blank line, blank lines remembered.
  const {rows, lead} = parseLyrics(text); lyricLead=lead;
  const mk = (start,end,r)=>({start, end, text:r.text, blanksAfter:r.blanksAfter});
  if(dur>0){                                              // audio known -> spread across the song
    capsPlaceholder=false; const seg=dur/Math.max(rows.length,1);
    return rows.map((r,i)=>mk(i*seg, (i+1)*seg, r));
  }
  capsPlaceholder=true;                                   // no audio yet -> 2s default, re-spread later
  return rows.map((r,i)=>mk(i*2, i*2+2, r));
}
// ---- SRT building (for handle write-back / download) ----
function _pad(n,w){ n=String(n); while(n.length<w) n="0"+n; return n; }
function _secToTs(s){ if(s<0||isNaN(s))s=0; const ms=Math.round(s*1000);
  return _pad(Math.floor(ms/3600000),2)+":"+_pad(Math.floor(ms%3600000/60000),2)+":"
    +_pad(Math.floor(ms%60000/1000),2)+","+_pad(ms%1000,3); }
function buildSRT(cues){
  return [...cues].sort((a,b)=>a.start-b.start)
    .map((c,i)=>(i+1)+"\n"+_secToTs(c.start)+" --> "+_secToTs(c.end)+"\n"+(c.text||"")).join("\n\n")+"\n";
}
// plain lyrics: the cue text from the .srt, sorted by time, verbatim (internal line breaks /
// whitespace preserved). One cue per line, with stanza breaks re-inserted so the saved lyrics
// keep their verse/chorus spacing. Text-less cues (a just-added line, or one whose text was
// cleared) contribute nothing to a lyrics file -- skip them, so they don't emit stray blank
// lines that masquerade as stanza breaks.
//
// Where the breaks come from:
//   * a loaded plain-lyrics .txt remembers its own blank lines (lyricLead + each cue's
//     .blanksAfter), so a load->save round-trip is exact; and
//   * a launched song's .srt has no blank lines, so we overlay them from the sibling lyric
//     file (.plain/.orig, sent by the server as `stanzaRef`).
// match key for aligning cue text to a reference line: lowercase, punctuation-insensitive
// (the .srt often has trailing commas/periods the lyric sheet omits). Only used for matching --
// the saved text is always the verbatim cue text.
function _norm(s){ return String(s).toLowerCase().replace(/[^\p{L}\p{N}]+/gu," ").trim(); }
// Longest-common-subsequence match of the cue texts against the reference lyric lines, so
// retiming, text fixes, and inserted lines don't shift the alignment (and repeated lines like
// a chorus still line up by position). Returns match[cueIndex] = reference line index, or -1
// for a cue with no counterpart (e.g. a line you added, which isn't in the reference).
function _lcsMatch(a, b){
  const n=a.length, m=b.length, dp=Array.from({length:n+1},()=>new Int32Array(m+1));
  for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--)
    dp[i][j] = a[i]===b[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
  const match=new Array(n).fill(-1); let i=0,j=0;
  while(i<n && j<m){
    if(a[i]===b[j]){ match[i]=j; i++; j++; }
    else if(dp[i+1][j] >= dp[i][j+1]) i++; else j++;
  }
  return match;
}
// Overlay the sibling lyric file's stanza breaks onto the current cues. Each reference break
// (after reference line j) is placed at the largest gap of silence among the cues spanning
// that boundary -- so an inserted line glues to whichever neighbour is closer in time, and the
// user fixes it if that guessed wrong. Returns {lead, breakAfter[]} or null if no reference.
function overlayBreaks(sorted){
  const ref=stanzaRef; if(!ref||!(ref.lines||[]).length) return null;
  const match=_lcsMatch(sorted.map(c=>_norm(c.text)), ref.lines.map(_norm));
  const refToCue={}; match.forEach((r,ci)=>{ if(r>=0) refToCue[r]=ci; });
  const matched=Object.keys(refToCue).map(Number).sort((x,y)=>x-y);
  const ba=ref.break_after||[], breakAfter=new Array(sorted.length).fill(0);
  for(let j=0;j<ba.length;j++){
    if(!(ba[j]>0)) continue;
    let left=-1; for(const r of matched){ if(r<=j) left=r; else break; }
    if(left<0) continue;                             // this boundary's left side was edited away
    let right=-1; for(const r of matched){ if(r>=j+1){ right=r; break; } }
    const a=refToCue[left], b=right>=0 ? refToCue[right] : sorted.length-1;
    let bestK=a, best=-Infinity;                     // largest silence in cues a..b
    for(let k=a;k<b;k++){ const g=sorted[k+1].start - sorted[k].end; if(g>best){ best=g; bestK=k; } }
    breakAfter[bestK]=Math.max(breakAfter[bestK], ba[j]);
  }
  return {lead: ref.lead|0, breakAfter};
}
function buildTXT(cues){
  const sorted=[...cues].sort((a,b)=>a.start-b.start).filter(c=>(c.text||"").trim()!==""), out=[];
  const ov = overlayBreaks(sorted);                  // stanza breaks from the sibling lyric file
  const lead = ov ? ov.lead : lyricLead;
  for(let i=0;i<lead;i++) out.push("");
  sorted.forEach((c,i)=>{
    out.push(c.text);
    if(i<sorted.length-1){
      const blanks = ov ? ov.breakAfter[i] : (c.blanksAfter|0);
      for(let k=0;k<blanks;k++) out.push("");
    }
  });
  return out.join("\n")+"\n";
}
function _download(name, text){
  const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([text],{type:"text/plain"}));
  a.download=name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

// ---- audio: decode any file the browser supports, feed the player + waveform ----
async function loadAudioBuffer(arrbuf, name){
  audioCtx = audioCtx || new (window.AudioContext||window.webkitAudioContext)();
  const ab = await audioCtx.decodeAudioData(arrbuf);
  player.pause(); player.init(audioCtx, ab); player.syncSet=false; player.offset=0;
  dur=ab.duration; durStr=fmt(dur); durSec=dur.toFixed(2);
  // if the captions are still the un-placed 2s default (txt loaded before audio) and untouched,
  // now that we know the length, spread them evenly across the song.
  if(capsPlaceholder && !dirty && CUES.length){
    const seg=dur/CUES.length;
    CUES.forEach((c,i)=>{ c.start=i*seg; c.end=(i+1)*seg; c.o0=c.start; c.oe=c.end; });
    ORIG=JSON.parse(JSON.stringify(CUES)); capsPlaceholder=false; render();
  }
  computePeaks(ab); drawWave();
  audioName=name; refreshSave();   // audio present -> enable play/tap/add
}
async function openAudioHandle(h){ const f=await h.getFile();
  await loadAudioBuffer(await f.arrayBuffer(), f.name); idbSet("audio", h); }
$("#loadaudio").addEventListener("click", async ()=>{
  if(window.showOpenFilePicker){
    try {
      const [h] = await window.showOpenFilePicker({ types:[{ description:"Audio",
        accept:{ "audio/mpeg":[".mp3"], "audio/wav":[".wav"], "audio/mp4":[".m4a",".mp4"],
                 "audio/ogg":[".ogg",".opus"], "audio/flac":[".flac"], "audio/aac":[".aac"] } }] });
      await openAudioHandle(h); return;
    } catch(err){ if(err.name==="AbortError") return; }   // any other error -> plain input below
  }
  $("#audiofile").click();   // fallback file input (works everywhere; just not remembered)
});
$("#audiofile").addEventListener("change", async e=>{
  const f=e.target.files[0]; e.target.value=""; if(!f) return;
  try { await loadAudioBuffer(await f.arrayBuffer(), f.name); }
  catch(err){ flash("couldn't decode "+f.name+" — "+err.message); }
});

// ---- captions: File System Access (write-back) when available, else a file input (download) ----
function setCaptions(cues, name, handle){
  capHandle=handle||null; capName=name||"captions.srt"; saveMode = handle ? "handle" : "download";
  applyCues(cues);   // fresh baseline: deltas blank, not dirty (filename + count show in the stats bar)
}
$("#load").addEventListener("click", async ()=>{
  if(window.showOpenFilePicker){
    try {
      const [h] = await window.showOpenFilePicker({ types:[{ description:"Captions",
        accept:{"text/plain":[".srt",".txt"]} }] });
      const f = await h.getFile();
      setCaptions(parseCaptions(await f.text()), f.name, h); idbSet("caps", h);
    } catch(err){ if(err.name!=="AbortError") flash("open failed: "+err.message); }
  } else { $("#loadfile").click(); }
});
$("#loadfile").addEventListener("change", async e=>{
  const f=e.target.files[0]; e.target.value=""; if(!f) return;
  setCaptions(parseCaptions(await f.text()), f.name, null);   // no handle -> Save downloads
});

// ===== remember the last audio + caption files across refreshes (Chrome/Edge; graceful else) =====
// File System Access handles are stored in IndexedDB; on reload we re-open them (with a one-click
// permission grant if needed). Anything that fails -- file moved, renamed, permission denied --
// is skipped silently. It can never break the app or lose more than the un-saved edits.
function _idb(){ return new Promise((res,rej)=>{ let r; try{ r=indexedDB.open("subtap",1); }catch(e){ return rej(e); }
  r.onupgradeneeded=()=>r.result.createObjectStore("kv"); r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error); }); }
async function idbSet(k,v){ try{ const db=await _idb();
  await new Promise((res,rej)=>{ const tx=db.transaction("kv","readwrite"); tx.objectStore("kv").put(v,k);
    tx.oncomplete=res; tx.onerror=()=>rej(tx.error); }); db.close(); }catch(e){} }
async function idbGet(k){ try{ const db=await _idb();
  const v=await new Promise((res,rej)=>{ const tx=db.transaction("kv","readonly"); const g=tx.objectStore("kv").get(k);
    g.onsuccess=()=>res(g.result); g.onerror=()=>rej(g.error); }); db.close(); return v; }catch(e){ return null; } }

function hideRestore(){ const el=$("#restore"); if(el){ el.classList.remove("show"); el.innerHTML=""; } }
async function doRestore(ah, ch){
  // load whatever still resolves; a missing/renamed file is skipped and its dead handle cleared
  if(ch){ try{ const f=await ch.getFile(); setCaptions(parseCaptions(await f.text()), f.name, ch); idbSet("caps",ch); }
          catch(e){ idbSet("caps",null); } }
  if(ah){ try{ const f=await ah.getFile(); await loadAudioBuffer(await f.arrayBuffer(), f.name); idbSet("audio",ah); }
          catch(e){ idbSet("audio",null); } }
  hideRestore();
}
function showRestore(ah, ch){
  const names=[ah&&"audio", ch&&"captions"].filter(Boolean).join(" + ");
  const el=$("#restore"); el.innerHTML='<button>↺ Reload last: '+names+'</button>'; el.classList.add("show");
  el.querySelector("button").addEventListener("click", async ()=>{
    try{ if(ah) await ah.requestPermission({mode:"read"}); }catch(e){}
    try{ if(ch) await ch.requestPermission({mode:"readwrite"}); }catch(e){}
    await doRestore(ah, ch);
  });
}
async function tryRestore(){
  if(!window.showOpenFilePicker) return;                 // no persistent handles without FS Access
  let ah=null, ch=null;
  try{ ah=await idbGet("audio"); ch=await idbGet("caps"); }catch(e){ return; }
  if(!ah && !ch) return;
  let needGesture=false;
  try{
    if(ah && await ah.queryPermission({mode:"read"})!=="granted") needGesture=true;
    if(ch && await ch.queryPermission({mode:"readwrite"})!=="granted") needGesture=true;
  }catch(e){ needGesture=true; }
  if(needGesture) showRestore(ah, ch); else await doRestore(ah, ch);
}

let _toastTimer=null;
function flash(m){                       // brief auto-fading toast (keeps the top bar clean)
  const t=$("#toast"); if(!t) return;
  if(!m){ t.classList.remove("show"); return; }
  t.textContent=m; t.classList.add("show");
  if(_toastTimer) clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>t.classList.remove("show"), 3200);
}

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

# short fingerprint of the served page -- shown in the UI so you can confirm the browser has the
# current code (the version string only changes on a git commit, so it can't tell you that).
PAGE_HASH = hashlib.sha1(PAGE.encode("utf-8")).hexdigest()[:7]

# favicon: a little waveform with a playhead, in the app's colors (SVG scales crisp at any size)
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#1b1e27"/>'
    '<g fill="#4fd1ff">'
    '<rect x="5" y="13" width="3" height="6" rx="1.5"/>'
    '<rect x="10" y="10" width="3" height="12" rx="1.5"/>'
    '<rect x="19" y="7" width="3" height="18" rx="1.5"/>'
    '<rect x="24" y="12" width="3" height="8" rx="1.5"/>'
    '</g>'
    '<rect x="14.75" y="4" width="2.5" height="24" rx="1.25" fill="#ffd24f"/>'
    '</svg>'
)

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


class Editor:
    mp3: Path = None
    srt: Path = None
    title: str = ""
    version: str = ""


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
            self.send_header("Cache-Control", "no-store")   # always serve fresh; never cache the app
            self.end_headers()
            self.wfile.write(body)

        def _serve_media(self):
            path = Editor.mp3
            if path is None:
                self._send(404, json.dumps({"error": "no audio loaded on the server"}))
                return
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
            path = self.path.split("?", 1)[0]   # route on the path, ignoring any ?query
            if path == "/" or path.startswith("/index"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path in ("/favicon.svg", "/favicon.ico"):
                self._send(200, FAVICON, "image/svg+xml")
            elif path.startswith(MEDIA_PREFIX):
                self._serve_media()
            elif path == ROUTE_DATA:
                if Editor.srt is None:      # no song pre-loaded -> empty; user opens files in the UI
                    self._send(200, json.dumps({
                        "title": "", "srt_name": "", "audio_url": None,
                        "version": Editor.version, "copyright": __copyright__,
                        "code": PAGE_HASH, "cues": [],
                    }))
                    return
                try:
                    cues = parse_srt(Editor.srt.read_text(encoding="utf-8"))
                except OSError as e:
                    self._send(500, json.dumps({"error": f"cannot read srt: {e}"}))
                    return
                self._send(200, json.dumps({
                    "title": Editor.title,
                    "srt_name": Editor.srt.name,
                    "audio_url": AUDIO_URL,
                    "version": Editor.version,
                    "copyright": __copyright__,
                    "code": PAGE_HASH,
                    "cues": cues,
                    "stanza": find_stanza_ref(Editor.srt),   # sibling .plain/.orig breaks for Save TXT
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
    ap.add_argument("song_dir", nargs="?", default=None,
                    help="Optional: a folder with an .mp3 and .srt to pre-load. Omit it and just "
                         "open audio + caption files from the browser (recommended for general use).")
    ap.add_argument("--srt", default=None, help="Specific .srt to edit (with song_dir)")
    ap.add_argument("--port", type=int, default=8756, help="Port (default: 8756; auto-bumps if busy)")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args()

    # song_dir is OPTIONAL. With it, Subtap pre-loads that song and Save writes back in place
    # (keeping numbered backups). Without it, the server just hosts the editor and you open your
    # own audio + caption files from the browser -- no assumptions about what's on disk.
    if args.song_dir:
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
    Editor.version = _version_string()
    print(f"Subtap {Editor.version}  {__copyright__}")

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
    if Editor.srt:
        print(f"song   : {Editor.srt.parent}")
        print(f"audio  : {Editor.mp3.name}")
        print(f"srt    : {Editor.srt.name}  (Save writes back here, keeping .srt.bak.NNN)")
    else:
        print("no song pre-loaded -- open your audio + caption files from the browser")
    print(f"editor : {url}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
