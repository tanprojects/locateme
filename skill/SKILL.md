---
name: route-c
description: Drive Eugene's Allens laptop over Route C (Elgato 4K X capture + KMBox input) through the resident `routec` daemon on his Mac. Use for any Route C read or write (Outlook, Teams, Laurel, iManage on the laptop), and whenever a read of the laptop comes back blank. Supersedes calling kvm_native.py directly.
---

# Route C through `routec`

A daemon on the Mac (`routecd`, launchd agent `com.ekorre.routec`) holds the Elgato stream and the
KMBox serial port. Use the `routec` command for everything. It needs no camera permission of its
own, so it works from any native macOS shell.

## Transport

Run `routec` on the Mac through Desktop Commander `start_process` (the remote or standalone
server; either works now that the daemon owns the camera). Never `device_bash`: that is a Linux VM.
Full path if PATH lacks it: `/opt/homebrew/bin/routec`.

## Start of every run

1. `routec status` (one line). If it does not start with `OK`, run `routec doctor` and act on
   what it says. **Never report "blank screen".** Report the verdict instead:
   - `CAPTURE OK, LAPTOP PICTURE BLACK`: the laptop is asleep, locked or has no signal. Frames are
     arriving. Take the lease and run `routec wake`, then `routec status` again.
   - `NO PICTURE (no_frames | stalled | device_busy | ...)`: a capture fault; the line names the
     cause and the fix. `routec restart-capture` is safe to try once.
   - `LETTERBOXED`: the laptop's display mode changed; restore it before any other input.
2. `routec status --deep` also checks for a lock or sign-in screen. If locked: stop, record it,
   tell Eugene. **Never type a password.**
3. `routec presence`. If `ACTIVE`, Eugene (or another agent) is using the laptop: read only, do
   not send input, try later. Input commands enforce this themselves and refuse with
   `human_active`.

## Reading (safe any time, no lease)

- `routec screen [X Y W H]`: lines as `cx cy  text`; the coordinates are click points.
- `routec find "text" [--box X Y W H]`: centre of the matched words.
- `routec ocr X Y W H`: text of a region.
- `routec look [X Y W H] [--crop X Y W H ...]`: prints JPEG paths; view them with Desktop
  Commander `read_file`.
- `routec wait "text" [--gone] [--timeout S]`: wait for text to appear or disappear, instead of
  sleeping.
- A read printing `WARNING: laptop picture is ...` means the laptop is not showing a live
  picture. Absent text on such a frame proves nothing.
- OCR is Apple Vision. If a result says `tesseract (DEGRADED)`, label that read as degraded.

## Writing

```sh
export ROUTEC_OWNER=<your-slug>
routec lease <your-slug> 30 [--unattended]     # unattended weekday leases end at 07:00
routec click X Y | click-text "Save" | type "..." | key ctr-l a | wheel X Y N
routec run steps.json                          # several steps in one job (see README)
routec release <your-slug>                     # ALWAYS, including after a failure
```

- Every input command prints `job <id>` first. **If a call errors or times out, run
  `routec job <id>`. Never re-send the command.** Re-sending with `--id <id>` is also safe: it
  returns the existing job and runs nothing.
- `may_have_executed: true` on a failed job means read the screen before anything else.
- Each click reports `screen_change`. A `warning: no visible change` means check the target on a
  fresh read; do not just click again.
- Typing has no length limit (it runs in the daemon). Typing is refused while a password prompt
  or lock screen is visible.
- No backspace key name: use `back`. Control is `ctr-l`. Bare `r` / `del` are refused (Laurel
  Release/Delete).
- Primary buttons (white text on colour) are invisible to OCR: find a neighbour and click the
  offset (`click-text "Cancel" --offset DX DY`), then confirm by reading.
- In batches prefer `wait` / `assert` / `refute` steps over `sleep`, so a batch stops when the
  screen is not what it expected.

## End of every run

`routec release <slug>`. Leave Outlook on the Inbox with the search cleared.

## If routec itself is broken

`routec doctor`, then `routec logs 60`. Fallback: `kvm_native.py` still works and shares the lease
file, but it has the old per-call capture and its blank-screen failure modes.
