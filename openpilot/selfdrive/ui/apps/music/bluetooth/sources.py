"""A connected phone we can control: an iPhone over AMS, or any phone exposing an
AVRCP target (Android) as a BlueZ MediaPlayer1. Sources are driven by the backend's
worker thread; the backend serializes access with its lock."""
from collections.abc import Callable
from typing import Any, Protocol

from openpilot.selfdrive.ui.apps.music.backend import PlaybackState
from openpilot.selfdrive.ui.apps.music.bluetooth import ams
from openpilot.selfdrive.ui.apps.music.bluetooth.bluez import GATT_CHAR, MEDIA_PLAYER


class BusLike(Protocol):
  def call(self, path: str, interface: str, member: str, signature: str | None = None, body: tuple = (),
           timeout: float = ...) -> tuple: ...
  def get_all(self, path: str, interface: str) -> dict[str, Any]: ...


class Source(Protocol):
  path: str  # object path that identifies this source (player or GATT service)

  def start(self) -> None: ...
  def stop(self) -> None: ...
  def handle_changed(self, path: str, interface: str, changed: dict[str, Any]) -> None: ...
  def snapshot(self) -> PlaybackState: ...
  def optimistic(self, command: str) -> None: ...
  def send(self, command: str, arg: float | None = None) -> None: ...


def _label(device: str, app: str) -> str:
  return f"{device} - {app}"  # the UI font has no middle dot if device and app else device or app


class AmsSource:
  """iPhone. Subscribes to Entity Update notifications and writes Remote Commands."""

  COMMANDS = {
    "play": ams.RemoteCommand.PLAY,
    "pause": ams.RemoteCommand.PAUSE,
    "next": ams.RemoteCommand.NEXT_TRACK,
    "previous": ams.RemoteCommand.PREVIOUS_TRACK,
  }

  def __init__(self, bus: BusLike, service_path: str, chars: dict[str, str], device_name: str, clock: Callable[[], float]):
    self.path = service_path
    self._bus = bus
    self._rc = chars[ams.REMOTE_COMMAND_UUID]
    self._eu = chars[ams.ENTITY_UPDATE_UUID]
    self._device = device_name
    self.ams = ams.AmsState(clock)

  def start(self) -> None:
    # notifications first, so the phone's answer to the registration isn't missed
    for char in (self._rc, self._eu):
      self._bus.call(char, GATT_CHAR, "StartNotify")
    for entity, attrs in ams.REGISTRATIONS:
      self._write(self._eu, ams.register_payload(entity, attrs))

  def stop(self) -> None:
    for char in (self._rc, self._eu):
      try:
        self._bus.call(char, GATT_CHAR, "StopNotify")
      except Exception:
        pass  # usually already disconnected

  def _write(self, char: str, value: bytes) -> None:
    self._bus.call(char, GATT_CHAR, "WriteValue", "aya{sv}", (value, {"type": ("s", "request")}))

  def handle_changed(self, path: str, interface: str, changed: dict[str, Any]) -> None:
    if interface != GATT_CHAR or "Value" not in changed:
      return
    value = bytes(changed["Value"])
    if path == self._eu:
      update = ams.parse_entity_update(value)
      if update is not None:
        self.ams.apply(update)
    elif path == self._rc:
      self.ams.commands = ams.parse_supported_commands(value)

  def snapshot(self) -> PlaybackState:
    return self.ams.playback_state(_label(self._device, self.ams.app))

  def optimistic(self, command: str) -> None:
    if command in ("play", "pause"):
      self.ams.set_playing(command == "play")

  def send(self, command: str, arg: float | None = None) -> None:
    if command == "volume" and arg is not None:
      # AMS has no absolute volume, only steps
      if abs(arg - self.ams.volume) > 0.01:
        cmd = ams.RemoteCommand.VOLUME_UP if arg > self.ams.volume else ams.RemoteCommand.VOLUME_DOWN
        self._write(self._rc, ams.command_payload(cmd))
    elif command in self.COMMANDS:
      self._write(self._rc, ams.command_payload(self.COMMANDS[command]))


class AvrcpSource:
  """Phone acting as an AVRCP target (Android). BlueZ exposes it as org.bluez.MediaPlayer1."""

  METHODS = {"play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous"}

  def __init__(self, bus: BusLike, player_path: str, device_name: str, clock: Callable[[], float]):
    self.path = player_path
    self._bus = bus
    self._device = device_name
    self._clock = clock
    self.app = ""
    self.status = ""
    self.position = 0.0
    self.position_at = clock()
    self.title = self.artist = self.album = ""
    self.duration = 0.0

  def start(self) -> None:
    self._apply(self._bus.get_all(self.path, MEDIA_PLAYER))

  def stop(self) -> None:
    pass

  def _apply(self, p: dict[str, Any]) -> None:
    if "Status" in p:
      self.position, self.position_at = self._live_position(), self._clock()  # keep the bar continuous
      self.status = str(p["Status"])
    if "Position" in p:
      self.position, self.position_at = int(p["Position"]) / 1000.0, self._clock()
    if "Name" in p:
      self.app = str(p["Name"])
    if "Track" in p:
      t = p["Track"]
      self.title = str(t.get("Title", ""))
      self.artist = str(t.get("Artist", ""))
      self.album = str(t.get("Album", ""))
      self.duration = int(t.get("Duration", 0)) / 1000.0

  def handle_changed(self, path: str, interface: str, changed: dict[str, Any]) -> None:
    if path == self.path and interface == MEDIA_PLAYER:
      self._apply(changed)

  @property
  def is_playing(self) -> bool:
    return self.status == "playing"

  def _live_position(self) -> float:
    pos = self.position + ((self._clock() - self.position_at) if self.is_playing else 0.0)
    return min(pos, self.duration) if self.duration > 0 else pos

  def snapshot(self) -> PlaybackState:
    return PlaybackState(available=True, title=self.title, artist=self.artist, album=self.album,
                         is_playing=self.is_playing, progress_s=self._live_position(), duration_s=self.duration,
                         device=_label(self._device, self.app))

  def optimistic(self, command: str) -> None:
    if command in ("play", "pause"):
      self._apply({"Status": "playing" if command == "play" else "paused"})

  def send(self, command: str, arg: float | None = None) -> None:
    if command in self.METHODS:  # AVRCP absolute volume belongs to the audio sink, which we aren't
      self._bus.call(self.path, MEDIA_PLAYER, self.METHODS[command])
