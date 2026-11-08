# Operating the iPhone with `phoneuse`

`phoneuse up` runs the AirPlay mirror and mouse control together; on the iPhone,
start Control Center → Screen Mirroring → PhoneUse. Agents then use the commands
below and look at `phoneuse screenshot`, which waits for the mirror to catch up
after the last action, so it always shows the result.

Coordinates are fractions of the screenshot (0 to 1), or iPhone points when any
value is above 1. The screen is 402 × 874 points.

## Loop for an agent

Take a screenshot, find the target, act, take a screenshot again. Taps land
exactly where asked, so there is no need to aim twice. Swipes land within a few
points, which is plenty for scrolling and edge gestures.

## Recipes (verified on an iPhone 17 Pro, iOS 26, November 2026)

| Goal | Command | What it does |
|---|---|---|
| Pointer on | `phoneuse cursor on` | checks first and does nothing if the pointer is already on; otherwise holds the Siri key (consumer usage 0xCF), waits until Siri's orb shows at the bottom centre (about 1-1.4 s), plays "Turn on AssistiveTouch" on the PC speakers, and lets go. `phoneuse cursor` moves the pointer A, B, A, B and wants a pointer-sized change centred on each spot that repeats on both visits, so a moving wallpaper or video can't pass for the pointer |
| Per-phone drag speed | `phoneuse calibrate` | hovers the pointer up from a fixed start with N relative reports of each size (28 down to 1 unit), finds it in the picture, and returns to the start by stepping beside it first (iOS ignores an absolute report that repeats the previous position after relative moves). The table goes to `~/.config/phoneuse/phones.json` under the mirroring phone's name; drags use it while that phone mirrors. An iPhone 15 Pro moved 0.08x-0.22x as far as the 17 Pro per report, and its drags fell far short until calibrated |
| Tap | `phoneuse tap X Y` | press and release at the point |
| Long press | `phoneuse longpress X Y [S]` | hold 0.8 s by default; opens context menus |
| Home | `phoneuse home` | Cmd+H; works from any app |
| Back | `phoneuse back` | swipe right starting at the very left edge (x = 0), like a finger sliding in |
| App switcher | `phoneuse switcher` | slow swipe up from the bottom, then hold |
| Control Center | `phoneuse control-center` | slow (0.6 s) swipe down from the top-right corner |
| Notification Center | `phoneuse notifications` | slow swipe down from the top-left |
| Close an overlay | `phoneuse dismiss` | fast, long swipe up from the very bottom; closes Control Center, Notification Center and the switcher |
| Scroll | `phoneuse scroll down [N]` / `up` | drags the content by N half-screens, slowing down before the lift: no fling |
| Scroll exactly | `phoneuse scroll-by DY [X]` | the same drag by DY (screen fraction, or points above 1); lands within ~5-8% |
| Open an app | `phoneuse open Settings` | Cmd+H, Cmd+Space, type the name, Enter |
| Type | `phoneuse type "hello\n"` | US layout; `\n` presses Enter |
| Keys | `phoneuse key cmd+space` | modifiers: cmd, shift, alt, ctrl |

## Things that don't work, or bite

- **Flings.** iOS keeps scrolling after the lift when the touch's last moves
  were fast, and resting before the lift doesn't stop it (a resting touch sends
  no moves, so the fast ones stay the last): a 0.45 drag at full speed to the
  end moved 0.65-0.67 of the screen even with a 0.4 s rest, and one ending at
  one unit per tick (~450 pt/s) still moved 0.65-0.73. Scroll drags slow down
  further instead (`touch.scroll_drag`): the last 60 pt at one unit per tick,
  the last 20 pt one unit every 3 ticks (~150 pt/s), then 0.15 s still and
  lift. Eight 0.45 scrolls in a row then moved 0.426-0.439. The mirror's wheel
  uses the same profile (`pointer.py`) and never grips for less than 15 pt,
  since a shorter press-and-move reads as a tap.
- iOS hides the iPhone's on-screen keyboard while any Bluetooth keyboard is
  connected, and Eject only toggles it blindly. Dropping the link while idle
  would bring the keyboard back, but once phoneuse drops it, iOS refuses
  phoneuse's reconnects ("Connection refused") until the owner taps PhoneUse in
  Bluetooth settings. Links the phone dropped itself reconnect fine. So the
  daemon's `release` command exists (it declines the iPhone's reconnects and
  drops hovers until the next real input), but nothing calls it.
- Avoid tapping the top centre of the screen: it hits the Dynamic Island, which
  opens the Screen Mirroring popup with a Stop Mirroring button.
- Cmd+H doesn't close Control Center or Notification Center; use `phoneuse dismiss`.
- iOS ignores the mouse wheel and Cmd+Tab. Over the mirror window and in the
  browser mirror, phoneuse turns the wheel into a drag instead (see pointer.py).
- Fast swipes down from the top are ignored; the top-edge recipes are slow on purpose.
- If the phone locks, the passcode screen appears. The agent must stop and ask the
  owner to unlock it. Never type a passcode.

## Why it works this way (HID facts measured on the device)

- The absolute pointer report lands exactly where asked, anywhere on the screen.
- Button presses count only from the absolute pointer, not from the relative mouse.
- While the button is held, an absolute position change ends the touch after about
  90 ms (it reads as a click), so a drag is an absolute press, then relative motion
  (button 0), then an absolute release at the same spot. Verified with touchlab/.
- Move the pointer, wait about 0.1 s, then press. Pressing in the same instant as
  a jump makes iOS start the touch on the way, so an edge swipe turns into a drag
  of the page (this is what broke `back` at first).
- iOS accelerates relative motion by report size and drops motion from reports
  that arrive less than about 8 ms apart, so drags send one report every 8 ms
  (including empty ones, which keeps slow drags alive), sized from a measured
  table (`touch.DRAG_PTS`) and rounded down so the touch never overshoots.

## On Windows (Bluetooth LE)

- Reports reach the phone once per Bluetooth LE connection interval (iOS settles
  on 15 ms for a keyboard), not every 8 ms as over Classic. The HID service spaces
  them at least one interval apart: two reports in one connection event arrive a
  fraction of a millisecond apart, and iOS drops pointer motion that comes faster
  than ~8 ms, so drags would lose distance.
- Each phone's drag table is measured over this link (`phoneuse calibrate`, also
  automatic the first time its pointer is on); the iPhone 17 Pro table from Classic
  doesn't apply. Measured on two phones over LE, a report moved 0.08-0.3x as far as
  that table says (phones also differ by Tracking Speed). Taps use the absolute
  pointer and need no calibration.
- Scroll drags over LE use small steps (at most 6 pt a report) and a steady end, and
  go 8 pt further than asked: a scroll view starts following the finger only after a
  few points, and those never come back (scroll-by fell 7.5-10 pt short).
- `open` checks each step on the screen: the first Spotlight opened right after an
  app closes ignores typed keys, Spotlight keeps its last search, and Enter once left
  the home screen; so it opens Spotlight again when needed, selects the old search,
  and taps the app it finds instead of pressing Enter.
- In the mirror window Ctrl is Cmd: the Windows key belongs to the Start menu.

## iPhone settings this relies on

- Accessibility → Touch → AssistiveTouch: on; Dwell Control: off (otherwise a
  resting pointer taps on its own).
- Optional: Accessibility → Accessories → Pointer Control colour Orange, large size, Automatically
  Hide Pointer off. Only `phoneuse where` and the telemetry tools use it, to find
  the pointer in screenshots; gestures never need to locate the pointer.
- Consider Display & Brightness → Auto-Lock: Never while an agent is working.
