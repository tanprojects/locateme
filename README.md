# Route C

A resident driver on the Mac for the Allens laptop: the **Elgato 4K X** reads the screen, the
**KMBox** sends mouse and keyboard. One daemon (`routecd`) owns both devices; everything else,
Claude included, uses the `routec` command.

## Why it exists

Claude kept reporting a "blank screen" when the laptop was plainly on. The old driver
(`Proxy/kvm_native.py`) started a fresh ffmpeg for every read, and that ffmpeg borrowed the
camera permission of whichever shell ran it. From a shell without it, the grab hung or came back
black, and nothing said why. The other causes were black warm-up frames, OBS holding the device,
and stalled pipelines.

Route C fixes those causes structurally:

| Old failure | Route C |
|---|---|
| Shell lacks camera permission: grab hangs or is black | Only the daemon opens the Elgato, under its own fixed identity (`RouteC.app`). Callers need no permission. |
| First frame after opening is black | The stream stays warm; frames inside the warm-up window are never served. |
| Stalled or stale pipeline looks like a blank screen | Watchdog reopens a stalled stream; a read never returns a frame older than about 1.5 s. |
| "Blank" never explained | Every frame carries a verdict: `live`, `black` (laptop asleep or no signal), `uniform`, `letterboxed`. The stream separately names its fault: `no_frames`, `stalled`, `device_busy`, `format_unsupported`, `device_missing`. |
| A timed-out command re-run double-types | Input runs as journalled jobs with ids. Asking again with the same id returns the result and never re-runs. |
| ~230-character typing limit (transport window) | Typing runs in the daemon; the caller waits or polls. No length limit. |

## Install (on the Mac)

```sh
cd ~/Claude/Projects/Proxy/RouteC
./install.sh
```

The installer builds `~/Applications/RouteC.app` (a small launcher that gives the daemon a stable
camera identity), installs the launchd agent `com.ekorre.routec` (starts at login, restarts if it
dies) and links `routec` into `/opt/homebrew/bin`. Approve the one-time camera prompt for RouteC.
Re-running `install.sh` updates the code and keeps the app, so the camera grant survives updates.
`./uninstall.sh` removes it.

Requirements already on the Mac: the Python 3.13 framework build with pyserial, Pillow and PyObjC
(Vision), and Homebrew ffmpeg.

## Use

```sh
routec doctor                      # every check, with the fix for anything wrong
routec status                      # one line: can Claude see the laptop, and if not, why
routec screen [X Y W H]            # OCR lines as "cx cy  text"
routec find "Inbox - Eugene"       # where text is -> click point
routec look [X Y W H]              # JPEG snapshot path to view
routec view --open                 # live view in a browser: exactly what Claude sees

export ROUTEC_OWNER=inbox-sweep
routec lease inbox-sweep 30        # required before any input
routec click 3668 651
routec click-text "Create entry"
routec type "Reviewing Frasers AOA; $1,500 + GST."
routec key ctr-l a
routec run steps.json              # many steps, one job
routec release inbox-sweep         # always, including after a failure
```

Coordinates are capture-frame pixels (3840x2160), the same as `kvm_native.py`, so existing
coordinate tables still apply.

### Batch steps (`routec run`)

Input: `click`, `rclick`, `dclick` (x, y, optional `mods`, `count`), `click_text` (text, `box`,
`index`, `offset`), `move`, `type` (text), `key` (keys), `wheel` (x, y, n, `repeat`).
Reads: `screen`, `ocr`, `find`, `assert`, `refute` (text, `box`), `wait` (text, `timeout`,
`gone`), `wait_change` (`box`, `timeout`), `snapshot`, `presence`, `sleep` (seconds).
Any step can carry `settle` (seconds). Boxes are `[x, y, w, h]`. The whole batch is checked
before anything is sent; the first failed step stops it and the report says how far it got.

### Jobs

Every input command prints its job id first. If the call is cut off, run `routec job ID`. Never
re-send the command. A failed job reports `may_have_executed`; when true, read the screen before
doing anything else.

## Safety rules built in

- **Lease**: input needs the Route C lease (same file as `kvm_native.py` and the console, so they
  respect each other). `--unattended` leases taken on a weekday before 07:00 end at 07:00.
- **Presence**: input is refused unless the screen has been quiet for 15 s (ignoring the cursor,
  the jiggler, the caret and the clock), and it is re-checked before every step of a batch.
- **Picture**: no input to a black, blank or letterboxed screen (`routec wake` nudges the mouse
  to wake it).
- **Passwords**: typing is refused while a lock screen or a visible password prompt is on screen.
- **Laurel**: bare `r` and `del` keys are refused (Release and Delete).
- **OCR**: Apple Vision. If Vision fails, tesseract is used and every result says `DEGRADED`.

## Files

| Path | What |
|---|---|
| `routec/capture.py` | persistent ffmpeg stream, watchdog, fault naming, OBS fallback |
| `routec/frames.py` | frame health verdicts, change detection |
| `routec/ocr.py` | Vision OCR (tiled), find, lock-screen detection |
| `routec/kmbox.py` | KMBox serial REPL, pointer, typing (firmware rules from `kvm_native.py`) |
| `routec/engine.py` | pre-flight checks, input steps, batches |
| `routec/jobs.py` | job queue and journal |
| `routec/presence.py` | continuous presence monitor |
| `routec/lease.py` | the shared input lease |
| `routec/server.py` | loopback HTTP API and live view |
| `routec/cli.py` | the `routec` command |
| `macos/` | launcher app source, Info.plist, launchd agent |
| `skill/SKILL.md` | how Claude should drive Route C |

State: `~/Library/Application Support/RouteC` (token, job journal). Logs: `~/Library/Logs/RouteC`.
Snapshots: `Proxy/.kvm-test/routec` (newest 60 kept; they contain client material).
Settings: `~/.config/routec/config.json` overrides any key in `routec/config.py`.

## Tests

`python3 -m pytest -q` runs the whole stack against a fake ffmpeg and a fake KMBox (Linux or Mac).
