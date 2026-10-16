# StageManagerGuard

[English](README.en.md) | [简体中文](README.md)

![CI](https://github.com/Test2049micro/StageManagerGuard/actions/workflows/test.yml/badge.svg)

**Automatically turns off macOS Stage Manager when an external display is connected, and restores your original setting once you're back to a single display.**

![Architecture: guard polls displays and writes request.json, daemon clicks the Stage Manager toggle in System Settings and reports back via result.json](assets/architecture.svg)

It is the only Stage Manager switch that **remembers your original setting**: off on
multi-display, back to **whatever you had before** on single-display — including
"you never had it on", in which case it will not switch it on behind your back.

## Why

With an external monitor, the Stage Manager app strip eats into your screen edge and
gets in the way of window dragging; back on the laptop screen you usually want it
again. Doing this by hand in System Settings is two trips a day.

Apple exposes no API to control Stage Manager — the similar project StageControl
stalled precisely because of that. This tool takes a different route: it uses the
Accessibility (AX) layer to click the real toggle in System Settings, working around
the dead end.

## How it works (and why it has to be this roundabout)

Verified on macOS 27 (macOS 13+ should behave the same; not tested on every release):

1. **`defaults write com.apple.windowmanager GloballyEnabled` does nothing.**
   The preference is written to disk and WindowManager is not even restarted, but
   actual system behavior never changes; `killall -HUP WindowManager` and a real
   `killall WindowManager` are equally ineffective (confirmed by reading back the
   checkbox in System Settings).
2. **The only reliable channel is clicking the Stage Manager toggle in
   System Settings › Desktop & Dock.** After a click, the UI value, the preference,
   and real window behavior all change together.
3. **A launchd job cannot click it.** Processes spawned by launchd hold no
   Accessibility permission (System Events always answers `AppleEvent timed out`);
   AX grants are bound to the user login session.

Hence the two-process architecture:

```
┌──────────────────────────────┐      request.json      ┌──────────────────────────────┐
│ guard (launchd, every 3s)    │ ────────────────────▶  │ daemon (resident, user session)│
│ count displays (NSScreen) →  │ ◀────────────────────  │ actually clicks the toggle     │
│ decide; never touches GUI    │      result.json       │ poll → click → read back       │
└──────────────────────────────┘                        └──────────────────────────────┘
```

- Display count uses the `NSScreen` view (mirrored/sleeping displays change the
  usable count; `system_profiler` over-reports).
- Every click is **verified by reading back** (both the UI value and the preference).
  "No error" does not mean "success".
- State files distinguish "closed by this tool" from "closed by you": only the
  former gets restored.
- Requests carry a monotonically increasing seq, an inflight rename-claim, and a
  stale-lease reaper, preventing double execution and silent stalls.

## Install

Requirements: macOS 13+ (the version Stage Manager shipped in), Command Line Tools
(`xcode-select --install`).

```bash
git clone https://github.com/Test2049micro/StageManagerGuard.git
cd StageManagerGuard
./install.sh
```

Then finish **the one authorization step by hand** (macOS grants Accessibility per
binary, so only you can do it):

> System Settings › Privacy & Security › Accessibility → enable the python3 path
> printed by `install.sh` when it finishes

Without the grant, the daemon keeps logging `AppleEvent timed out` and the feature
stays inert (it will not do anything wrong, either). If you see the raw osascript
error "Not authorized to send Apple events", the daemon now translates it into the
same hint in its log.

## Uninstall

```bash
./uninstall.sh
```

## Known limitations (honest list)

- **Reliability: 6/6 measured successes (34–40 s each), with stated preconditions.**
  One controlled run (alternating the switch 6 times, checking real system state
  each round): daemon self-reported 6/6, actual state achieved 6/6, **zero drift**.
  Earlier development saw 34 cumulative `cannot open settings pane` failures —
  caused by the test harness racing the guard's 3 s poll over the same state files
  (a harness bug), plus the Settings pane needing 18–28 s to cold start.
  **Please re-test in your own multi-display environment**: a passing single-display
  run does not guarantee the multi-display trigger path.
- **Not yet verified end-to-end with real multi-display hot-plug.** During late
  development the external display was physically disconnected; the
  "multi-display → auto-off" main path was validated only against a stub
  (screen_count=3) for decision logic and plumbing. If you try it, issues with logs
  are welcome.
- The System Settings window appears briefly while clicking (auto-closes after
  2 minutes idle).
- **Where the 34–40 seconds come from** (all measured, not estimated):
  1. Settings pane cold start ~16 s (only when the window was previously closed);
  2. Each `osascript` call ~8–11 s (AppleEvent connection overhead, unavoidable);
  3. After a click, the preference (`GloballyEnabled`) lags the UI value by ~**5 s**.
  Items 2 and 3 stack: the first read-back usually sees the preference not yet
  updated, so a second confirmation round is needed. Read-back requires UI and
  preference to agree before declaring success — slow, but no false positives.
- If Apple renames the toggle or moves the pane in a future macOS, read-back fails
  and the tool **leaves your setting untouched** (fails safe: never turns on a
  feature you didn't have on).
- Multi-display detection counts screens actually in service per `NSScreen`;
  mirrored displays count as two.
- **Non-English systems**: the toggle name and pane title are localized (e.g.
  Simplified Chinese「台前调度」/「桌面与程序坞」). Chinese/English candidate names are
  built in (Chinese values taken from Apple's own loctable), but **Chinese-language
  systems are unverified on real hardware**; other languages need extra entries in
  `TOGGLE_CANDIDATES` / `WINDOW_CANDIDATES` (values can be read from
  `DesktopSettings.appex/Contents/Resources/Localizable.loctable`).

## How to check it's working

```bash
# 1) Both jobs are alive
launchctl print gui/$(id -u)/com.local.stage-manager-guard | grep -E 'state|last exit'
launchctl print gui/$(id -u)/com.local.stage-manager-daemon | grep -E 'state'

# 2) Probe and decision are healthy
python3 src/stage_manager_guard.py --probe
# e.g. {"screens": 2, "pref_enabled": true, "closed_by_script": false}

# 3) Logs (self-rotating, ~2MB x 2)
#    ~/Library/Application Support/StageManagerGuard/guard.log
#    ~/Library/Application Support/StageManagerGuard/daemon.log
```

Common failures:

| Symptom | Cause | Fix |
|---|---|---|
| daemon.log repeats `AppleEvent timed out` | Accessibility not granted | System Settings › Privacy & Security › Accessibility, enable the python3 printed by install.sh |
| daemon.log repeats `cannot open settings pane` | intermittent AX laziness | retries automatically; if it persists for hours, restart the daemon: `launchctl kickstart -k gui/$(id -u)/com.local.stage-manager-daemon` |
| Crash but launchd log is empty | plist sends stdout to /dev/null | crash tracebacks go to guard.log (lines starting with `FATAL:`) |
| Displays changed but nothing happens | probe failure makes the guard refuse to guess | check guard.log for `ABORT`; make sure `./bin/DisplayProbe screens` runs |

## Development

```bash
make build    # build DisplayProbe (swiftc ships with macOS)
make test     # run the regression suite (no third-party deps)
```

## License

MIT
