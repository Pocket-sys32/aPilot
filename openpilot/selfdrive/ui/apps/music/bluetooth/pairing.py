"""Pairing window: a BlueZ agent plus an LE advertisement, both exported on our bus.

Pairing is only possible while the user has opened the window from the Music app.
The agent uses numeric comparison (DisplayYesNo): the code shows on the comma and the
user confirms it on the phone. The advertisement solicits AMS, which is what makes an
iPhone list the comma in Settings > Bluetooth. Android pairs over classic Bluetooth
through adapter discoverability."""
import threading
from collections.abc import Callable

from openpilot.selfdrive.ui.apps.music.bluetooth import ams
from openpilot.selfdrive.ui.apps.music.bluetooth.bluez import ADVERTISEMENT, AGENT, INTROSPECTABLE, OBJECT_MANAGER, PROPS, Rejected

# Bluetooth audio profiles. The remote must never accept these: the phone would then play
# its music through the comma instead of the car stereo.
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
AVRCP_TARGET_UUID = "0000110c-0000-1000-8000-00805f9b34fb"  # the phone's side of AVRCP
AUDIO_UUIDS = frozenset({A2DP_SOURCE_UUID, A2DP_SINK_UUID,
                         "00001108-0000-1000-8000-00805f9b34fb",  # headset
                         "0000111e-0000-1000-8000-00805f9b34fb",  # hands-free
                         "0000111f-0000-1000-8000-00805f9b34fb"})  # hands-free gateway

PAIR_WINDOW = 120.0  # s
CAPABILITY = "DisplayYesNo"

_INTROSPECT = """<node><interface name="{iface}"/><interface name="org.freedesktop.DBus.Properties"/></node>"""


class PairingWindow:
  def __init__(self, clock: Callable[[], float], is_paired: Callable[[str], bool]):
    self._clock = clock
    self._is_paired = is_paired  # device path -> already bonded?
    self._lock = threading.Lock()
    self._deadline = 0.0
    self.passkey: int | None = None
    self.local_name = "comma"

  # ***** window *****

  @property
  def active(self) -> bool:
    with self._lock:
      return self._clock() < self._deadline

  def open(self) -> None:
    with self._lock:
      self._deadline = self._clock() + PAIR_WINDOW
      self.passkey = None

  def close(self) -> None:
    with self._lock:
      self._deadline = 0.0
      self.passkey = None

  def _require_window(self) -> None:
    if not self.active:
      raise Rejected

  # ***** org.bluez.Agent1 (BlueZ calls these on the bus serve thread) *****

  def agent(self, member: str, interface: str, body: tuple) -> tuple[str | None, tuple]:
    if interface == INTROSPECTABLE:
      return "s", (_INTROSPECT.format(iface=AGENT),)
    if member in ("Release", "Cancel"):
      with self._lock:
        self.passkey = None
      return None, ()
    if member in ("RequestConfirmation", "DisplayPasskey"):
      self._require_window()
      with self._lock:
        self.passkey = int(body[1])
      return None, ()
    if member == "RequestAuthorization":  # "just works" pairing from a phone without a display check
      self._require_window()
      return None, ()
    if member == "AuthorizeService":
      device, uuid = body[0], str(body[1]).lower()
      if uuid in AUDIO_UUIDS:
        raise Rejected  # remote control only, the audio stays with the car
      if not (self.active or self._is_paired(device)):
        raise Rejected
      return None, ()
    # RequestPinCode / RequestPasskey / DisplayPinCode: we have no keyboard; numeric comparison only
    raise Rejected

  # ***** org.bluez.LEAdvertisement1 *****

  def advertisement_properties(self) -> dict:
    return {
      "Type": ("s", "peripheral"),
      "SolicitUUIDs": ("as", [ams.SERVICE_UUID]),
      "LocalName": ("s", self.local_name),
      "Discoverable": ("b", self.active),
    }

  def advertisement(self, path: str) -> Callable[[str, str, tuple], tuple[str | None, tuple]]:
    def handle(member: str, interface: str, body: tuple) -> tuple[str | None, tuple]:
      if interface == INTROSPECTABLE:
        return "s", (_INTROSPECT.format(iface=ADVERTISEMENT),)
      if interface == PROPS and member == "GetAll":
        return "a{sv}", (self.advertisement_properties(),)
      if interface == PROPS and member == "Get":
        return "v", (self.advertisement_properties()[body[1]],)
      if interface == OBJECT_MANAGER and member == "GetManagedObjects":
        return "a{oa{sa{sv}}}", ({path: {ADVERTISEMENT: self.advertisement_properties()}},)
      if member == "Release":
        return None, ()
      raise KeyError(member)
    return handle
