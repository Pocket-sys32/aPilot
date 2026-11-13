"""Minimal Spotify Web API client for remote-controlling playback on the user's own devices."""
from dataclasses import dataclass
from typing import Any, cast

import requests

from openpilot.selfdrive.ui.apps.music.spotify.auth import AuthError, HttpSession, Tokens, TokenStore, refresh

API = "https://api.spotify.com/v1"
TIMEOUT = 5  # s


class SpotifyError(Exception):
  def __init__(self, message: str, status: int = 0):
    super().__init__(message)
    self.status = status


class PremiumRequired(SpotifyError):
  """Playback control endpoints need Spotify Premium."""


class NoActiveDevice(SpotifyError):
  """Nothing is playing anywhere, so there's no device to send commands to."""


class RateLimited(SpotifyError):
  def __init__(self, retry_after: float):
    super().__init__(f"rate limited, retry after {retry_after}s", 429)
    self.retry_after = retry_after


@dataclass(frozen=True)
class Player:
  title: str
  artist: str
  album: str
  is_playing: bool
  progress_s: float
  duration_s: float
  volume: float | None  # 0..1, None when the device doesn't support remote volume
  device: str


def parse_player(data: dict) -> Player | None:
  item = data.get("item")
  if not item:
    return None
  device = data.get("device") or {}
  vol = device.get("volume_percent")
  if item.get("type") == "episode":
    artist = (item.get("show") or {}).get("name", "")
    album = artist
  else:
    artist = ", ".join(a.get("name", "") for a in item.get("artists", []))
    album = (item.get("album") or {}).get("name", "")
  return Player(
    title=item.get("name", ""),
    artist=artist,
    album=album,
    is_playing=bool(data.get("is_playing")),
    progress_s=(data.get("progress_ms") or 0) / 1000,
    duration_s=(item.get("duration_ms") or 0) / 1000,
    volume=None if vol is None else vol / 100,
    device=device.get("name", ""),
  )


class SpotifyClient:
  def __init__(self, store: TokenStore, session: HttpSession | None = None):
    self._store = store
    self._session: HttpSession = session if session is not None else cast(HttpSession, requests.Session())
    self._tokens: Tokens | None = store.load()

  @property
  def logged_in(self) -> bool:
    return self._tokens is not None

  def reload(self) -> None:
    """Pick up a login done by spotify_login.py while running."""
    self._tokens = self._store.load()

  def _access_token(self, force_refresh: bool = False) -> str:
    if self._tokens is None:
      raise AuthError("not logged in")
    if force_refresh or self._tokens.expired:
      self._tokens = refresh(self._tokens, self._session)
      self._store.save(self._tokens)
    return self._tokens.access_token

  def _request(self, method: str, path: str, params: dict | None = None) -> Any:
    for attempt in range(2):
      r = self._session.request(method, API + path, params=params, timeout=TIMEOUT,
                                headers={"Authorization": f"Bearer {self._access_token(force_refresh=attempt > 0)}"})
      if r.status_code == 401 and attempt == 0:
        continue  # access token expired early or was revoked: refresh once and retry
      break

    if r.status_code == 429:
      raise RateLimited(float(r.headers.get("Retry-After", "5")))
    if r.status_code == 404 and path.startswith("/me/player"):
      raise NoActiveDevice("no active Spotify device", 404)
    if r.status_code == 403:
      try:
        reason = (r.json().get("error") or {}).get("reason", "")
      except ValueError:
        reason = ""
      if reason == "PREMIUM_REQUIRED":
        raise PremiumRequired("Spotify Premium required", 403)
      raise SpotifyError(f"forbidden: {reason or r.text[:100]}", 403)
    if r.status_code == 401:
      raise AuthError("Spotify rejected the login")
    if r.status_code >= 400:
      raise SpotifyError(f"{method} {path} failed ({r.status_code})", r.status_code)
    return r

  def player(self) -> Player | None:
    r = self._request("GET", "/me/player")
    if r.status_code == 204 or not r.content:
      return None  # nothing playing on any device
    return parse_player(r.json())

  def play(self) -> None:
    self._request("PUT", "/me/player/play")

  def pause(self) -> None:
    self._request("PUT", "/me/player/pause")

  def next(self) -> None:
    self._request("POST", "/me/player/next")

  def previous(self) -> None:
    self._request("POST", "/me/player/previous")

  def set_volume(self, volume: float) -> None:
    self._request("PUT", "/me/player/volume", params={"volume_percent": round(max(0.0, min(1.0, volume)) * 100)})
