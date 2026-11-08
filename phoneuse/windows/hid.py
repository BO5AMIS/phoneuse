"""Bluetooth LE mouse + keyboard for an iPhone, on Windows: the PC's own Bluetooth
adapter publishes a HID-over-GATT service (HOGP) through the Windows Runtime, and the
iPhone connects to it like to any Bluetooth LE keyboard. No driver, no admin rights,
and Windows keeps using the adapter for everything else (headphones and so on).

The report descriptor is the Linux daemon's (hidproto.py): keyboard, relative mouse,
absolute pointer and consumer keys, one GATT Report characteristic each. The command
protocol is the Linux daemon's too, so everything above it is shared; see
hid_daemon.py for the commands. Differences that come from Bluetooth LE:

  * The iPhone is the one that connects; this side only advertises. `connect` waits
    a few seconds for the iPhone to come back instead of paging it.
  * Pairing happens from the iPhone: Settings > Bluetooth > this PC's name. Windows
    may ask to confirm the pairing in a notification.
  * Notifications go out once per connection event. phoneuse asks Windows for the
    throughput-optimized connection parameters (iOS settles on 15 ms for HID).

Run by `phoneuse up` (python -m phoneuse.windows.hid). Local clients connect through
ipc.py (127.0.0.1 + token).
"""
import asyncio
import os
import sys
import time
import unicodedata
import uuid

from winrt.windows.devices.bluetooth import (
    BluetoothAdapter, BluetoothConnectionStatus, BluetoothError, BluetoothLEDevice,
    BluetoothLEPreferredConnectionParameters,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties, GattLocalCharacteristicParameters, GattLocalDescriptorParameters,
    GattProtectionLevel, GattServiceProvider, GattServiceProviderAdvertisingParameters,
    GattServiceProviderAdvertisementStatus, GattSessionStatus, GattWriteOption,
)
from winrt.windows.devices.radios import RadioState
from winrt.windows.storage.streams import DataWriter

from .. import ipc
from ..config import runtime_path
from ..hidproto import (
    BUTTONS, CHARS, CONSUMER_KEYS, REPORT_MAP, REPORT_SIZES, AbsoluteNudge, absolute, key_code,
)
from . import precise_timers

CONNECT_WAIT = 8.0  # `connect`: how long to wait for the iPhone to reconnect by itself (s)
# A report waits this long for a dropped link to come back (s). iOS drops and restores
# the link by itself now and then (measured: bursts of several drops a minute, each
# back within 0.1-3 s), and an agent's tap shouldn't fail because it landed in one.
RECONNECT_WAIT = 5.0
STARTED = time.time()  # `status` reports it: phoneuse up warns when hid.py is newer
# Least ms between reports; None: one connection interval (see BleHid.gap). For tuning.
PACING_MS = float(os.environ["PHONEUSE_PACING_MS"]) if os.environ.get("PHONEUSE_PACING_MS") else None


def log(*a):
    print(time.strftime("%H:%M:%S"), "hid:", *a, flush=True)


def u16(n):
    return uuid.UUID(f"0000{n:04x}-0000-1000-8000-00805f9b34fb")


def ibuffer(data):
    w = DataWriter()
    w.write_bytes(bytes(data))
    return w.detach_buffer()


def same_name(a, b):
    n = lambda x: unicodedata.normalize("NFKC", x or "").replace("’", "'").casefold().strip()
    return bool(a and b) and n(a) == n(b)


def last_mirroring_phone():
    """The phone that last started screen mirroring (cli.follow_sender writes it), so a
    restarted service still knows it."""
    try:
        with open(runtime_path("mirroring-phone"), encoding="utf-8-sig") as f:
            return f.read().strip() or None
    except OSError:
        return None


HID_SERVICE, BATTERY_SERVICE = 0x1812, 0x180F
HID_INFORMATION, REPORT_MAP_CHAR, HID_CONTROL_POINT, REPORT = 0x2A4A, 0x2A4B, 0x2A4C, 0x2A4D
BATTERY_LEVEL, REPORT_REFERENCE = 0x2A19, 0x2908
ENCRYPTED = GattProtectionLevel.ENCRYPTION_REQUIRED


def advertising_parameters():
    # Both discoverable: a connectable but undiscoverable service is aborted by Windows
    # (measured: it stays unpublished). Discoverable puts the UUIDs in the advertisement;
    # the HID one is what makes the iPhone list this PC as a keyboard.
    p = GattServiceProviderAdvertisingParameters()
    p.is_connectable = True
    p.is_discoverable = True
    return p


def advertising(provider):
    return provider.advertisement_status in (GattServiceProviderAdvertisementStatus.STARTED,
                                             GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA)


class Client:
    """One iPhone that has connected (a GATT session subscribed to our input reports).
    Kept across its reconnects, so its event handlers are registered once."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.name = None
        self.device = None  # BluetoothLEDevice, kept so its connection-parameter request stays in force
        self.params_request = None
        self.interval_ms = None
        self.session = None  # the GattSession whose status we follow, and our handler's token
        self.session_token = None
        self.tuning = False  # _tune is running for it


class BleHid:
    def __init__(self, loop):
        self.loop = loop
        self.adapter = None
        self.radio_on = None
        self.providers = []
        self.reports = {}  # report id -> GattLocalCharacteristic
        self.battery = None
        self.clients = {}  # device id -> Client (subscribed, session active)
        self.known = {}  # device id -> Client, every iPhone seen since the service started
        self.mirror_name = last_mirroring_phone()
        self.lock = asyncio.Lock()  # one notification at a time, in order
        self.buttons = 0
        self.consumer_bits = 0
        self.nudge = AbsoluteNudge()
        self.last_sent = 0.0  # when the last report went out (perf_counter)
        self.ready = asyncio.Event()  # the service is published
        self.connected_event = asyncio.Event()
        self.stats = {"sent": 0, "failed": 0, "waited": 0, "total_ms": 0.0, "max_ms": 0.0}
        self.problem = None  # why the service isn't published, for `status`
        self.generation = 0  # counts publishes, so events from old providers are ignored
        self.readvertising = False

    # ---------- publishing ----------
    async def start(self):
        """Publish the HID and battery services and advertise, retrying until the
        adapter allows it (Bluetooth may be off)."""
        while True:
            try:
                await self._publish()
                self.problem = None
                self.ready.set()
                return
            except Exception as e:
                self.problem = str(e)
                self._teardown()  # nothing half-published stays behind for the next try
                log("can't publish yet:", e, "(retrying in 5 s)")
                await asyncio.sleep(5)

    def _quit(self):
        self._teardown()
        os._exit(0)

    def _teardown(self):
        self.generation += 1  # events from the old providers are ignored from now on
        for _, provider in self.providers:
            try:
                provider.stop_advertising()
            except OSError:
                pass
        self.providers, self.reports, self.clients = [], {}, {}
        self.connected_event.clear()

    async def _publish(self):
        self.adapter = await BluetoothAdapter.get_default_async()
        if self.adapter is None:
            raise RuntimeError("no Bluetooth adapter")
        if not self.adapter.is_low_energy_supported or not self.adapter.is_peripheral_role_supported:
            raise RuntimeError("this Bluetooth adapter can't act as a Bluetooth LE peripheral")
        radio = await self.adapter.get_radio_async()
        self.radio_on = radio is None or radio.state == RadioState.ON
        if not self.radio_on:
            raise RuntimeError("Bluetooth is off (turn it on in Windows settings)")

        self.generation += 1
        generation = self.generation
        hid = await self._provider(HID_SERVICE)
        self.providers = [("HID", hid)]
        s = hid.service
        await self._char(s, HID_INFORMATION, GattCharacteristicProperties.READ,
                         value=bytes([0x11, 0x01, 0x00, 0x02]))  # HID 1.11, no country, normally connectable
        await self._char(s, REPORT_MAP_CHAR, GattCharacteristicProperties.READ, value=REPORT_MAP)
        cp = await self._char(s, HID_CONTROL_POINT, GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE, encrypted=False)
        cp.add_write_requested(self._on_write)
        for rid, size in REPORT_SIZES.items():
            c = await self._char(s, REPORT, GattCharacteristicProperties.READ | GattCharacteristicProperties.NOTIFY,
                                 value=bytes(size))
            d = GattLocalDescriptorParameters()
            d.static_value = ibuffer([rid, 1])  # report id, input report
            d.read_protection_level = ENCRYPTED
            r = await c.create_descriptor_async(u16(REPORT_REFERENCE), d)
            if r.error != BluetoothError.SUCCESS:
                raise RuntimeError(f"report reference: {BluetoothError(r.error).name}")
            c.add_subscribed_clients_changed(self._on_subscribers)
            self.reports[rid] = c

        bat = await self._provider(BATTERY_SERVICE)
        self.providers.append(("battery", bat))
        self.battery = await self._char(bat.service, BATTERY_LEVEL,
                                        GattCharacteristicProperties.READ | GattCharacteristicProperties.NOTIFY,
                                        value=bytes([100]), encrypted=False)

        for _, provider in self.providers:
            provider.start_advertising(advertising_parameters())
        await asyncio.sleep(1.0)  # Windows reports ABORTED on the way up now and then, then starts
        for name, provider in self.providers:
            if not advertising(provider):
                raise RuntimeError(f"Windows didn't start advertising the {name} service "
                                   f"({GattServiceProviderAdvertisementStatus(provider.advertisement_status).name})")
            provider.add_advertisement_status_changed(
                lambda p, a, name=name: self.loop.call_soon_threadsafe(
                    self._on_advertising, generation, name, p, a.status, a.error))
        log(f"advertising as a Bluetooth keyboard and mouse ({self.local_name()}); "
            "pair from the iPhone: Settings > Bluetooth")
        self._refresh_clients()

    async def _provider(self, service):
        r = await GattServiceProvider.create_async(u16(service))
        if r.error != BluetoothError.SUCCESS:
            raise RuntimeError(f"service {service:#06x}: {BluetoothError(r.error).name}")
        return r.service_provider

    async def _char(self, service, uuid16, props, value=None, encrypted=True):
        p = GattLocalCharacteristicParameters()
        p.characteristic_properties = props
        if encrypted:  # reading the HID characteristics makes the iPhone pair (bond) first
            p.read_protection_level = ENCRYPTED
            p.write_protection_level = ENCRYPTED
        if value is not None:
            p.static_value = ibuffer(value)
        r = await service.create_characteristic_async(u16(uuid16), p)
        if r.error != BluetoothError.SUCCESS:
            raise RuntimeError(f"characteristic {uuid16:#06x}: {BluetoothError(r.error).name}")
        return r.characteristic

    def local_name(self):
        return os.environ.get("COMPUTERNAME", "this PC")

    def _on_advertising(self, generation, name, provider, status, error):
        """Advertising stopped after it had started. Some adapters pause it while a
        phone is connected, so first just advertise again, keeping the connected phone;
        only if that doesn't take (Bluetooth was off, the adapter reset) publish
        everything again."""
        if generation != self.generation or advertising(provider) or self.readvertising:
            return
        log(f"advertising the {name} service stopped: {GattServiceProviderAdvertisementStatus(status).name} "
            f"({BluetoothError(error).name})")
        self.readvertising = True
        asyncio.ensure_future(self._readvertise(generation, name, provider))

    async def _readvertise(self, generation, name, provider):
        try:
            await asyncio.sleep(2)
            if generation != self.generation or advertising(provider):
                return
            try:
                provider.start_advertising(advertising_parameters())
                await asyncio.sleep(1.5)
            except OSError:
                pass
            if advertising(provider):
                log(f"advertising the {name} service again")
                return
            log("advertising won't start again; publishing the services anew")
            self.ready.clear()
            self._teardown()
            await asyncio.sleep(2)
            await self.start()
        finally:
            self.readvertising = False

    def _on_write(self, characteristic, args):
        # HID Control Point (suspend / exit suspend): nothing to do, but take the write.
        deferral = args.get_deferral()

        async def take():
            try:
                req = await args.get_request_async()
                if req is not None and req.option == GattWriteOption.WRITE_WITH_RESPONSE:
                    req.respond()
            finally:
                deferral.complete()
        asyncio.run_coroutine_threadsafe(take(), self.loop)

    # ---------- connected iPhones ----------
    def _on_subscribers(self, characteristic, _args):
        self.loop.call_soon_threadsafe(self._refresh_clients)

    async def watch_clients(self):
        """Look at the subscribers every second as well: Windows doesn't always raise an
        event when a phone comes back (a reconnecting phone can reuse its old session)."""
        while True:
            await asyncio.sleep(1.0)
            if self.ready.is_set():
                try:
                    self._refresh_clients()
                except Exception as e:  # never let a Windows hiccup end the service
                    log("checking connected iPhones failed:", e)

    def _refresh_clients(self):
        seen, closed = {}, set()
        for c in self.reports.values():
            for sub in c.subscribed_clients:
                try:
                    session = sub.session
                    if session.session_status != GattSessionStatus.ACTIVE:
                        closed.add(session.device_id.id)
                        continue
                    seen[session.device_id.id] = session
                except OSError:
                    continue
        for dev_id in list(self.clients):
            if dev_id not in seen:
                cl = self.clients.pop(dev_id)
                why = "link closed" if dev_id in closed else "stopped listening for reports"
                log(f"disconnected: {cl.name or dev_id} ({why})")
        for dev_id, session in seen.items():
            if dev_id in self.clients:
                continue
            cl = self.clients[dev_id] = self.known.setdefault(dev_id, Client(dev_id))
            self._follow_session(cl, session)
            if cl.device is None:
                if not cl.tuning:
                    cl.tuning = True
                    asyncio.ensure_future(self._tune(cl))
            else:
                self._ask_fast(cl)
                log(f"connected again: {cl.name} (interval {cl.interval_ms} ms)")
        if self.clients:
            self.connected_event.set()
        else:
            self.connected_event.clear()

    def _follow_session(self, cl, session):
        """Refresh as soon as this session opens or closes (one handler per phone)."""
        if cl.session is not None:
            try:
                cl.session.remove_session_status_changed(cl.session_token)
            except OSError:
                pass
        cl.session = session
        cl.session_token = session.add_session_status_changed(
            lambda s, a: self.loop.call_soon_threadsafe(self._refresh_clients))

    async def _tune(self, cl):
        """Learn the iPhone's name, ask for short connection intervals and follow its
        link. Once per phone; a reconnect reuses all of it."""
        try:
            device = await BluetoothLEDevice.from_id_async(cl.device_id)
            if device is None:
                return
            cl.device, cl.name = device, device.name
            self._ask_fast(cl)
            self._read_interval(cl)
            device.add_connection_parameters_changed(
                lambda d, a: self.loop.call_soon_threadsafe(self._read_interval, cl))
            device.add_connection_status_changed(
                lambda d, a: self.loop.call_soon_threadsafe(self._on_link, cl))
            log(f"connected: {cl.name} (interval {cl.interval_ms} ms)")
        except Exception as e:
            log("connected:", cl.device_id, f"(details unavailable: {e})")
        finally:
            cl.tuning = False

    def _ask_fast(self, cl):
        """Ask for the shortest connection interval: iOS otherwise often stays at 30 ms,
        which caps the pointer at ~33 reports/s. Asked again on every reconnect."""
        if cl.params_request is not None:
            try:
                cl.params_request.close()
            except (AttributeError, OSError):
                pass
        try:
            cl.params_request = cl.device.request_preferred_connection_parameters(
                BluetoothLEPreferredConnectionParameters.throughput_optimized)
        except (AttributeError, OSError) as e:
            cl.params_request = None
            log("connection parameters: can't ask for faster ones:", e)

    def _on_link(self, cl):
        try:
            up = cl.device.connection_status == BluetoothConnectionStatus.CONNECTED
        except OSError:
            return
        log(f"{cl.name}: Bluetooth link {'up' if up else 'down'}")
        self._refresh_clients()

    def _read_interval(self, cl):
        try:
            p = cl.device.get_connection_parameters()
            new = round(p.connection_interval * 1.25, 2)
            if not new:  # the link went down; _on_link says so
                return
            if new != cl.interval_ms and cl.interval_ms is not None:
                log(f"{cl.name}: connection interval {cl.interval_ms} -> {new} ms")
            cl.interval_ms = new
        except (AttributeError, OSError):
            pass

    def targets(self):
        """The iPhone that gets our reports: the only one connected, or the one whose
        name is the mirroring phone's. Never several: a second paired iPhone in range
        (the owner's own, next to a test phone) must not get an agent's taps. Over
        Bluetooth LE, iOS often gives its name as just "iPhone" while AirPlay has the
        full name, so two connected phones usually can't be told apart: refuse then."""
        clients = list(self.clients.values())
        if len(clients) <= 1:
            return clients
        if self.mirror_name:
            mirroring = [c for c in clients if same_name(c.name, self.mirror_name)]
            if len(mirroring) == 1:
                return mirroring
        names = ", ".join(repr(c.name or c.device_id) for c in clients)
        raise RuntimeError(f"{len(clients)} iPhones are connected over Bluetooth ({names}) and PhoneUse "
                           f"can't tell which one is mirroring, so nothing was sent. On the phone that "
                           f"shouldn't be controlled: Settings > Bluetooth > {self.local_name()} > "
                           "Forget This Device")

    def subscribed(self, characteristic, cl):
        for sub in characteristic.subscribed_clients:
            try:
                if sub.session.device_id.id == cl.device_id:
                    return sub
            except OSError:
                continue
        return None

    # ---------- reports ----------
    def gap(self):
        """Least time between two reports: one connection interval. Bluetooth LE sends
        notifications at connection events, and two reports in one event reach iOS a
        fraction of a millisecond apart; iOS drops pointer motion that comes faster
        than ~8 ms apart (measured over Classic), so a drag would lose distance."""
        if PACING_MS is not None:
            return PACING_MS / 1000
        intervals = [c.interval_ms for c in self.clients.values() if c.interval_ms]
        return max(intervals or [15.0]) / 1000

    async def send(self, report_id, payload):
        for extra in self.nudge.before(report_id, payload):
            await self._send_through_drops(report_id, extra)
        await self._send_through_drops(report_id, payload)

    async def _send_through_drops(self, report_id, payload):
        """Send a report; when the link is down, or drops as it goes out, wait for the
        iPhone to come back and send it again, for up to RECONNECT_WAIT. Reports carry
        the whole state (buttons, keys held, an absolute position), so a repeat is
        harmless; motion in a lost relative report is lost, as on any dropped link."""
        t0 = time.perf_counter()
        deadline = t0 + RECONNECT_WAIT
        waited = False
        while True:
            if not self.clients:
                waited = True
                try:
                    await asyncio.wait_for(self.connected_event.wait(), max(0.0, deadline - time.perf_counter()))
                except asyncio.TimeoutError:
                    raise RuntimeError("no iPhone connected over Bluetooth") from None
            try:
                await self._send(report_id, payload)
                break
            except ConnectionError:
                if time.perf_counter() >= deadline:
                    raise RuntimeError("the iPhone didn't take the report (connection lost?)") from None
                waited = True
                await asyncio.sleep(0.25)  # Windows marks the session closed a moment later
                self._refresh_clients()
        if waited:
            self.stats["waited"] += 1
            log(f"a report waited {time.perf_counter() - t0:.1f} s for the iPhone's link to come back")

    async def _send(self, report_id, payload):
        c = self.reports[report_id]
        buf = ibuffer(payload)
        async with self.lock:
            wait = self.last_sent + self.gap() - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            t0 = self.last_sent = time.perf_counter()
            targets = self.targets()
            ok = False
            for cl in targets:
                sub = self.subscribed(c, cl)
                if sub is None:
                    continue
                try:
                    r = await c.notify_value_async(buf, sub)
                    ok = ok or r.status == 0  # GattCommunicationStatus.SUCCESS
                except OSError as e:
                    log("notify failed:", e)
            ms = (time.perf_counter() - t0) * 1000
            st = self.stats
            st["sent" if ok else "failed"] += 1
            st["total_ms"] += ms
            st["max_ms"] = max(st["max_ms"], ms)
        if not ok:
            raise ConnectionError("the iPhone didn't take the report")

    async def mouse(self, dx=0, dy=0, wheel=0):
        b = lambda v: v & 0xFF
        await self.send(2, [self.buttons, b(dx), b(dy), b(wheel)])

    async def move(self, dx, dy, step=8, delay=0.008):
        while dx or dy:
            sx = max(-step, min(step, dx))
            sy = max(-step, min(step, dy))
            await self.mouse(sx, sy)
            dx -= sx
            dy -= sy
            await asyncio.sleep(delay)

    async def consumer_set(self, name, down):
        bit = 1 << CONSUMER_KEYS[name] if name else 0xFF
        self.consumer_bits = self.consumer_bits | bit if down else self.consumer_bits & ~bit
        await self.send(4, [self.consumer_bits])

    async def consumer(self, name, hold=0.05):
        await self.consumer_set(name, True)
        await asyncio.sleep(hold)
        await self.consumer_set(name, False)

    async def key(self, code, mods=0):
        await self.send(1, [mods, 0, code, 0, 0, 0, 0, 0])
        await asyncio.sleep(0.012)
        await self.send(1, [0] * 8)
        await asyncio.sleep(0.012)

    # ---------- commands (same protocol as hid_daemon.py) ----------
    def status(self):
        names = [c.name or c.device_id for c in self.clients.values()]
        intervals = [c.interval_ms for c in self.clients.values() if c.interval_ms]
        st = self.stats
        avg = st["total_ms"] / max(1, st["sent"] + st["failed"])
        return (f"connected={bool(self.clients)} released=False peer={names[0] if names else None!r} "
                f"connected_phones={names} mirror={self.mirror_name!r} link=ble name={self.local_name()!r} "
                f"interval_ms={intervals[0] if intervals else None} published={self.ready.is_set()} "
                f"problem={self.problem!r} notify_avg_ms={avg:.1f} notify_max_ms={st['max_ms']:.1f} "
                f"waited_for_link={st['waited']} started={STARTED:.0f}")

    async def run(self, line):
        parts = line.strip().split(" ", 1)
        cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        args = rest.split()
        if cmd == "status":
            return "ok " + self.status()
        if cmd == "connect":
            if self.clients:
                return "ok already connected"
            try:
                await asyncio.wait_for(self.connected_event.wait(), CONNECT_WAIT)
                return "ok connected to " + ", ".join(c.name or c.device_id for c in self.clients.values())
            except asyncio.TimeoutError:
                raise RuntimeError(f"no iPhone connected. On the iPhone: Settings > Bluetooth, tap "
                                   f"{self.local_name()} (under My Devices once paired)")
        if cmd == "mirror":
            self.mirror_name = rest.strip() or None
            log("mirroring phone:", self.mirror_name)
            return f"ok mirror={self.mirror_name!r} connected={bool(self.clients)}"
        if cmd == "shutdown":  # `phoneuse down`
            log("stopping: the Bluetooth keyboard and mouse go away until phoneuse up starts them again")
            self.loop.call_later(0.3, self._quit)
            return "ok stopping"
        if cmd in ("release", "pair"):
            return f"ok {cmd} has no effect over Bluetooth LE; always discoverable as {self.local_name()!r}"
        if cmd == "eject":
            await self.consumer("eject")
            return "ok"
        if cmd == "consumer":
            if args == ["up"]:
                await self.consumer_set(None, False)
                return "ok"
            if not args or args[0] not in CONSUMER_KEYS:
                return "err consumer keys: " + ", ".join(CONSUMER_KEYS)
            how = args[1] if len(args) > 1 else "0.05"
            if how in ("down", "up"):
                await self.consumer_set(args[0], how == "down")
            else:
                await self.consumer(args[0], float(how))
            return "ok"
        if cmd == "a":
            await self.send(3, absolute(args[0], args[1], int(args[2]) if len(args) > 2 else 0))
            return "ok"
        if cmd == "m":
            dx, dy = (max(-127, min(127, int(v))) for v in args[:2])
            wheel = max(-127, min(127, int(args[2]))) if len(args) > 2 else 0
            if len(args) > 3:
                self.buttons = int(args[3]) & 0x07
            await self.mouse(dx, dy, wheel)
            return "ok"
        if cmd == "k":
            codes = [int(c) & 0xFF for c in args[1:7]]
            await self.send(1, [int(args[0]) & 0xFF, 0] + codes + [0] * (6 - len(codes)))
            return "ok"
        if cmd == "move":
            await self.move(int(args[0]), int(args[1]))
        elif cmd == "corner":
            for _ in range(60):
                await self.mouse(-127, -127)
                await asyncio.sleep(0.008)
        elif cmd == "click":
            self.buttons |= BUTTONS[args[0] if args else "left"]
            await self.mouse()
            await asyncio.sleep(0.05)
            self.buttons = 0
            await self.mouse()
        elif cmd == "down":
            self.buttons |= BUTTONS[args[0] if args else "left"]
            await self.mouse()
        elif cmd == "up":
            self.buttons = 0
            await self.mouse()
        elif cmd == "scroll":
            n = int(args[0])
            for _ in range(abs(n)):
                await self.mouse(wheel=1 if n > 0 else -1)
                await asyncio.sleep(0.02)
        elif cmd == "key":
            await self.key(*key_code(args[0], args[1:]))
        elif cmd == "type":
            for ch in rest:
                if ch not in CHARS:
                    raise ValueError(f"cannot type {ch!r}")
            for ch in rest:
                await self.key(*CHARS[ch])
        else:
            return f"err unknown command {cmd!r}"
        return "ok"


async def serve(hid, sock):
    async def client(reader, writer):
        try:
            line = await reader.readline()
            if line.strip() != f"auth {ipc.token()}".encode():
                return
            while line := await reader.readline():
                text = line.decode(errors="replace")
                if not text.strip():
                    continue
                try:
                    reply = await hid.run(text)
                except Exception as e:
                    reply = f"err {e}"
                writer.write((reply.replace("\n", " ") + "\n").encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(client, sock=sock)
    log("commands on", ipc.describe("hid"))
    async with server:
        await server.serve_forever()


async def amain():
    precise_timers()
    # The command port first: a second copy must exit before it touches Bluetooth.
    # Publishing the GATT services again, even briefly, changes what a paired iPhone
    # sees, and an iPhone 14 Pro Max then stopped using the keyboard until it was
    # paired again (measured: Bluetooth off/on on either side didn't bring it back).
    try:
        sock = ipc.listen("hid")
    except OSError as e:
        log(f"another copy of the HID service is running ({e}); leaving Bluetooth to it")
        return
    hid = BleHid(asyncio.get_running_loop())
    await asyncio.gather(serve(hid, sock), hid.start(), hid.watch_clients())


def main():
    if sys.platform != "win32":
        sys.exit("phoneuse.windows.hid runs on Windows; on Linux the HID service is hid_daemon.py")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
