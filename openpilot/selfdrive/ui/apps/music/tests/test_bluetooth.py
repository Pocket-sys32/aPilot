import unittest

from jeepney import new_signal, DBusAddress

from openpilot.selfdrive.ui.apps.music.bluetooth import ams
from openpilot.selfdrive.ui.apps.music.bluetooth.backend import (BluetoothMusicBackend, ACTION_CANCEL, ACTION_PAIR, ACTION_TURN_ON,
                                                                  NO_BLUETOOTH, NO_PHONE, OFF, BLOCKED, NOTHING_PLAYING)
from openpilot.selfdrive.ui.apps.music.bluetooth.bluez import (ADAPTER, DEVICE, GATT_CHAR, GATT_SERVICE, MEDIA_PLAYER, MEDIA_TRANSPORT,
                                                                PROPS, BluezError, Rejected)
from openpilot.selfdrive.ui.apps.music.bluetooth.pairing import A2DP_SINK_UUID, A2DP_SOURCE_UUID, AVRCP_TARGET_UUID, PairingWindow

HCI = "/org/bluez/hci0"
PHONE = HCI + "/dev_AA"
SVC = PHONE + "/service0010"
RC, EU = SVC + "/char0011", SVC + "/char0014"
PLAYER = PHONE + "/player0"


def eu(entity, attr, value: str, flags=0) -> bytes:
  return bytes([entity, attr, flags]) + value.encode()


def parsed(data: bytes) -> ams.EntityUpdate:
  u = ams.parse_entity_update(data)
  assert u is not None
  return u


class TestAms(unittest.TestCase):
  def test_payloads(self):
    self.assertEqual(ams.register_payload(ams.Entity.TRACK, (0, 1, 2, 3)), b"\x02\x00\x01\x02\x03")
    self.assertEqual(ams.command_payload(ams.RemoteCommand.NEXT_TRACK), b"\x03")

  def test_parse(self):
    u = parsed(eu(2, 2, "Song ✓", flags=1))
    self.assertEqual((u.entity, u.attr, u.truncated, u.value), (2, 2, True, "Song ✓"))
    self.assertIsNone(ams.parse_entity_update(b"\x00\x01"))
    self.assertEqual(ams.parse_playback_info("1,1.0,12.5"), (ams.PlaybackStatus.PLAYING, 1.0, 12.5))
    self.assertIsNone(ams.parse_playback_info(""))
    self.assertIsNone(ams.parse_playback_info("9,1,2"))

  def test_state_interpolates_elapsed(self):
    now = [10.0]
    s = ams.AmsState(lambda: now[0])
    for data in (eu(2, 2, "Song"), eu(2, 0, "Artist"), eu(2, 3, "200.5"), eu(0, 0, "Spotify"), eu(0, 1, "1,1.0,30"),
                 eu(0, 2, "0.25")):
      s.apply(parsed(data))
    now[0] += 4
    st = s.playback_state("iPhone")
    self.assertTrue(st.available and st.is_playing)
    self.assertEqual((st.title, st.artist, st.duration_s, st.volume, st.device), ("Song", "Artist", 200.5, 0.25, "iPhone"))
    self.assertAlmostEqual(st.progress_s, 34.0)
    s.set_playing(False)
    now[0] += 10
    self.assertAlmostEqual(s.position(), 34.0)  # paused: no longer advancing


class TestPairing(unittest.TestCase):
  def setUp(self):
    self.now = [0.0]
    self.w = PairingWindow(lambda: self.now[0], lambda dev: dev == PHONE)

  def test_rejects_pairing_outside_window(self):
    with self.assertRaises(Rejected):
      self.w.agent("RequestConfirmation", "org.bluez.Agent1", ("/x", 123456))
    self.w.open()
    self.w.agent("RequestConfirmation", "org.bluez.Agent1", ("/x", 123456))
    self.assertEqual(self.w.passkey, 123456)
    self.now[0] += 1000
    self.assertFalse(self.w.active)

  def test_never_authorizes_audio(self):
    self.w.open()
    for uuid in (A2DP_SINK_UUID, A2DP_SOURCE_UUID):
      with self.assertRaises(Rejected):
        self.w.agent("AuthorizeService", "org.bluez.Agent1", (PHONE, uuid))
    self.w.agent("AuthorizeService", "org.bluez.Agent1", (PHONE, AVRCP_TARGET_UUID))

  def test_advertisement_solicits_ams(self):
    sig, (p,) = self.w.advertisement("/adv")("GetAll", PROPS, ("org.bluez.LEAdvertisement1",))
    self.assertEqual(sig, "a{sv}")
    self.assertEqual(p["SolicitUUIDs"], ("as", [ams.SERVICE_UUID]))
    self.assertEqual(p["Discoverable"], ("b", False))


class FakeBus:
  def __init__(self, objs):
    self.objs = objs
    self.calls: list[tuple] = []
    self.set_props: list[tuple] = []
    self.exported = {}
    self.fail: set[str] = set()

  def subscribe(self, rule, q):
    pass

  def object_path(self, name):
    return "/test/" + name

  def export(self, path, handler):
    self.exported[path] = handler

  def close(self):
    pass

  def managed_objects(self):
    return self.objs

  def get_all(self, path, interface):
    return self.objs[path][interface]

  def call(self, path, interface, member, signature=None, body=(), timeout=0):
    self.calls.append((path, member, body))
    if member in self.fail:
      raise BluezError("org.bluez.Error.Failed")
    return ()

  def set_property(self, path, interface, name, signature, value):
    self.set_props.append((path, name, value))

  def members(self):
    return [c[1] for c in self.calls]


def adapter(powered=True, state="on"):
  return {HCI: {ADAPTER: {"Alias": "comma", "Powered": powered, "PowerState": state}}}


def phone(**extra):
  p = {"Alias": "Pixel", "Paired": True, "Connected": True, "ServicesResolved": True, "UUIDs": []}
  p.update(extra)
  return {PHONE: {DEVICE: p}}


def changed(path, interface, values):
  msg = new_signal(DBusAddress(path, interface=PROPS), "PropertiesChanged", "sa{sv}as", (interface, values, []))
  return msg


class TestBackend(unittest.TestCase):
  def make(self, objs):
    self.now = 100.0
    self.bus = FakeBus(objs)
    b = BluetoothMusicBackend(bus_factory=lambda: self.bus, clock=lambda: self.now, start_thread=False)
    b.state()
    b.step()
    return b

  def test_no_bluez(self):
    def boom():
      raise OSError("no system bus")
    b = BluetoothMusicBackend(bus_factory=boom, start_thread=False)
    b.step()
    self.assertEqual(b.state().status, NO_BLUETOOTH)

  def test_adapter_off_and_blocked(self):
    b = self.make(adapter(powered=False, state="off"))
    self.assertEqual((b.state().status, b.state().action), (OFF, ACTION_TURN_ON))
    b.run_action()
    b.step(b._events.get_nowait())
    self.assertIn((HCI, "Powered", True), self.bus.set_props)
    b = self.make(adapter(powered=False, state="off-blocked"))
    self.assertEqual((b.state().status, b.state().action), (BLOCKED, ""))

  def test_pair_flow(self):
    b = self.make(adapter())
    st = b.state()
    self.assertEqual((st.status, st.action), (NO_PHONE, ACTION_PAIR))
    self.assertIn("RegisterAdvertisement", self.bus.members())  # iPhones can find us to reconnect
    b.run_action()
    b.step(b._events.get_nowait())
    self.assertIn("RequestDefaultAgent", self.bus.members())
    self.assertIn((HCI, "Discoverable", True), self.bus.set_props)
    self.assertEqual(b.state().action, ACTION_CANCEL)
    self.assertIn("comma", b.state().status)
    # phone pairs: agent shows the code, then the device becomes paired and trusted
    self.bus.exported["/test/agent"]("RequestConfirmation", "org.bluez.Agent1", (PHONE, 42))
    self.assertIn("000042", b.state().status)
    b.step(changed(PHONE, DEVICE, {"Paired": ("b", True)}))
    self.assertIn((PHONE, "Trusted", True), self.bus.set_props)
    self.assertIn("UnregisterAgent", self.bus.members())
    self.assertFalse(b.pairing.active)

  def test_pairing_times_out(self):
    b = self.make(adapter())
    b.run_action()
    b.step(b._events.get_nowait())
    self.now += 1000
    b.step()
    self.assertIn((HCI, "Discoverable", False), self.bus.set_props)
    self.assertEqual(b.state().action, ACTION_PAIR)

  def test_iphone_ams(self):
    objs = adapter() | phone(Alias="iPhone")
    objs[SVC] = {GATT_SERVICE: {"UUID": ams.SERVICE_UUID.upper()}}
    objs[RC] = {GATT_CHAR: {"UUID": ams.REMOTE_COMMAND_UUID}}
    objs[EU] = {GATT_CHAR: {"UUID": ams.ENTITY_UPDATE_UUID}}
    b = self.make(objs)
    writes = [c for c in self.bus.calls if c[1] == "WriteValue"]
    self.assertEqual([c[0] for c in writes], [EU, EU])
    self.assertEqual(writes[0][2][0], b"\x00\x00\x01\x02")
    self.assertEqual(b.state().status, NOTHING_PLAYING)
    for data in (eu(2, 2, "Song"), eu(0, 0, "Music"), eu(0, 1, "1,1.0,5")):
      b.step(changed(EU, GATT_CHAR, {"Value": ("ay", data)}))
    st = b.state()
    self.assertTrue(st.available and st.is_playing)
    self.assertEqual((st.title, st.device), ("Song", "iPhone - Music"))
    b.play_pause()
    self.assertFalse(b.state().is_playing)  # optimistic
    b.step(b._events.get_nowait())
    self.assertEqual(self.bus.calls[-1], (RC, "WriteValue", (b"\x01", {"type": ("s", "request")})))
    self.assertNotIn("RegisterAdvertisement", self.bus.members()[2:])  # connected: stop advertising

  def test_android_avrcp(self):
    objs = adapter() | phone()
    objs[PLAYER] = {MEDIA_PLAYER: {"Name": "Spotify", "Status": "paused", "Position": 30_000,
                                   "Track": {"Title": "Song", "Artist": "A", "Duration": 200_000}}}
    b = self.make(objs)
    st = b.state()
    self.assertEqual((st.title, st.progress_s, st.duration_s, st.device), ("Song", 30.0, 200.0, "Pixel - Spotify"))
    b.step(changed(PLAYER, MEDIA_PLAYER, {"Status": ("s", "playing")}))
    self.now += 2
    self.assertAlmostEqual(b.state().progress_s, 32.0)
    b.next()
    b.step(b._events.get_nowait())
    self.assertEqual(self.bus.calls[-1][:2], (PLAYER, "Next"))

  def test_android_audio_is_handed_back_to_car(self):
    objs = adapter() | phone(UUIDs=[AVRCP_TARGET_UUID, A2DP_SOURCE_UUID])
    objs[PHONE + "/fd0"] = {MEDIA_TRANSPORT: {"State": "active"}}
    self.make(objs)
    self.assertIn((PHONE, "DisconnectProfile", (A2DP_SOURCE_UUID,)), self.bus.calls)
    # no player yet: the comma opens the remote-control channel itself
    self.assertIn((PHONE, "ConnectProfile", (AVRCP_TARGET_UUID,)), self.bus.calls)

  def test_dbus_errors_dont_kill_worker(self):
    objs = adapter() | phone()
    objs[PLAYER] = {MEDIA_PLAYER: {"Status": "playing", "Track": {"Title": "Song"}}}
    b = self.make(objs)
    self.bus.fail.add("Pause")
    b.play_pause()
    b.step(b._events.get_nowait())
    self.assertIsNotNone(b._bus)


if __name__ == "__main__":
  unittest.main()
