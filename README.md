# phoneuse

> [!WARNING]
> **Under heavy construction.** This is a technology demo, and I have tested it on
> one setup: an iPhone 17 Pro, Arch Linux, Hyprland and an NVIDIA GPU. Other setups
> will likely need fixes.

phoneuse lets a Linux PC, or a Windows PC ([On Windows](#on-windows)), see and
operate an unmodified iPhone. The iPhone sends its
screen to the PC over AirPlay, and the PC sends taps and keystrokes to the iPhone as
a Bluetooth mouse and keyboard. The PC can do that with its own Bluetooth adapter or
through a small ESP32 bridge that you leave next to the phone. You can use the mirror
window as you would use the phone, or let an agent operate the phone from the command
line or over MCP.

You don't need a jailbreak, a developer account, a cable or an app on the phone.
phoneuse uses two features that every iPhone has: Screen Mirroring and AssistiveTouch.

## What you need

| | Tested with |
|---|---|
| iPhone | iPhone 17 Pro, iOS 26 (phoneuse takes the screen size from the model) |
| Linux with BlueZ | Arch Linux, BlueZ 5.87 |
| Hyprland | 0.56 |
| UxPlay | 1.73.7 |
| GStreamer, GTK 4, `gtk4paintablesink` | 1.28, 4.24 |
| Python 3.11+ | `python-dbus`, `python-gobject`, `numpy`, `pillow`, `aiohttp` |
| `grim` | takes screenshots for agents |
| `uv` (optional) | runs the models that read the screen |
| For the bridge (optional) | LilyGO T-Display (ESP32), ESP-IDF v6.1, `python-bleak`, `python-cryptography`, `python-pyserial` |

The phone and the PC must be on the same network. If you run a firewall, open TCP
ports 7000, 7001 and 7100 and UDP ports 6000, 6001 and 7011 for AirPlay, and UDP
port 47700 for the bridge. The companion app needs TCP port 8778.

## Setup

1. **Install:**
   ```sh
   git clone https://github.com/lxe/phoneuse.git ~/phoneuse
   ln -s ~/phoneuse/bin/phoneuse ~/.local/bin/phoneuse
   phoneuse install   # needs sudo
   ```
   `phoneuse install` sets up the HID daemon as the `phoneuse-hid` service. It also
   turns off bluetoothd's input plugin, so the PC stops accepting Bluetooth keyboards
   and mice for itself. To undo the install, run `sudo scripts/install.sh --uninstall`.

2. **Start phoneuse:**
   ```sh
   phoneuse up
   ```
   Then, on the iPhone, open Control Center, tap Screen Mirroring and choose
   **PhoneUse**.

3. **Connect the mouse and keyboard**, in one of two ways:
   - **With this PC's Bluetooth:** on the iPhone, open Settings › Bluetooth and tap
     **PhoneUse**.
   - **With the bridge:** follow [The Bluetooth bridge](#the-bluetooth-bridge) below.

4. **Turn on the pointer** (AssistiveTouch), in one of two ways:
   - Run `phoneuse cursor on`. phoneuse plays a spoken request through this PC's
     speakers and asks Siri to turn it on.
   - On the iPhone, open Settings › Accessibility › Touch › AssistiveTouch.

   Keep Dwell Control off: when it is on, iOS taps wherever the pointer stops. The
   first time you turn on a phone's pointer, phoneuse spends about 13 seconds
   measuring how far that phone moves the pointer for each mouse report, and it uses
   that measurement for drags. To measure again, run `phoneuse calibrate`.

## The Bluetooth bridge

The bridge is an ESP32 board that the iPhone pairs with as its Bluetooth keyboard and
mouse. phoneuse sends commands to the bridge over Wi-Fi, so you can leave the bridge
next to the phone on a battery or a charger, and the PC's own Bluetooth adapter stays
unused.

1. **Flash the bridge** once, over USB. Install ESP-IDF, then build and flash the
   firmware:
   ```sh
   git clone -b v6.1 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v6.1
   ~/esp/esp-idf-v6.1/install.sh esp32
   phoneuse bridge flash
   ```
2. **Set it up** in the browser mirror: open Settings › Bluetooth bridge › Set Up
   Bridge. Check that the bridge's screen shows the same code as the page, then pick
   a 2.4 GHz Wi-Fi network and type its password. This step doesn't need the USB
   cable.
3. **Pair the iPhone** with the bridge: on the iPhone, open Settings › Bluetooth and
   tap **PhoneUse Bridge**.

`phoneuse bridge status` shows the bridge's address, which phone is connected to it,
and how long a message takes to reach the bridge and come back. When the phone
disconnects, the bridge reconnects to it by itself. [docs/BRIDGE.md](docs/BRIDGE.md)
describes the protocol.

## On Windows

Windows can't act as a Bluetooth Classic keyboard, so on Windows phoneuse is a
Bluetooth LE keyboard and mouse instead (HID over GATT, through the Windows
Runtime). It uses the PC's own Bluetooth adapter, needs no driver and no admin
rights, and Windows keeps using the adapter for everything else. The mirror window is
the browser mirror in a window of its own.

| | Tested with |
|---|---|
| Windows | Windows 11 |
| iPhone | iPhone 17 Pro Max and iPhone 14 Pro Max, iOS 26 |
| Python | 3.13, from python.org |
| UxPlay | 1.74, built in MSYS2 by the installer |
| Edge or Chrome | the mirror window |
| NVIDIA GPU (optional) | NVENC for the video hub, CUDA for the models that read the screen |

1. **Install**, from the checkout, in PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
   ```
   The installer makes `.venv`, installs MSYS2 to `C:\msys64` and builds UxPlay there,
   adds a Windows Firewall rule for UxPlay (it asks for admin rights once; the rule
   allows the local network only), puts `bin` on your PATH and downloads the models
   that read the screen (`-NoVision` skips them). It ends with `phoneuse doctor`,
   which checks each part and says how to fix what is missing.
2. **Start phoneuse:** run `phoneuse up`. The mirror window opens. Then, on the
   iPhone, open Control Center, tap Screen Mirroring and choose **PhoneUse**.
3. **Pair**, once: on the iPhone, open Settings › Bluetooth and tap this PC's Windows
   name, such as `DESKTOP-…` (Windows doesn't let phoneuse choose its Bluetooth LE
   name). Confirm on the phone, and on Windows if it asks.
4. **Turn on the pointer** as on Linux. The first time, phoneuse measures the phone's
   pointer: over Bluetooth LE every phone needs that measurement.

How it differs from Linux:

- In the mirror window, Ctrl works as Cmd: the Windows key belongs to the Start menu.
- The Bluetooth keyboard and mouse run as a background service that keeps running
  when `phoneuse up` stops, and the next `phoneuse up` uses it again. Each time the
  service starts, the iPhone sees its Bluetooth services disappear and come back, and
  one iPhone stopped using them until it was paired again. `phoneuse down` stops it.
- Pair only the phone that phoneuse should control. Over Bluetooth LE, iOS may give
  its name as just "iPhone", so when two paired phones are connected phoneuse can't
  tell which one is mirroring, and it sends nothing.
- A motherboard's Wi-Fi and Bluetooth card needs its antennas, even if the PC uses
  Ethernet. Without them the phone was heard at about -90 dBm next to the PC, the
  link dropped several times an hour, and keys and taps sent during a drop were lost.
- The phone takes a report every 15 ms over Bluetooth LE instead of every 8 ms, and
  the mirror trails the phone by about 0.1 s, so by hand it feels slower than on
  Linux. Agents don't notice.
- The bridge hasn't been tried on Windows.

Logs are in `%LOCALAPPDATA%\phoneuse\run`. For agents, `bin\phoneuse-mcp.exe` is the
MCP server: `claude mcp add --scope user phoneuse C:\path\to\phoneuse\bin\phoneuse-mcp.exe`.

## Using it

**The mirror window:** the phone's pointer follows your mouse. Click to tap, drag to
swipe, and use the scroll wheel to scroll. To swipe in from an edge of the screen,
press on the bezel and drag; dragging up from the bottom bezel goes to the home
screen. While the window has focus, your keyboard types on the phone, and the Super
key works as Cmd.

**The browser mirror:** http://localhost:8777 shows the same mirror, and only this
computer can open it. In its settings you can save the passcode, set up the bridge
and keep the phone's screen awake.

**Unlocking:** when an agent runs `phoneuse unlock`, phoneuse asks you first, with a
banner in the browser mirror and a desktop notification. It types the saved passcode
only after you approve.

**Agents:** each action is a subcommand, and `phoneuse --help` lists them all:

```sh
phoneuse open Settings
phoneuse text                 # print the screen as text, with positions
phoneuse tap-on "Wi-Fi"       # tap the element that matches the description
phoneuse screenshot shot.png
phoneuse done                 # hand the phone back to you
```

**MCP:** the MCP server offers the same actions. After each action, it returns the
screen once the screen has stopped changing.

```sh
scripts/setup-mcp.sh
claude mcp add --scope user phoneuse ~/phoneuse/bin/phoneuse-mcp
```

[skill/SKILL.md](skill/SKILL.md) is a skill for Claude Code. To use it, link it into
`~/.claude/skills/phoneuse`.

## Configuration

These settings are optional and go in `~/.config/phoneuse/config.toml`
(on Windows, `%APPDATA%\phoneuse\config.toml`):

```toml
name = "PhoneUse"             # the Bluetooth and AirPlay name; rerun phoneuse install after changing it
screen_points = "auto"        # the iPhone's screen size in points, such as [402, 874];
                              # auto: from the model of the phone that last mirrored
decoder = "auto"              # the GStreamer H.264 decoder
mirror_width = 498            # the mirror window's width
web_port = 8777               # the browser mirror's port
# Windows only:
encoder = "auto"              # the video hub's encoder: h264_nvenc, libx264 or h264_mf
uxplay = "auto"               # path to uxplay.exe (auto: C:\msys64\ucrt64\bin)
app_window = true             # phoneuse up opens the mirror in a window of its own
service_port = 47810          # the first of four local ports phoneuse's services use
```

## More

- [ARCHITECTURE.md](ARCHITECTURE.md) explains how the parts of phoneuse work together, and which iOS behaviours phoneuse depends on.
- [docs/RECIPES.md](docs/RECIPES.md) lists which gestures work on iOS, and why.
- [docs/BRIDGE.md](docs/BRIDGE.md) covers the ESP32 bridge's setup and protocol.

## License

MIT
