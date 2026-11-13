"""MusicBackend that remote-controls the phone's music over Bluetooth.

- iPhone: Apple Media Service over BLE (needs no app on the phone).
- Android: AVRCP, with the comma as controller only. The comma never accepts Bluetooth
  audio (A2DP), so the music keeps playing through the car. If a phone connects audio
  anyway (e.g. the PC's audio stack accepted it), it is disconnected again.

All D-Bus traffic runs on a worker thread; the UI only reads a snapshot."""
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from jeepney import Message

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.apps.music.backend import PlaybackState
from openpilot.selfdrive.ui.apps.music.bluetooth import ams
from openpilot.selfdrive.ui.apps.music.bluetooth.bluez import (ADAPTER, ADV_MANAGER, AGENT_MANAGER, DEVICE, GATT_CHAR, GATT_SERVICE,
                                                                MEDIA_PLAYER, MEDIA_TRANSPORT, Bus, BluezError, ManagedObjects,
                                                                children, find_adapter, interfaces_rule, msg_member, msg_path,
                                                                properties_changed_rule, props)
from openpilot.selfdrive.ui.apps.music.bluetooth.pairing import A2DP_SOURCE_UUID, AVRCP_TARGET_UUID, CAPABILITY, PairingWindow
from openpilot.selfdrive.ui.apps.music.bluetooth.sources import AmsSource, AvrcpSource, Source

SCAN_INTERVAL = 3.0  # s between full object scans (signals trigger scans sooner)
RECONNECT_INTERVAL = 5.0  # s between attempts to reach BlueZ
PROFILE_RETRY = 10.0  # s between AVRCP connect / A2DP disconnect attempts per device

NO_BLUETOOTH = "Bluetooth isn't available"
BLOCKED = "Bluetooth is blocked on this device"
OFF = "Bluetooth is off"
NO_PHONE = "Pair your phone to control its music"
WAITING = "Waiting for your phone to connect"
PAIRING = "On your phone, open Bluetooth and pick \"{name}\""
CONFIRM = "Confirm code {code:06d} on your phone"
NOTHING_PLAYING = "Play something on your phone"

ACTION_TURN_ON = "Turn on Bluetooth"
ACTION_PAIR = "Pair phone"
ACTION_CANCEL = "Cancel"

Event = Message | tuple[str, str, float | None]


class BluetoothMusicBackend:
  def __init__(self, bus_factory: Callable[[], Any] = Bus, clock: Callable[[], float] = time.monotonic,
               start_thread: bool = True):
    self._bus_factory = bus_factory
    self._clock = clock
    self._start_thread = start_thread
    self._thread: threading.Thread | None = None
    self._events: queue.Queue[Event] = queue.Queue()
    self._lock = threading.Lock()

    self._bus: Any = None
    self._next_connect = 0.0
    self._adapter: str | None = None
    self._powered = False
    self._paired: set[str] = set()
    self._source: Source | None = None
    self._status, self._action = "", ""
    self._dirty = True
    self._next_scan = 0.0
    self._profile_attempts: dict[tuple[str, str], float] = {}

    self.pairing = PairingWindow(clock, lambda dev: dev in self._paired)
    self._pairing_open = False  # worker's view, to notice the window timing out
    self._adv_registered: bool | None = None  # None: not registered, else its Discoverable flag
    self._agent_path = ""
    self._adv_path = ""

  # ***** MusicBackend (UI thread) *****

  def state(self) -> PlaybackState:
    self._ensure_thread()
    with self._lock:
      st = self._source.snapshot() if self._source is not None else PlaybackState()
      status, action = self._status, self._action
    if st.available:
      return st
    if self.pairing.active:
      code = self.pairing.passkey
      status = CONFIRM.format(code=code) if code is not None else PAIRING.format(name=self.pairing.local_name)
      action = ACTION_CANCEL
      st = replace(st, title="Pairing")
    return replace(st, status=status, action=action)

  def play_pause(self) -> None:
    with self._lock:
      if self._source is None or not self._source.snapshot().available:
        return
      cmd = "pause" if self._source.snapshot().is_playing else "play"
      self._source.optimistic(cmd)
    self._events.put(("cmd", cmd, None))

  def next(self) -> None:
    self._events.put(("cmd", "next", None))

  def previous(self) -> None:
    self._events.put(("cmd", "previous", None))

  def set_volume(self, volume: float) -> None:
    self._events.put(("cmd", "volume", max(0.0, min(1.0, volume))))

  def run_action(self) -> None:
    self._events.put(("action", "", None))

  # ***** worker *****

  def _ensure_thread(self) -> None:
    if self._start_thread and self._thread is None:
      self._thread = threading.Thread(target=self._run, name="bt-music", daemon=True)
      self._thread.start()

  def _run(self) -> None:
    while True:
      try:
        item: Event | None = self._events.get(timeout=0.5)
      except queue.Empty:
        item = None
      self.step(item)

  def step(self, item: Event | None = None) -> None:
    """One worker iteration. Public for tests."""
    if self._bus is None and not self._connect():
      return
    try:
      if isinstance(item, tuple):
        self._handle_request(*item)
      elif item is not None:
        self._handle_signal(item)
      if self._pairing_open and not self.pairing.active:
        self._end_pairing()  # timed out
      if self._dirty or self._clock() >= self._next_scan:
        self._scan()
    except (BluezError, TimeoutError) as e:
      cloudlog.warning(f"bt music: {e}")
      self._dirty = True
    except Exception:
      cloudlog.exception("bt music: lost BlueZ")
      self._drop_bus()

  def _connect(self) -> bool:
    now = self._clock()
    if now < self._next_connect:
      return False
    self._next_connect = now + RECONNECT_INTERVAL
    try:
      bus = self._bus_factory()
      bus.subscribe(properties_changed_rule(), self._events)
      bus.subscribe(interfaces_rule(), self._events)
    except Exception as e:
      cloudlog.debug(f"bt music: no bus: {e}")
      self._set_status(NO_BLUETOOTH, "")
      return False
    self._bus = bus
    self._agent_path, self._adv_path = bus.object_path("agent"), bus.object_path("advertisement")
    bus.export(self._agent_path, self.pairing.agent)
    bus.export(self._adv_path, self.pairing.advertisement(self._adv_path))
    self._dirty = True
    return True

  def _drop_bus(self) -> None:
    self._set_source(None)
    self.pairing.close()
    self._pairing_open = False
    self._adv_registered = None
    try:
      self._bus.close()
    except Exception:
      pass
    self._bus = None
    self._set_status(NO_BLUETOOTH, "")

  def _handle_request(self, kind: str, name: str, arg: float | None) -> None:
    if kind == "cmd":
      with self._lock:
        source = self._source
      if source is not None:
        source.send(name, arg)
    elif kind == "action":
      if self._pairing_open:
        self._end_pairing()
      elif self._adapter is not None and not self._powered:
        self._bus.set_property(self._adapter, ADAPTER, "Powered", "b", True)
      elif self._adapter is not None:
        self._start_pairing()
      self._dirty = True

  def _handle_signal(self, msg: Message) -> None:
    member = msg_member(msg)
    if member != "PropertiesChanged":
      self._dirty = True  # InterfacesAdded / InterfacesRemoved: a device, player or GATT service came or went
      return
    path = msg_path(msg)
    interface, changed = msg.body[0], props(msg.body[1])
    with self._lock:
      if self._source is not None:
        self._source.handle_changed(path, interface, changed)
    if interface in (ADAPTER, DEVICE, MEDIA_PLAYER, MEDIA_TRANSPORT):
      self._dirty = True
    if interface == DEVICE and changed.get("Paired") and self._pairing_open:
      self._bus.set_property(path, DEVICE, "Trusted", "b", True)  # reconnects need no prompt
      self._end_pairing()

  # ***** pairing *****

  def _start_pairing(self) -> None:
    bus, adapter = self._bus, self._adapter
    bus.call("/org/bluez", AGENT_MANAGER, "RegisterAgent", "os", (self._agent_path, CAPABILITY))
    bus.call("/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o", (self._agent_path,))
    self.pairing.open()
    self._pairing_open = True
    bus.set_property(adapter, ADAPTER, "Pairable", "b", True)
    bus.set_property(adapter, ADAPTER, "DiscoverableTimeout", "u", 0)  # we close it ourselves
    bus.set_property(adapter, ADAPTER, "Discoverable", "b", True)  # classic Bluetooth, for Android

  def _end_pairing(self) -> None:
    self.pairing.close()
    self._pairing_open = False
    self._dirty = True
    for call in (lambda: self._bus.set_property(self._adapter, ADAPTER, "Discoverable", "b", False),
                 lambda: self._bus.call("/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o", (self._agent_path,))):
      try:
        call()
      except BluezError:
        pass

  # ***** scanning *****

  def _scan(self) -> None:
    self._dirty = False
    self._next_scan = self._clock() + SCAN_INTERVAL
    objs = self._bus.managed_objects()
    self._adapter = find_adapter(objs)
    if self._adapter is None:
      self._set_source(None)
      self._set_status(NO_BLUETOOTH, "")
      return
    adapter = objs[self._adapter][ADAPTER]
    self.pairing.local_name = adapter.get("Alias") or "comma"
    self._powered = bool(adapter.get("Powered"))
    if adapter.get("PowerState") == "off-blocked":
      self._set_source(None)
      self._set_status(BLOCKED, "")
      return
    if not self._powered:
      self._set_source(None)
      self._set_status(OFF, ACTION_TURN_ON)
      return

    devices = children(objs, self._adapter, DEVICE)
    self._paired = {d for d, p in devices.items() if p.get("Paired")}
    connected = {d: p for d, p in devices.items() if p.get("Connected") and p.get("Paired")}
    self._keep_audio_off(objs, connected)

    candidates = self._candidates(objs, connected)
    with self._lock:
      current = self._source
    if current is None or current.path not in candidates:
      self._set_source(None)
      make = next(iter(candidates.values()), None)
      if make is not None:
        source = make()
        source.start()
        self._set_source(source)
    if not candidates:
      self._connect_avrcp(objs, connected)

    with self._lock:
      has_source = self._source is not None
    if has_source:
      self._set_status(NOTHING_PLAYING, "")
    else:
      self._set_status(WAITING if self._paired else NO_PHONE, ACTION_PAIR)
    self._update_advertisement(want=self._pairing_open or not has_source)

  def _candidates(self, objs: ManagedObjects, connected: dict[str, dict]) -> dict[str, Callable[[], Source]]:
    out: dict[str, Callable[[], Source]] = {}
    for dev, p in sorted(connected.items()):
      name = p.get("Alias") or p.get("Name") or "Phone"
      players = children(objs, dev, MEDIA_PLAYER)
      # prefer the player that's actually playing
      for path in sorted(players, key=lambda pp: players[pp].get("Status") != "playing"):
        out[path] = lambda path=path, name=name: AvrcpSource(self._bus, path, name, self._clock)
      if p.get("ServicesResolved"):
        for svc, s in children(objs, dev, GATT_SERVICE).items():
          if str(s.get("UUID", "")).lower() != ams.SERVICE_UUID:
            continue
          chars = {str(c.get("UUID", "")).lower(): cp for cp, c in children(objs, svc, GATT_CHAR).items()}
          if ams.REMOTE_COMMAND_UUID in chars and ams.ENTITY_UPDATE_UUID in chars:
            out[svc] = lambda svc=svc, chars=chars, name=name: AmsSource(self._bus, svc, chars, name, self._clock)
    return out

  def _try_profile(self, dev: str, method: str, uuid: str) -> None:
    now = self._clock()
    if now < self._profile_attempts.get((dev, method), 0.0):
      return
    self._profile_attempts[(dev, method)] = now + PROFILE_RETRY
    try:
      self._bus.call(dev, DEVICE, method, "s", (uuid,))
    except BluezError as e:
      cloudlog.debug(f"bt music: {method} {uuid} on {dev}: {e}")

  def _keep_audio_off(self, objs: ManagedObjects, connected: dict[str, dict]) -> None:
    # an A2DP transport means the phone is streaming its audio to us: hand it back to the car
    for dev in connected:
      if children(objs, dev, MEDIA_TRANSPORT):
        cloudlog.info(f"bt music: disconnecting audio from {dev}")
        self._try_profile(dev, "DisconnectProfile", A2DP_SOURCE_UUID)

  def _connect_avrcp(self, objs: ManagedObjects, connected: dict[str, dict]) -> None:
    # Android won't always open the control channel by itself when audio isn't connected
    for dev, p in connected.items():
      uuids = {str(u).lower() for u in p.get("UUIDs", [])}
      if AVRCP_TARGET_UUID in uuids and ams.SERVICE_UUID not in uuids:
        self._try_profile(dev, "ConnectProfile", AVRCP_TARGET_UUID)

  def _update_advertisement(self, want: bool) -> None:
    discoverable = self._pairing_open
    if self._adv_registered is not None and (not want or self._adv_registered != discoverable):
      try:
        self._bus.call(self._adapter, ADV_MANAGER, "UnregisterAdvertisement", "o", (self._adv_path,))
      except BluezError:
        pass
      self._adv_registered = None
    if want and self._adv_registered is None:
      try:
        self._bus.call(self._adapter, ADV_MANAGER, "RegisterAdvertisement", "oa{sv}", (self._adv_path, {}))
        self._adv_registered = discoverable
      except BluezError as e:  # no LE support or no free slot: Android still works, iPhone can't find us
        cloudlog.debug(f"bt music: advertising failed: {e}")

  # ***** state *****

  def _set_source(self, source: Source | None) -> None:
    with self._lock:
      old, self._source = self._source, source
    if old is not None and old is not source:
      old.stop()

  def _set_status(self, status: str, action: str) -> None:
    with self._lock:
      self._status, self._action = status, action
