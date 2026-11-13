import json
import os
import stat
import tempfile
import unittest
import urllib.parse

from openpilot.selfdrive.ui.apps.music.spotify import auth
from openpilot.selfdrive.ui.apps.music.spotify.auth import Tokens, TokenStore, _now
from openpilot.selfdrive.ui.apps.music.spotify.backend import (SpotifyMusicBackend, NOT_SET_UP, PREMIUM, NOTHING_PLAYING, OFFLINE,
                                                               LOGIN_EXPIRED)
from openpilot.selfdrive.ui.apps.music.spotify.client import SpotifyClient

PLAYER = {
  "is_playing": True,
  "progress_ms": 30_000,
  "device": {"name": "Pixel 8", "volume_percent": 40},
  "item": {"type": "track", "name": "Song", "duration_ms": 200_000,
           "artists": [{"name": "A"}, {"name": "B"}], "album": {"name": "Album"}},
}


class FakeResponse:
  def __init__(self, status=200, body=None, headers=None):
    self.status_code = status
    self._body = body
    self.headers = headers or {}
    self.content = b"" if body is None else json.dumps(body).encode()
    self.text = self.content.decode()

  def json(self):
    if self._body is None:
      raise ValueError("no body")
    return self._body

  def raise_for_status(self):
    if self.status_code >= 400:
      raise RuntimeError(self.status_code)


class FakeSession:
  """Answers requests from a queue of (method, path-suffix) -> FakeResponse rules and records calls."""

  def __init__(self):
    self.routes: dict[tuple[str, str], list[FakeResponse]] = {}
    self.calls: list[tuple[str, str, dict]] = []

  def on(self, method, path, *responses):
    self.routes.setdefault((method, path), []).extend(responses)

  def _answer(self, method, url, kwargs):
    path = urllib.parse.urlparse(url).path
    self.calls.append((method, path, kwargs))
    queue = self.routes.get((method, path))
    if not queue:
      raise AssertionError(f"unexpected {method} {path}")
    return queue.pop(0) if len(queue) > 1 else queue[0]

  def request(self, method, url, **kwargs):
    return self._answer(method, url, kwargs)

  def post(self, url, **kwargs):
    return self._answer("POST", url, kwargs)


def _store(tmp, tokens: Tokens | None):
  store = TokenStore(os.path.join(tmp, "spotify.json"))
  if tokens:
    store.save(tokens)
  return store


def _fresh():
  return Tokens("cid", "refresh-1", "access-1", _now() + 3600)


class TestAuth(unittest.TestCase):
  def test_pkce_rfc7636_vector(self):
    self.assertEqual(auth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                     "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    v = auth.make_verifier()
    self.assertTrue(43 <= len(v) <= 128)

  def test_authorize_url(self):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(auth.authorize_url("cid", "v" * 50, "st", 9999)).query)
    self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:9999/callback"])
    self.assertEqual(q["code_challenge_method"], ["S256"])
    self.assertEqual(q["state"], ["st"])
    self.assertIn("user-modify-playback-state", q["scope"][0])

  def test_store_roundtrip_private(self):
    with tempfile.TemporaryDirectory() as tmp:
      tokens = _fresh()
      store = _store(tmp, tokens)
      self.assertEqual(store.load(), tokens)
      self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
      store.clear()
      self.assertIsNone(store.load())

  def test_refresh_keeps_old_refresh_token_unless_rotated(self):
    s = FakeSession()
    s.on("POST", "/api/token", FakeResponse(200, {"access_token": "a2", "expires_in": 3600}),
         FakeResponse(200, {"access_token": "a3", "expires_in": 3600, "refresh_token": "refresh-2"}))
    t = auth.refresh(_fresh(), s)
    self.assertEqual((t.access_token, t.refresh_token), ("a2", "refresh-1"))
    t = auth.refresh(t, s)
    self.assertEqual((t.access_token, t.refresh_token), ("a3", "refresh-2"))


class TestClient(unittest.TestCase):
  def test_parse_and_204(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(200, PLAYER), FakeResponse(204))
      c = SpotifyClient(_store(tmp, _fresh()), s)
      p = c.player()
      assert p is not None
      self.assertEqual((p.title, p.artist, p.album, p.device), ("Song", "A, B", "Album", "Pixel 8"))
      self.assertEqual((p.progress_s, p.duration_s, p.volume, p.is_playing), (30.0, 200.0, 0.4, True))
      self.assertIsNone(c.player())

  def test_401_refreshes_once_and_saves(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("PUT", "/v1/me/player/pause", FakeResponse(401), FakeResponse(204))
      s.on("POST", "/api/token", FakeResponse(200, {"access_token": "access-2", "expires_in": 3600}))
      store = _store(tmp, _fresh())
      SpotifyClient(store, s).pause()
      self.assertEqual([c[1] for c in s.calls], ["/v1/me/player/pause", "/api/token", "/v1/me/player/pause"])
      self.assertEqual(s.calls[-1][2]["headers"]["Authorization"], "Bearer access-2")
      loaded = store.load()
      assert loaded is not None
      self.assertEqual(loaded.access_token, "access-2")

  def test_expired_token_refreshed_before_request(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("POST", "/api/token", FakeResponse(200, {"access_token": "access-2", "expires_in": 3600}))
      s.on("POST", "/v1/me/player/next", FakeResponse(204))
      SpotifyClient(_store(tmp, Tokens("cid", "refresh-1", "old", 0)), s).next()
      self.assertEqual([c[1] for c in s.calls], ["/api/token", "/v1/me/player/next"])


class TestBackend(unittest.TestCase):
  def _backend(self, tmp, session, tokens=True):
    self.now = 100.0
    client = SpotifyClient(_store(tmp, _fresh() if tokens else None), session)
    return SpotifyMusicBackend(client, clock=lambda: self.now, start_thread=False)

  def test_not_set_up(self):
    with tempfile.TemporaryDirectory() as tmp:
      b = self._backend(tmp, FakeSession(), tokens=False)
      b.state()
      b.step()
      self.assertEqual(b.state().status, NOT_SET_UP)
      self.assertFalse(b.state().available)

  def test_poll_and_interpolate_progress(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(200, PLAYER))
      b = self._backend(tmp, s)
      b.state()
      b.step()
      st = b.state()
      self.assertTrue(st.available)
      self.assertEqual((st.title, st.device, st.progress_s), ("Song", "Pixel 8", 30.0))
      self.now += 2.0
      self.assertAlmostEqual(b.state().progress_s, 32.0)  # bar keeps moving between polls

  def test_only_polls_while_screen_open(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(200, PLAYER))
      b = self._backend(tmp, s)
      b.step()  # UI never read state -> no network
      self.assertEqual(s.calls, [])
      b.state()
      b.step()
      self.assertEqual(len(s.calls), 1)
      self.now += 10  # screen closed for a while
      b.step()
      self.assertEqual(len(s.calls), 1)

  def test_play_pause_is_optimistic(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(200, PLAYER))
      s.on("PUT", "/v1/me/player/pause", FakeResponse(204))
      b = self._backend(tmp, s)
      b.state()
      b.step()
      b.play_pause()
      self.assertFalse(b.state().is_playing)  # flips before the network call
      b.step(("pause", None))
      self.assertIn(("PUT", "/v1/me/player/pause"), [(c[0], c[1]) for c in s.calls])

  def test_error_statuses(self):
    cases = [
      (FakeResponse(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}}), PREMIUM),
      (FakeResponse(204), NOTHING_PLAYING),
      (FakeResponse(500), OFFLINE),
    ]
    for resp, status in cases:
      with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
        s = FakeSession()
        s.on("GET", "/v1/me/player", resp)
        b = self._backend(tmp, s)
        b.state()
        b.step()
        self.assertEqual(b.state().status, status)
        self.assertFalse(b.state().available)

  def test_revoked_login(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(401))
      s.on("POST", "/api/token", FakeResponse(400, {"error": "invalid_grant"}))
      b = self._backend(tmp, s)
      b.state()
      b.step()
      self.assertEqual(b.state().status, LOGIN_EXPIRED)

  def test_rate_limit_backs_off(self):
    with tempfile.TemporaryDirectory() as tmp:
      s = FakeSession()
      s.on("GET", "/v1/me/player", FakeResponse(429, headers={"Retry-After": "30"}))
      b = self._backend(tmp, s)
      b.state()
      b.step()
      self.now += 5
      b.state()
      b.step()
      self.assertEqual(len(s.calls), 1)  # waited instead of hammering the API


if __name__ == "__main__":
  unittest.main()
