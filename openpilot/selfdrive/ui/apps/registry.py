"""The set of apps shown in the honeycomb launcher. Order matters: the first app
sits at the center, the next six form the first ring, and so on."""
from collections.abc import Callable
from dataclasses import dataclass

import raylib_nb as rl

from openpilot.selfdrive.ui.apps import glyphs
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.honeycomb import HoneycombItem


@dataclass
class AppSpec(HoneycombItem):
  create: Callable[[], Widget] | None = None  # builds the app's content widget (hosted per device)
  action: Callable[[], None] | None = None  # custom launch instead of hosting content, e.g. settings


def _music() -> Widget:
  from openpilot.selfdrive.ui.apps.music.music_app import MusicPlayerView
  return MusicPlayerView()


def _placeholder(name: str) -> Callable[[], Widget]:
  def create() -> Widget:
    from openpilot.selfdrive.ui.apps.placeholder_app import PlaceholderApp
    return PlaceholderApp(name)
  return create


def build_apps(open_settings: Callable[[], None]) -> list[AppSpec]:
  return [
    AppSpec("music", "Music", rl.Color(30, 215, 96, 255), glyphs.music_note, glyph_color=rl.BLACK, create=_music),
    AppSpec("settings", "Settings", rl.Color(90, 90, 96, 255), icon="icons_mici/settings.png", action=open_settings),
    AppSpec("maps", "Maps", rl.Color(52, 120, 246, 255), glyphs.map_pin, create=_placeholder("Maps")),
    AppSpec("weather", "Weather", rl.Color(255, 159, 10, 255), glyphs.sun, create=_placeholder("Weather")),
    AppSpec("timer", "Timer", rl.Color(255, 69, 58, 255), glyphs.clock, create=_placeholder("Timer")),
    AppSpec("stats", "Drive Stats", rl.Color(175, 82, 222, 255), glyphs.bars, create=_placeholder("Drive Stats")),
    AppSpec("phone", "Phone", rl.Color(48, 176, 199, 255), glyphs.phone, create=_placeholder("Phone")),
  ]


class AppContentCache:
  """Creates each app's content widget once and reuses it across launches."""

  def __init__(self):
    self._content: dict[str, Widget] = {}

  def get(self, app: AppSpec) -> Widget | None:
    if app.create is None:
      return None
    if app.id not in self._content:
      self._content[app.id] = app.create()
    return self._content[app.id]
