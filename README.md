# Subtap

**Tap your subtitles into time.** A tiny, dependency-free local web app for retiming a song's
`.srt` caption timings by ear — waveform, playback, and a DistroKid-style tap-to-sync that
works on *just a section* of the song instead of forcing you to do the whole thing in one take.

It's a single Python file (~840 lines) with **zero dependencies** beyond the standard library.
No `pip install`, no `node_modules`, no build step. Run it, and it opens a browser editor that
saves straight back to your `.srt`.

## Why

Forced-alignment tools (Whisper/stable-ts, etc.) get you 90% of the way, but reverb, held
notes, and ad-libs throw them off. Subtap is for fixing that last 10% where **your ear is the
ground truth** — nudge a line, or just hold a key and tap the lines into place as the song plays.

## Features

- **Waveform** you can click or drag to scrub the play position.
- **Tap-sync** a chosen section: arm on the next line, hold a key (Ctrl) as each line is sung
  — the *whole line* drops to where you tap (duration preserved). Two modes: tap-starts and
  hold-through (start+stop). Adjustable reaction-time offset.
- **Per-line deltas** — every Start / End / Duration shows exactly how far it moved from the
  original, so you can see what you changed at a glance.
- **One-click revert** per line (lights up only on lines that actually moved).
- **Overlap warnings** — start/end boxes turn red when a line collides with a neighbor.
- **"Now showing"** row highlight tracks the current lyric as it plays.
- **Nudge / snap-to-playhead / drag-edges / split / add / delete**, all keyboard-friendly.
- **Numbered backups** — every save writes `<name>.srt.bak.NNN` first, so a save can never
  clobber your only copy.

## Requirements

- Python 3.8+
- A modern browser (uses Web Audio + Canvas)

## Usage

```sh
python subtap.py "path/to/song folder"     # a folder containing one .mp3 and one .srt
python subtap.py "Song Name"               # bare name: searched in cwd, and one level deep
python subtap.py "Artist/Song" --port 8756
python subtap.py "Song" --no-browser       # don't auto-open the browser
```

The folder just needs one `.mp3` and one `.srt` (named like the folder, if there are several).
Edit, hit **Save**, and it writes back to the `.srt` (keeping numbered backups). Then re-render
your video however you like.

### Keys

`Space` play · `I`/`O` set start/end to playhead · `,`/`.` nudge start · `[`/`]` nudge end
· `P` play line · `↑`/`↓` select · `◀`/`▶` re-aim the tap pointer · `Ctrl` tap-sync hold · `Ctrl+S` save

## How it works

A tiny `http.server` serves one HTML page and three endpoints (`/api/data`, `/api/save`, and a
range-request audio stream). The browser does the heavy lifting — decoding the audio for the
waveform, drawing on a canvas, and playing back — while Python only reads/writes the `.srt`.
That's the whole architecture.

## License

MIT — see [LICENSE](LICENSE).
