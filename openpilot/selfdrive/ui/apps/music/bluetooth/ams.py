"""Apple Media Service (AMS): the iPhone's BLE GATT service for remote-controlling
whatever app is playing (Spotify, Apple Music, podcasts...). The comma is the GATT
client ("Media Remote"); the iPhone is the server ("Media Source").

Pure protocol code: encoding writes, decoding notifications, and folding them into a
PlaybackState. See Apple's "Apple Media Service Specification" (v1.0)."""
import enum
from collections.abc import Callable
from dataclasses import dataclass

from openpilot.selfdrive.ui.apps.music.backend import PlaybackState

SERVICE_UUID = "89d3502b-0f36-433a-8ef4-c502ad55f8dc"
REMOTE_COMMAND_UUID = "9b3c81d8-57b1-4a8a-b8df-0e56f7ca51c2"  # write, notify
ENTITY_UPDATE_UUID = "2f7cabce-808d-411f-9a0c-bb92ba96c102"  # write with response, notify
ENTITY_ATTRIBUTE_UUID = "c6b2f38c-23ab-46d8-a6ab-a3a870bbd5d7"  # read, write (full values of truncated ones)


class RemoteCommand(enum.IntEnum):
  PLAY = 0
  PAUSE = 1
  TOGGLE_PLAY_PAUSE = 2
  NEXT_TRACK = 3
  PREVIOUS_TRACK = 4
  VOLUME_UP = 5
  VOLUME_DOWN = 6
  ADVANCE_REPEAT = 7
  ADVANCE_SHUFFLE = 8
  SKIP_FORWARD = 9
  SKIP_BACKWARD = 10
  LIKE_TRACK = 11
  DISLIKE_TRACK = 12
  BOOKMARK_TRACK = 13


class Entity(enum.IntEnum):
  PLAYER = 0
  QUEUE = 1
  TRACK = 2


class PlayerAttr(enum.IntEnum):
  NAME = 0  # app name, e.g. "Spotify"
  PLAYBACK_INFO = 1  # "state,rate,elapsed"
  VOLUME = 2  # "0.0".."1.0"


class TrackAttr(enum.IntEnum):
  ARTIST = 0
  ALBUM = 1
  TITLE = 2
  DURATION = 3  # seconds, float string


class PlaybackStatus(enum.IntEnum):
  PAUSED = 0
  PLAYING = 1
  REWINDING = 2
  FAST_FORWARDING = 3


FLAG_TRUNCATED = 0x01

# what the Music app needs; registering more makes the phone send more traffic
REGISTRATIONS: tuple[tuple[Entity, tuple[int, ...]], ...] = (
  (Entity.PLAYER, (PlayerAttr.NAME, PlayerAttr.PLAYBACK_INFO, PlayerAttr.VOLUME)),
  (Entity.TRACK, (TrackAttr.ARTIST, TrackAttr.ALBUM, TrackAttr.TITLE, TrackAttr.DURATION)),
)


def register_payload(entity: Entity, attrs: tuple[int, ...]) -> bytes:
  """Value written to Entity Update to subscribe to an entity's attributes."""
  return bytes([entity, *attrs])


def command_payload(command: RemoteCommand) -> bytes:
  return bytes([command])


@dataclass(frozen=True)
class EntityUpdate:
  entity: int
  attr: int
  truncated: bool
  value: str


def parse_entity_update(data: bytes) -> EntityUpdate | None:
  """Entity Update notification: [EntityID][AttributeID][Flags][UTF-8 value...]."""
  if len(data) < 3:
    return None
  return EntityUpdate(data[0], data[1], bool(data[2] & FLAG_TRUNCATED), data[3:].decode("utf-8", errors="replace"))


def parse_supported_commands(data: bytes) -> frozenset[int]:
  """Remote Command notification: one byte per command the current app supports."""
  return frozenset(data)


def parse_playback_info(value: str) -> tuple[PlaybackStatus, float, float] | None:
  """'state,rate,elapsed'; empty when no app is playing."""
  parts = value.split(",")
  if len(parts) != 3:
    return None
  try:
    status = PlaybackStatus(int(parts[0]))
    return status, float(parts[1] or 0), float(parts[2] or 0)
  except ValueError:
    return None


def _float(value: str, default: float = 0.0) -> float:
  try:
    return float(value)
  except ValueError:
    return default


class AmsState:
  """Accumulates Entity Update notifications into what the Music app shows.

  Elapsed time is only sent when playback state changes, so the current position is
  elapsed + (now - received) * rate, as the spec describes."""

  def __init__(self, clock: Callable[[], float]):
    self._clock = clock
    self.app = ""
    self.status: PlaybackStatus | None = None  # None: no app has a player
    self.rate = 0.0
    self.elapsed = 0.0
    self.elapsed_at = 0.0
    self.volume = 0.5
    self.title = ""
    self.artist = ""
    self.album = ""
    self.duration = 0.0
    self.commands: frozenset[int] = frozenset()

  def apply(self, update: EntityUpdate) -> None:
    v = update.value
    if update.entity == Entity.PLAYER:
      if update.attr == PlayerAttr.NAME:
        self.app = v
      elif update.attr == PlayerAttr.PLAYBACK_INFO:
        info = parse_playback_info(v)
        if info is None:
          self.status, self.rate = None, 0.0
        else:
          self.status, self.rate, self.elapsed = info
          self.elapsed_at = self._clock()
      elif update.attr == PlayerAttr.VOLUME:
        self.volume = max(0.0, min(1.0, _float(v, self.volume)))
    elif update.entity == Entity.TRACK:
      if update.attr == TrackAttr.ARTIST:
        self.artist = v
      elif update.attr == TrackAttr.ALBUM:
        self.album = v
      elif update.attr == TrackAttr.TITLE:
        self.title = v
      elif update.attr == TrackAttr.DURATION:
        self.duration = max(0.0, _float(v))

  @property
  def is_playing(self) -> bool:
    return self.status == PlaybackStatus.PLAYING

  def position(self) -> float:
    pos = self.elapsed + (self._clock() - self.elapsed_at) * self.rate
    return max(0.0, min(self.duration, pos) if self.duration > 0 else pos)

  def set_playing(self, playing: bool) -> None:
    """Optimistic local update after sending play/pause; the phone confirms with a notification."""
    self.elapsed, self.elapsed_at = self.position(), self._clock()
    self.status = PlaybackStatus.PLAYING if playing else PlaybackStatus.PAUSED
    self.rate = 1.0 if playing else 0.0

  def playback_state(self, device: str) -> PlaybackState:
    has_player = self.status is not None or bool(self.title)
    return PlaybackState(available=has_player, title=self.title, artist=self.artist, album=self.album,
                         is_playing=self.is_playing, progress_s=self.position(), duration_s=self.duration,
                         volume=self.volume, device=device)
