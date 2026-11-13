"""Spotify login (Authorization Code with PKCE) and token storage.

PKCE needs only a client ID, no client secret, so nothing secret about the Spotify app
lives on the device. Spotify only allows redirects to HTTPS or a loopback IP literal
(http://127.0.0.1:PORT), so the one-time login runs through spotify_login.py with a
local callback server (over an SSH port forward when logging in a real device).
"""
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, asdict
from typing import Any, Protocol

import requests

from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = ("user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing")
DEFAULT_PORT = 8888
TIMEOUT = 10  # s
EXPIRY_MARGIN = 60  # s, refresh this long before the access token actually expires


def redirect_uri(port: int = DEFAULT_PORT) -> str:
  return f"http://127.0.0.1:{port}/callback"


def token_path() -> str:
  # /data survives reboots and updates on device; ~/.comma on a PC
  return os.path.join(Paths.comma_home() if PC else "/data", "spotify.json")


class HttpSession(Protocol):
  """The slice of requests.Session used here (lets tests pass a fake)."""
  def request(self, method: str, url: str, **kwargs: Any) -> Any: ...
  def post(self, url: str, **kwargs: Any) -> Any: ...


# Token expiry uses the wall clock on purpose: it's saved to disk and must survive reboots, which a
# monotonic clock doesn't. If the clock is off after boot, Spotify answers 401 and the client refreshes.
def _now() -> float:
  return time.time()  # noqa: TID251


# ***** PKCE (RFC 7636) *****

def make_verifier() -> str:
  return secrets.token_urlsafe(64)[:128]


def code_challenge(verifier: str) -> str:
  digest = hashlib.sha256(verifier.encode("ascii")).digest()
  return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id: str, verifier: str, state: str, port: int = DEFAULT_PORT) -> str:
  return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
    "client_id": client_id,
    "response_type": "code",
    "redirect_uri": redirect_uri(port),
    "code_challenge_method": "S256",
    "code_challenge": code_challenge(verifier),
    "scope": " ".join(SCOPES),
    "state": state,
  })


# ***** tokens *****

class AuthError(Exception):
  """Login is missing or no longer valid; the user has to run spotify_login.py again."""


@dataclass
class Tokens:
  client_id: str
  refresh_token: str
  access_token: str = ""
  expires_at: float = 0.0  # unix time

  @property
  def expired(self) -> bool:
    return not self.access_token or _now() >= self.expires_at - EXPIRY_MARGIN


def _apply_token_response(client_id: str, data: dict, previous_refresh: str = "") -> Tokens:
  # Spotify may rotate the refresh token; keep the old one if it doesn't send a new one
  return Tokens(client_id=client_id,
                refresh_token=data.get("refresh_token") or previous_refresh,
                access_token=data["access_token"],
                expires_at=_now() + float(data.get("expires_in", 3600)))


def exchange_code(client_id: str, code: str, verifier: str, port: int = DEFAULT_PORT,
                  session: HttpSession | None = None) -> Tokens:
  r = (session or requests).post(TOKEN_URL, timeout=TIMEOUT, data={
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": redirect_uri(port),
    "client_id": client_id,
    "code_verifier": verifier,
  })
  if r.status_code != 200:
    raise AuthError(f"code exchange failed ({r.status_code}): {r.text[:200]}")
  return _apply_token_response(client_id, r.json())


def refresh(tokens: Tokens, session: HttpSession | None = None) -> Tokens:
  r = (session or requests).post(TOKEN_URL, timeout=TIMEOUT, data={
    "grant_type": "refresh_token",
    "refresh_token": tokens.refresh_token,
    "client_id": tokens.client_id,
  })
  if r.status_code in (400, 401):
    # revoked, expired or rotated refresh token: needs a fresh login
    raise AuthError(f"refresh rejected ({r.status_code})")
  r.raise_for_status()
  return _apply_token_response(tokens.client_id, r.json(), tokens.refresh_token)


class TokenStore:
  """Tokens in a small JSON file readable only by the owner (0600), written atomically."""

  def __init__(self, path: str | None = None):
    self.path = path or token_path()

  def load(self) -> Tokens | None:
    try:
      with open(self.path) as f:
        data = json.load(f)
      return Tokens(**data) if data.get("client_id") and data.get("refresh_token") else None
    except (OSError, ValueError, TypeError):
      return None

  def save(self, tokens: Tokens) -> None:
    directory = os.path.dirname(self.path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".spotify-")
    try:
      os.fchmod(fd, 0o600)
      with os.fdopen(fd, "w") as f:
        json.dump(asdict(tokens), f)
      os.replace(tmp, self.path)
    except BaseException:
      if os.path.exists(tmp):
        os.unlink(tmp)
      raise

  def clear(self) -> None:
    try:
      os.unlink(self.path)
    except FileNotFoundError:
      pass
