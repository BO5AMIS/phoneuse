# How phoneuse works

phoneuse has two independent channels to the phone. The screen comes in over
AirPlay, the same way it would go to an Apple TV, and input goes out over
Bluetooth, with the PC pretending to be a mouse and keyboard. Nothing runs on the
phone. Everything else in the repository connects these two channels: turning
clicks on the mirror window into touches, and giving agents commands and a way to
read the screen.

```
            AirPlay video (Wi-Fi)                      Bluetooth HID (mouse + keyboard)
 iPhone  ─────────────────────────▶  mirror window      ◀─────────────────────────  phoneuse-hid daemon
                                     + browser mirror                                   (root, systemd)
                                     (phoneuse up)
                                          │                                                 ▲
                         Hyprland IPC: where the window is, where the cursor is             │ command socket
                                          ▼                                                 │ /run/phoneuse-hid.sock
                                     mirror_input  ──── hover / press / drag / keys ────────┤
                                     browser mirror ─── hover / press / drag / keys ────────┤
                                     phoneuse CLI   ──── tap / swipe / type / home ... ──────┘
                                          │
                                     vision service (OCR + grounding) for `phoneuse text/find`
```

## The pieces

**`phoneuse/hid_daemon.py`** runs as root under systemd (`phoneuse-hid`). It
registers a classic Bluetooth HID profile with BlueZ (the SDP record in
`sdp_record.xml`), owns the HID control and interrupt channels, and pairs with
"Just Works". Its report descriptor declares three things: a keyboard, a
relative mouse and an absolute pointer. Other programs send it one-line commands
on `/run/phoneuse-hid.sock`: `a X Y BUTTON` for the absolute pointer, `m DX DY
WHEEL BUTTON` for the relative mouse, `k MODS KEYS` and `type TEXT` for the
keyboard. It also keeps the Bluetooth link responsive (below). Any paired device
may connect, and a new connection replaces the current one.
`scripts/install.sh` reconfigures bluetoothd so this can work: the input plugin
would otherwise claim the HID channels, and the hostname plugin would reset the
device class from "keyboard and mouse" to "computer".

**`phoneuse up`** (`phoneuse/cli.py`) starts the AirPlay receiver (UxPlay), the
video hub, the frames service, the mirror window, the browser mirror and the
mirror input handler,
and restarts any of them that exits. The mirror input
handler is a separate process that reloads when its code changes, so edits don't
drop the AirPlay connection.

**`phoneuse/mirror_input.py`** makes the mirror window behave like the phone's
screen. It reads mouse buttons and keys from `/dev/input` read-only, so it never
takes over the PC's mouse or keyboard, and gets the cursor position and window
geometry from Hyprland's IPC socket, which answers in about 0.03 ms.

**`phoneuse/window.py`** is the mirror window, an ordinary GTK 4 window with
Hyprland's own decorations. It fills itself with a dark bezel and draws the
phone picture in the middle with the screen's rounded corners; presses on the
bezel become touches at the nearest screen edge. A Hyprland rule floats it and
keeps its aspect ratio when you resize it, and if it ends up another shape
anyway, the picture stays aspect-fit and the bezel gets wider.

**`phoneuse/pointer.py`** turns a pointer over the mirror into touches on the
phone: hover, press, drag and release, as described under "What iOS does with
mouse input" below. The mirror window's input handler and the browser
mirror both use it, so both behave the same.

**`phoneuse/hub.py`** is the video hub. UxPlay sends the phone's stream to it,
and it serves every viewer. iOS sends a full keyframe only when mirroring starts
(a measured minute of use had none), so anything that starts later couldn't
decode the phone's own stream. The hub decodes the stream once, keeps the last
frame, and encodes it again with NVENC (x264 without an NVIDIA GPU): no
B-frames, keyframes only on request. When a viewer joins, the hub encodes its
last frame again as a keyframe, so the viewer shows the phone at once and can
restart at any time. Only a hub restart needs Screen Mirroring restarted, so the
hub does nothing else. NVDEC copies each frame into system memory there: in CUDA
output mode it hands out its own decode surfaces, which iOS's long reference
chains reuse while the hub still holds the last one, smearing green over the
picture.

**`phoneuse/frames.py`** is the frames service, a hub viewer that watches the
picture for agents. It counts meaningful changes (a coarse luma thumbnail per
frame; the pointer moving or a caret blinking doesn't count; regions that have
moved without pause for over a second, like video, are ignored) and answers
`settle`: return as soon as an action's result has appeared and big motion (a
page, a scroll) has stopped; small motion that never stops (tiles cycling)
counts as ambient and is reported, not waited for. No fixed delays. The MCP
server doesn't wait at all by default: each action returns frames from 0.1, 0.4
and 0.8 s after it in one image (identical frames dropped), and the agent judges
the motion itself, calling `wait` when it wants another look. It
also hands out screenshots of the current picture, so they don't depend on the
mirror window being uncovered.

**`phoneuse/web.py`** is the browser mirror at http://localhost:8777. It takes
the hub's stream and passes the encoded frames to each tab over a WebSocket,
asking the hub for a keyframe when a tab joins. Tabs decode with WebCodecs
(`phoneuse/web/index.html`) and send pointer and key events back. The server
listens on 127.0.0.1 only and accepts input only while the phone is mirroring
and the tab is showing it. While an agent acts (every CLI or MCP action checks
in), the page glows, shows where the agent points, and takes no input from the
page; Take over makes the agent's next action fail with a stop message until
the user resumes it. A pill shows when the Bluetooth link is down.

## The mirror

UxPlay receives the AirPlay stream and passes the H.264 video on, undecoded, as
RTP over localhost (`-vrtp`) to the hub, which sends its own stream on to the
mirror window (port 5600) and the browser mirror (5602). The mirror window
decodes it with GStreamer and draws each frame as soon as it is decoded
(`sync=false`), on the GPU when NVDEC is available. Drawing in our own window
lets the window be resized and styled freely; UxPlay's own GL window crashed
when resized. The window and the browser mirror can restart at any time: they
get a keyframe from the hub when they join. Only a restart of the hub itself
needs Screen Mirroring reconnected.

iOS sends a frame only when the screen changes, and its stream doesn't declare
that frames never need reordering. A default H.264 decoder therefore keeps each
frame until the next one arrives, which GStreamer's latency tracer measured at
200 ms. `nvh264dec compliance=flexible` releases frames in about 1 ms, and the
software decoder does too. A pointer move shows up in the mirror about 100 ms
later, most of it the phone encoding and sending the video; the hub's second
encode adds roughly 15 ms (two samples, 117 ms against 101 ms).

Audio comes along with the mirror. The playback buffer is kept at 40 ms instead
of the sink's default of up to 200 ms.

## Tools for finding out what's wrong

`tools/telemetry.py` records a session: raw mouse events, the cursor position
every 4 ms, every Bluetooth report sent (btmon), and a 60 fps video of the
mirror. `tools/telemetry_analyze.py` lines these up and tracks the pointer in
every frame, which is how the drag problems above were found. `touchlab/` shows
what iOS turns the reports into. `tools/latency.py` times pointer jumps from
report to screen, and `PHONEUSE_TRACE=1 phoneuse up` turns on GStreamer's
per-element latency tracer.

## On Windows

The same two channels, built from what Windows offers. Everything above the HID
command protocol and the frames service (pointer.py, touch.py, the CLI, the MCP
server, the browser page, settling, vision) is shared.

```
            AirPlay video (Wi-Fi)                         Bluetooth LE HID (HID over GATT)
 iPhone  ─────────────────────────▶  UxPlay (MSYS2 build)      ◀──────────  windows/hid.py
                                       │ RTP, 127.0.0.1:5590                  (Windows Runtime GATT server,
                                       ▼                                       user process, no driver)
                                    avhub.py ── TCP subscriptions ──▶ frames.py, web.py      ▲
                                    (PyAV: decode once, NVENC again)       │                  │ 127.0.0.1 + token
                                                                    Edge app window ─ input ──┤ (ipc.py)
                                                                    phoneuse CLI / MCP ───────┘
```

**`phoneuse/windows/hid.py`** replaces the BlueZ daemon. Windows has no API for
being a Bluetooth Classic HID device, but its GATT server (`GattServiceProvider`)
accepts the HID service (0x1812): the PC's own adapter advertises it, and the
iPhone pairs and connects as with any Bluetooth LE keyboard. The report
descriptor is the Linux one (`hidproto.py`), one Report characteristic per report
id with its Report Reference descriptor; reading them needs encryption, which
makes the phone bond first. Windows refuses to publish a connectable service that
isn't discoverable (measured: aborted), so the battery service is advertised too.
The service speaks the Linux daemon's command protocol. Differences: the phone
connects, never the PC (`connect` waits for the phone to come back), and reports
go out once per connection interval. phoneuse asks Windows for throughput-
optimized connection parameters and spaces reports at least one interval apart,
since two reports in one connection event reach iOS a fraction of a millisecond
apart and iOS drops pointer motion faster than ~8 ms. Each phone's drag table is
measured as on Linux (calibrate.py, phones.json), at this pace; until then
touch.BLE_REF_PTS stands in (an iPhone 14 Pro Max over LE). The service also runs on
its own, outside `phoneuse up`: each start republishes the GATT services, and an
iPhone that saw them disappear and come back kept using a stale copy until it was
paired again. Reports that land in a link drop wait (up to 5 s) for the phone to
come back instead of failing.

**`phoneuse/avhub.py`** is the video hub, in PyAV instead of GStreamer: it
reassembles UxPlay's RTP (`rtp.py`), decodes with FFmpeg in low-delay mode, keeps
the last frame and encodes again with NVENC (x264 without an NVIDIA GPU; 2.7 ms a
frame on an RTX 4080). Viewers subscribe over a TCP connection and get a keyframe
of the last frame at once; a subscription lasts as long as its connection, so
there is nothing to renew or time out, and a viewer that falls behind skips to a
fresh keyframe instead of piling up delay. Whether a phone is mirroring comes from
UxPlay's TCP connection on port 7100 (psutil).

**UxPlay** is built from source in MSYS2 (`scripts/windows/build-uxplay.sh`),
pinned to a tested commit; 1.74 brings its own mDNS responder, so Bonjour isn't
needed. It logs with `printf` and never flushes, which over a pipe would hold back
the "connection request from" line that tells the HID service which phone is
mirroring; a forced-include header turns stdout buffering off (a compiler flag,
no source change). UxPlay would take its AirPlay device ID from the first network
adapter's MAC, often a Hyper-V adapter whose address changes, so phoneuse passes a
fixed random one (`airplay-id` in the config directory).

**Local services** (`ipc.py`) use TCP on 127.0.0.1 instead of unix sockets. Each
connection starts with a per-user token from `%LOCALAPPDATA%\phoneuse\run`:
without it any web page could POST line commands to these ports. Connecting to a
closed local port normally takes 2 s on Windows (SYN retries); ipc.py turns the
retries off, so a stopped service is noticed at once.

**`phoneuse up`** puts every service but the Bluetooth one in a job object that
kills them when `up` ends, however it ends (closed window, Ctrl+C, crash), so no
orphaned uxplay.exe holds the AirPlay ports. It refuses to start twice. The mirror window is the
browser mirror in Edge or Chrome app mode with a profile of its own; in it, Ctrl
is Cmd, since the Windows key belongs to the Start menu. The saved passcode is
encrypted with DPAPI for the Windows user; unlock requests also show as a Windows
notification that opens the page. `phoneuse doctor` checks the whole setup,
including Block rules Windows adds for UxPlay when its firewall prompt is
dismissed, which would otherwise silently hide the receiver from the phone.

`tests/` checks this side without a phone: `e2e_video.py` streams a synthetic
picture through the hub, frames service and browser mirror the way UxPlay does;
`e2e_page.py` has headless Edge decode it; `test_offline.py` covers RTP
reassembly, the HID tables, the absolute-report nudge and the screen size by model.
