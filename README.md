# Subtap 〰️

**Tap your subtitles into time.** A tiny, dependency-free local web app for retiming a song's
`.srt` captions *by ear* — waveform, sample-accurate playback, per-line deltas, and a
DistroKid-style **tap-to-sync** that works on *just a section* of the song instead of forcing you
to do the whole thing in one take.

It's a **single Python file** (~1,000 lines) with **zero dependencies** beyond the standard
library. No `pip install`, no `node_modules`, no build step. Run it and it opens a browser editor
that saves straight back to your `.srt`.

![Subtap in action](docs/screenshot.png)

> *Want to see it populated without loading anything? Run it and visit `http://localhost:8756/?demo=1`.*

## Why

Forced-alignment tools (Whisper, stable-ts, etc.) get you 90% of the way, but reverb, held notes,
and ad-libs throw them off. Subtap is for fixing that last 10% where **your ear is the ground
truth** — nudge a line, or just hold a key and tap the lines into place as the song plays.

## Features

- **Waveform** you can click or drag to scrub (sample-accurate — playback runs through the Web
  Audio API off the decoded buffer, not a flaky `<audio>` element).
- **Tap-sync a chosen section** — arm on the next line, hold a key as each line is sung, and the
  *whole line* drops to where you tap (duration preserved). Two modes: **start+stop** (hold per
  line) and **starts** (one tap per line). Adjustable reaction-time offset.
- **Per-line deltas** — every Start / End / Duration shows exactly how far it moved from the
  original, so you can see what you changed at a glance.
- **One-click revert** per line (lights up only on lines that actually moved).
- **Overlap warnings** — start/end boxes turn red when a line collides with a neighbor.
- **"Now showing"** row highlight tracks the current lyric as it plays.
- **Nudge / snap-to-playhead / drag-edges / split / add / delete**, all keyboard-friendly.
- **Load a plain-lyrics `.txt`** and it spreads the lines evenly across the song, ready to tap
  from scratch.
- **Numbered backups** in server mode — every save writes `<name>.srt.bak.NNN` first, so a save
  can never clobber your only copy.

## Requirements

- Python 3.8+
- A modern browser (Chrome/Edge recommended — see [Saving](#saving))

## Usage

Just run it and open your files from the browser:

```sh
python subtap.py
```

> **Windows:** just double-click **`Subtap.cmd`**.

Click **Load Audio** (wav / mp3 / m4a / ogg / flac) and **Load Captions** (`.srt`, or a
plain-lyrics `.txt`). Edit, then **Save SRT**.

### Pre-load a folder (optional)

Point it at a folder holding one `.mp3` + one `.srt` and that song loads on launch:

```sh
python subtap.py "path/to/song folder"
python subtap.py "Artist/Song" --port 8756 --no-browser
```

### Keys

`Space` play · `I`/`O` set start/end to playhead · `,`/`.` nudge start · `[`/`]` nudge end
· `P` play line · `↑`/`↓` select · `◀`/`▶` re-aim the tap pointer · `Ctrl` tap-sync hold · `Ctrl+S` save

## Saving

| Mode | How | Where it saves |
|------|-----|----------------|
| **Pre-loaded folder** | `python subtap.py "<folder>"` | In place, on disk, keeping numbered `.srt.bak.NNN` backups. Works in **any** browser. |
| **Chrome / Edge** (files opened in the UI) | File System Access API | **Writes back to the file you opened.** Refresh even offers to reload your last session. |
| **Firefox / Safari** (files opened in the UI) | download fallback | Downloads the edited `.srt` (those browsers can't write local files). |

The stats bar shows the current mode (`save:write-back` / `save:download`).

## How it works

A tiny `http.server` serves one HTML page and a few endpoints (`/api/data`, `/api/save`, a
range-request audio stream). The browser does the heavy lifting — decoding the audio into a buffer
for the waveform, playing it back through the Web Audio API, and drawing on a canvas — while Python
just reads/writes the `.srt`. That's the whole architecture, in one file.

Curious? Everything lives in `subtap.py` — the Python server up top, then the whole browser app
inside one `PAGE = r"""..."""` string. Visit `?demo=1` to see it fully populated with no files loaded.

## Layout

```
Subtap/
  subtap.py            the whole thing — server + browser app, one file, stdlib only
  Subtap.cmd           double-click to run it (Windows)
  release.ps1          one-shot: bump version, tag, cut a GitHub release with subtap.py attached
  docs/screenshot.png  hero image (a posed ?demo=1 render)
  README.md  LICENSE  .gitignore
```

## Contributing

This is a personal tool, published as-is — **issues and pull requests aren't accepted** (PRs
auto-close). Fork it and make it your own. 〰️

## License

MIT — see [LICENSE](LICENSE). © 2026 RelentlessOldMan.
