"""MusicBackend that remote-controls Spotify playback through the Web API.

All network calls happen on a background thread so the UI never waits on the network.
It only polls while the Music screen is reading state, and buttons update the shown
state optimistically, then reconcile with Spotify shortly after."""
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace

import requests

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.apps.music.backend import PlaybackState
from openpilot.selfdrive.ui.apps.music.spotify.auth import AuthError, TokenStore
from openpilot.selfdrive.ui.apps.music.spotify.client import (NoActiveDevice, Player, PremiumRequired, RateLimited,
                                                              SpotifyClient, SpotifyError)

POLL_INTERVAL = 1.5  # s between /me/player polls while the screen is open
IN_USE_WINDOW = 3.0  # s since the UI last read state before polling stops
RECONCILE_DELAY = 0.4  # s after a command before re-reading Spotify
LOGIN_CHECK_INTERVAL = 5.0  # s between checks for a new login while logged out

NOT_SET_UP = "Set up Spotify: run spotify_login.py"
LOGIN_EXPIRED = "Spotify login expired: log in again"
PREMIUM = "Spotify Premium required"
NOTHING_PLAYING = "Play something on Spotify"
OFFLINE = "Offline"


class SpotifyMusicBackend:
  def __init__(self, client: SpotifyClient | None = None, clock: Callable[[], float] = time.monotonic,
               start_thread: bool = True):
    self._client = client or SpotifyClient(TokenStore())
    self._clock = clock
    self._lock = threading.Lock()
    self._commands: queue.Queue[tuple[str, float | None]] = queue.Queue()
    self._state = PlaybackState(status="" if self._client.logged_in else NOT_SET_UP)
    self._fetched_at = clock()  # when _state.progress_s was true
    self._last_used = -1e9
    self._next_poll = 0.0
    self._next_login_check = 0.0
    self._start_thread = start_thread
    self._thread: threading.Thread | None = None

  # ***** MusicBackend (UI thread) *****

  def state(self) -> PlaybackState:
    now = self._clock()
    self._last_used = now
    self._ensure_thread()
    with self._lock:
      st, fetched_at = self._state, self._fetched_at
    if st.is_playing and st.duration_s > 0:
      # advance the progress bar smoothly between polls
      st = replace(st, progress_s=min(st.duration_s, st.progress_s + (now - fetched_at)))
    return st

  def play_pause(self) -> None:
    with self._lock:
      if not self._state.available:
        return
      playing = self._state.is_playing
      self._set_state(replace(self._state, is_playing=not playing, progress_s=self._live_progress()))
    self._commands.put(("pause" if playing else "play", None))

  def next(self) -> None:
    self._commands.put(("next", None))

  def previous(self) -> None:
    self._commands.put(("previous", None))

  def set_volume(self, volume: float) -> None:
    with self._lock:
      self._set_state(replace(self._state, volume=max(0.0, min(1.0, volume))))
    self._commands.put(("volume", volume))

  def run_action(self) -> None:
    pass  # login happens in spotify_login.py

  # ***** worker *****

  def _ensure_thread(self) -> None:
    if self._start_thread and self._thread is None:
      self._thread = threading.Thread(target=self._run, name="spotify", daemon=True)
      self._thread.start()

  def _run(self) -> None:
    while True:
      try:
        cmd = self._commands.get(timeout=0.2)
      except queue.Empty:
        cmd = None
      self.step(cmd)

  def step(self, cmd: tuple[str, float | None] | None = None) -> None:
    """One worker iteration: run a command if given, then poll if due. Public for tests."""
    now = self._clock()
    try:
      if not self._client.logged_in:
        if now >= self._next_login_check:
          self._next_login_check = now + LOGIN_CHECK_INTERVAL
          self._client.reload()
        if not self._client.logged_in:
          self._set_status(NOT_SET_UP, clear_track=True)
          return

      if cmd is not None:
        self._run_command(*cmd)
        self._next_poll = now + RECONCILE_DELAY

      in_use = now - self._last_used < IN_USE_WINDOW
      if in_use and now >= self._next_poll:
        self._next_poll = now + POLL_INTERVAL
        self._apply_player(self._client.player())

    except RateLimited as e:
      self._next_poll = now + e.retry_after
    except PremiumRequired:
      self._set_status(PREMIUM, clear_track=False)
    except NoActiveDevice:
      self._set_status(NOTHING_PLAYING, clear_track=True)
    except AuthError as e:
      cloudlog.warning(f"spotify auth: {e}")
      self._set_status(LOGIN_EXPIRED, clear_track=True)
      self._client.reload()
    except (SpotifyError, requests.RequestException) as e:
      cloudlog.debug(f"spotify: {e}")
      self._set_status(OFFLINE, clear_track=False)
      self._next_poll = now + POLL_INTERVAL * 2

  def _run_command(self, name: str, arg: float | None) -> None:
    if name == "play":
      self._client.play()
    elif name == "pause":
      self._client.pause()
    elif name == "next":
      self._client.next()
    elif name == "previous":
      self._client.previous()
    elif name == "volume" and arg is not None:
      self._client.set_volume(arg)

  def _apply_player(self, player: Player | None) -> None:
    if player is None:
      self._set_status(NOTHING_PLAYING, clear_track=True)
      return
    with self._lock:
      volume = self._state.volume if player.volume is None else player.volume
      self._set_state(PlaybackState(available=True, title=player.title, artist=player.artist, album=player.album,
                                    is_playing=player.is_playing, progress_s=player.progress_s,
                                    duration_s=player.duration_s, volume=volume, device=player.device))

  # ***** state helpers (call with the lock held, except _set_status) *****

  def _live_progress(self) -> float:
    st = self._state
    if st.is_playing:
      return min(st.duration_s, st.progress_s + (self._clock() - self._fetched_at))
    return st.progress_s

  def _set_state(self, st: PlaybackState) -> None:
    self._state = st
    self._fetched_at = self._clock()

  def _set_status(self, status: str, clear_track: bool) -> None:
    with self._lock:
      if clear_track:
        self._set_state(PlaybackState(status=status, volume=self._state.volume))
      else:
        # keep showing the last track, but no longer controllable
        self._set_state(replace(self._state, available=False, is_playing=False, status=status))
