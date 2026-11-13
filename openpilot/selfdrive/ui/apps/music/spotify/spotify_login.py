#!/usr/bin/env python3
"""One-time Spotify login for the Music app.

1. Create an app at https://developer.spotify.com/dashboard, select "Web API", and add
   this redirect URI exactly:  http://127.0.0.1:8888/callback
2. On a PC:    python spotify_login.py --client-id <ID>
   On a comma: ssh -L 8888:127.0.0.1:8888 comma@<device-ip>
               then run the same command on the device, and open the printed link in your
               PC browser. The port forward carries Spotify's redirect back to the device.

The resulting login is saved to /data/spotify.json on device (~/.comma/spotify.json on PC),
readable only by its owner. Control needs Spotify Premium.
"""
import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

from openpilot.selfdrive.ui.apps.music.spotify.auth import (DEFAULT_PORT, AuthError, TokenStore, authorize_url, exchange_code,
                                                            make_verifier, redirect_uri)

LOGIN_TIMEOUT = 300  # s


def _wait_for_code(port: int, expected_state: str) -> str:
  result: dict[str, str] = {}
  done = threading.Event()

  class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
      url = urllib.parse.urlparse(self.path)
      if url.path != "/callback":
        self.send_error(404)
        return
      q = urllib.parse.parse_qs(url.query)
      if q.get("state", [""])[0] != expected_state:
        result["error"] = "state mismatch (possible stale or forged redirect)"
      elif "error" in q:
        result["error"] = q["error"][0]
      else:
        result["code"] = q.get("code", [""])[0]
      ok = "code" in result
      body = ("Spotify connected. You can close this tab." if ok else f"Login failed: {result.get('error')}").encode()
      self.send_response(200 if ok else 400)
      self.send_header("Content-Type", "text/plain; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
      done.set()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (matches the base signature)
      pass

  server = http.server.HTTPServer(("127.0.0.1", port), Handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    if not done.wait(LOGIN_TIMEOUT):
      raise AuthError("timed out waiting for the Spotify redirect")
  finally:
    server.shutdown()
  if "code" not in result:
    raise AuthError(result.get("error", "no code returned"))
  return result["code"]


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--client-id", help="Spotify app client ID (reuses the saved one if omitted)")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--logout", action="store_true", help="forget the saved login")
  args = parser.parse_args()

  store = TokenStore()
  if args.logout:
    store.clear()
    print("Spotify login removed.")
    return 0

  saved = store.load()
  client_id = args.client_id or (saved.client_id if saved else None)
  if not client_id:
    parser.error("--client-id is required for the first login")

  verifier, state = make_verifier(), secrets.token_urlsafe(16)
  url = authorize_url(client_id, verifier, state, args.port)
  print(f"Redirect URI (must be registered on your Spotify app): {redirect_uri(args.port)}")
  print(f"\nOpen this link to log in:\n\n  {url}\n")
  try:
    webbrowser.open(url)
  except Exception:
    pass

  try:
    code = _wait_for_code(args.port, state)
    store.save(exchange_code(client_id, code, verifier, args.port))
  except AuthError as e:
    print(f"Login failed: {e}", file=sys.stderr)
    return 1
  print(f"Spotify connected. Saved to {store.path}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
