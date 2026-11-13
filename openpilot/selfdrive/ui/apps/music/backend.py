"""Music controller backends. The UI only talks to MusicBackend, so Bluetooth or
Spotify implementation can replace MockMusicBackend without UI changes."""
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PlaybackState:
  available: bool = False  # backend connected and a player is active
  title: str = ""
  artist: str = ""
  album: str = ""
  is_playing: bool = False
  progress_s: float = 0.0
  duration_s: float = 0.0
  volume: float = 0.5  # 0..1
  device: str = ""  # where the music is actually playing, e.g. "Pixel 8"
  status: str = ""  # shown when there's nothing to control, e.g. "Spotify Premium required"
  action: str = ""  # label of a button that fixes that status, e.g. "Pair phone" (runs run_action)


class MusicBackend(Protocol):
  def state(self) -> PlaybackState: ...
  def play_pause(self) -> None: ...
  def next(self) -> None: ...
  def previous(self) -> None: ...
  def set_volume(self, volume: float) -> None: ...
  def run_action(self) -> None: ...


_DEMO_TRACKS = (
  ("Highway Lines", "The Lane Keepers", "Long Drive", 214.0),
  ("Open Road", "Cruise Control", "Engaged", 187.0),
  ("Night Commute", "Model Dreams", "Supercombo", 243.0),
)


class MockMusicBackend:
  """Simulated player for UI development; time advances while 'playing'."""

  def __init__(self, clock=time.monotonic):
    self._clock = clock
    self._index = 0
    self._playing = False
    self._progress = 0.0
    self._last_t = clock()
    self._volume = 0.5

  def _tick(self) -> None:
    now = self._clock()
    if self._playing:
      self._progress += now - self._last_t
      while self._progress >= _DEMO_TRACKS[self._index][3]:
        self._progress -= _DEMO_TRACKS[self._index][3]
        self._index = (self._index + 1) % len(_DEMO_TRACKS)
    self._last_t = now

  def state(self) -> PlaybackState:
    self._tick()
    title, artist, album, duration = _DEMO_TRACKS[self._index]
    return PlaybackState(True, title, artist, album, self._playing, self._progress, duration, self._volume)

  def play_pause(self) -> None:
    self._tick()
    self._playing = not self._playing

  def next(self) -> None:
    self._tick()
    self._index = (self._index + 1) % len(_DEMO_TRACKS)
    self._progress = 0.0

  def previous(self) -> None:
    self._tick()
    # like most players: restart the track unless near its start
    if self._progress > 3.0:
      self._progress = 0.0
    else:
      self._index = (self._index - 1) % len(_DEMO_TRACKS)
      self._progress = 0.0

  def set_volume(self, volume: float) -> None:
    self._volume = max(0.0, min(1.0, volume))

  def run_action(self) -> None:
    pass
