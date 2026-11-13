"""Thin BlueZ-over-D-Bus layer (jeepney, like system/ui/lib/wifi_manager.py).

Bus wraps one threaded router: method calls into BlueZ, signal subscriptions, and
serving our own exported objects (pairing agent, LE advertisement) that BlueZ calls
back into. Exported objects are served on their own thread, because BlueZ calls them
*while* handling our requests (RegisterAdvertisement reads the advertisement before
it replies), so answering them from the calling thread would deadlock."""
import queue
import threading
from collections.abc import Callable
from typing import Any

from jeepney import DBusAddress, MatchRule, Message, MessageType, new_error, new_method_call, new_method_return
from jeepney.bus_messages import message_bus
from jeepney.io.threading import DBusRouter, open_dbus_connection
from jeepney.low_level import HeaderFields
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHAR = "org.bluez.GattCharacteristic1"
MEDIA_PLAYER = "org.bluez.MediaPlayer1"
MEDIA_TRANSPORT = "org.bluez.MediaTransport1"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT = "org.bluez.Agent1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
ADVERTISEMENT = "org.bluez.LEAdvertisement1"
PROPS = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

CALL_TIMEOUT = 10.0  # s; GATT writes to a slow phone can take a while

ManagedObjects = dict[str, dict[str, dict[str, Any]]]


class BluezError(Exception):
  def __init__(self, name: str, message: str = ""):
    super().__init__(f"{name}: {message}" if message else name)
    self.name = name


class Rejected(Exception):
  """Raised by an exported-object handler to answer with org.bluez.Error.Rejected."""


def variant_value(v: Any) -> Any:
  """jeepney gives variants as (signature, value); unwrap, recursing into a{sv}."""
  sig, value = v
  return props(value) if sig == "a{sv}" else value


def props(d: dict[str, Any]) -> dict[str, Any]:
  return {k: variant_value(v) for k, v in d.items()}


def msg_path(msg: Message) -> str:
  return msg.header.fields.get(HeaderFields.path, "")


def msg_member(msg: Message) -> str:
  return msg.header.fields.get(HeaderFields.member, "")


def msg_interface(msg: Message) -> str:
  return msg.header.fields.get(HeaderFields.interface, "")


# handler(member, interface, body) -> (reply signature, reply body); raise Rejected / KeyError to fail
Handler = Callable[[str, str, tuple], tuple[str | None, tuple]]


class Bus:
  def __init__(self, export_root: str = "/ai/comma/music"):
    self._router = DBusRouter(open_dbus_connection(bus="SYSTEM"))
    self._export_root = export_root
    self._handlers: dict[str, Handler] = {}
    self._handlers_lock = threading.Lock()
    self._calls: queue.Queue[Message] = queue.Queue()
    self._call_filter = self._router.filter(MatchRule(type="method_call", path_namespace=export_root), queue=self._calls)
    self._signal_filters: list[Any] = []
    threading.Thread(target=self._serve, name="bt-dbus-serve", daemon=True).start()

  def close(self) -> None:
    self._router.close()

  # ***** calls into BlueZ *****

  def call(self, path: str, interface: str, member: str, signature: str | None = None, body: tuple = (),
           timeout: float = CALL_TIMEOUT) -> tuple:
    msg = new_method_call(DBusAddress(path, BLUEZ, interface), member, signature, body)
    reply = self._router.send_and_get_reply(msg, timeout=timeout)
    try:
      return unwrap_msg(reply)
    except DBusErrorResponse as e:
      raise BluezError(e.name, str(e.data[0]) if e.data else "") from None

  def managed_objects(self) -> ManagedObjects:
    (objs,) = self.call("/", OBJECT_MANAGER, "GetManagedObjects")
    return {path: {iface: props(p) for iface, p in ifaces.items()} for path, ifaces in objs.items()}

  def get_all(self, path: str, interface: str) -> dict[str, Any]:
    (p,) = self.call(path, PROPS, "GetAll", "s", (interface,))
    return props(p)

  def set_property(self, path: str, interface: str, name: str, signature: str, value: Any) -> None:
    self.call(path, PROPS, "Set", "ssv", (interface, name, (signature, value)))

  # ***** signals *****

  def subscribe(self, rule: MatchRule, q: queue.Queue) -> None:
    """Deliver matching signals into q (messages arrive on the router thread)."""
    self._router.send_and_get_reply(message_bus.AddMatch(rule), timeout=CALL_TIMEOUT)
    self._signal_filters.append(self._router.filter(rule, queue=q))

  # ***** exported objects *****

  def object_path(self, name: str) -> str:
    return f"{self._export_root}/{name}"

  def export(self, path: str, handler: Handler) -> None:
    with self._handlers_lock:
      self._handlers[path] = handler

  def unexport(self, path: str) -> None:
    with self._handlers_lock:
      self._handlers.pop(path, None)

  def _serve(self) -> None:
    while True:
      msg = self._calls.get()
      if msg.header.message_type != MessageType.method_call:
        continue
      with self._handlers_lock:
        handler = self._handlers.get(msg_path(msg))
      try:
        if handler is None:
          raise KeyError(msg_path(msg))
        sig, body = handler(msg_member(msg), msg_interface(msg), msg.body)
        reply = new_method_return(msg, sig, body)
      except Rejected:
        reply = new_error(msg, "org.bluez.Error.Rejected")
      except KeyError:
        reply = new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", "s", (f"no {msg_member(msg)} here",))
      except Exception as e:  # never leave BlueZ waiting on a reply
        reply = new_error(msg, "org.bluez.Error.Failed", "s", (str(e),))
      try:
        self._router.send(reply)
      except OSError:
        return


def properties_changed_rule() -> MatchRule:
  return MatchRule(type="signal", sender=BLUEZ, interface=PROPS, member="PropertiesChanged")


def interfaces_rule() -> MatchRule:
  return MatchRule(type="signal", sender=BLUEZ, interface=OBJECT_MANAGER)


def find_adapter(objs: ManagedObjects) -> str | None:
  return next((p for p, ifaces in sorted(objs.items()) if ADAPTER in ifaces), None)


def children(objs: ManagedObjects, parent: str, interface: str) -> dict[str, dict[str, Any]]:
  prefix = parent.rstrip("/") + "/"
  return {p: ifaces[interface] for p, ifaces in objs.items() if p.startswith(prefix) and interface in ifaces}
