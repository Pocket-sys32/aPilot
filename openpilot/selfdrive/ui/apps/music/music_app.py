import raylib_nb as rl

from openpilot.selfdrive.ui.apps import glyphs
from openpilot.selfdrive.ui.apps.music.backend import MusicBackend, MockMusicBackend
from openpilot.system.ui.lib.application import FontWeight, MousePos, TextAlignment
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import gui_label

# designed at mici size (536x240) and scaled up for larger screens
BASE_W, BASE_H = 536.0, 240.0
MAX_SCALE = 2.5

ACCENT = rl.Color(30, 215, 96, 255)
BTN_COLOR = rl.Color(45, 45, 45, 255)
BTN_PRESSED = rl.Color(75, 75, 75, 255)
TRACK_BG = rl.Color(255, 255, 255, 50)
SUBTLE = rl.Color(170, 170, 170, 255)


def _fmt_time(seconds: float) -> str:
  s = max(0, int(seconds))
  return f"{s // 60}:{s % 60:02d}"


class MusicPlayerView(Widget):
  """Now-playing controller: track info, progress, prev / play-pause / next."""

  def __init__(self, backend: MusicBackend | None = None):
    super().__init__()
    self._backend: MusicBackend = backend or MockMusicBackend()
    self._state = self._backend.state()
    self._buttons: list[tuple[rl.Vector2, float]] = []  # (center, radius) for prev, play, next
    self._pressed_btn: int | None = None

  def _update_state(self):
    self._state = self._backend.state()

  def _scale(self) -> float:
    return min(self._rect.width / BASE_W, self._rect.height / BASE_H, MAX_SCALE)

  def _hit(self, pos: MousePos) -> int | None:
    for i, (c, r) in enumerate(self._buttons):
      if (pos.x - c.x) ** 2 + (pos.y - c.y) ** 2 <= (r * 1.15) ** 2:
        return i
    return None

  def _handle_mouse_press(self, mouse_pos: MousePos) -> None:
    self._pressed_btn = self._hit(mouse_pos)

  def _handle_mouse_release(self, mouse_pos: MousePos) -> None:
    super()._handle_mouse_release(mouse_pos)
    btn, self._pressed_btn = self._pressed_btn, None
    if btn is None or btn != self._hit(mouse_pos):
      return
    (self._backend.previous, self._backend.play_pause, self._backend.next)[btn]()
    self._state = self._backend.state()

  def _render(self, rect: rl.Rectangle):
    k = self._scale()
    w = BASE_W * k
    x0 = rect.x + (rect.width - w) / 2
    y0 = rect.y + (rect.height - BASE_H * k) / 2
    pad = 24 * k
    st = self._state

    title = st.title if st.available else "Nothing playing"
    subtitle = st.artist if st.available else "Connect a music source"
    gui_label(rl.Rectangle(x0 + pad, y0 + 22 * k, w - pad * 2, 46 * k), title, int(40 * k), rl.WHITE, FontWeight.BOLD)
    gui_label(rl.Rectangle(x0 + pad, y0 + 66 * k, w - pad * 2, 32 * k), subtitle, int(28 * k), SUBTLE, FontWeight.MEDIUM)

    # progress
    bar_y = y0 + 112 * k
    bar = rl.Rectangle(x0 + pad, bar_y, w - pad * 2, 6 * k)
    rl.draw_rectangle_rounded(bar, 1.0, 8, TRACK_BG)
    frac = (st.progress_s / st.duration_s) if st.duration_s > 0 else 0.0
    if frac > 0:
      rl.draw_rectangle_rounded(rl.Rectangle(bar.x, bar.y, bar.width * min(1.0, frac), bar.height), 1.0, 8, ACCENT)
    if st.available:
      time_rect = rl.Rectangle(bar.x, bar_y + 10 * k, bar.width, 24 * k)
      gui_label(time_rect, _fmt_time(st.progress_s), int(20 * k), SUBTLE, FontWeight.MEDIUM)
      gui_label(time_rect, _fmt_time(st.duration_s), int(20 * k), SUBTLE, FontWeight.MEDIUM, alignment=TextAlignment.RIGHT)

    # controls
    cy = y0 + 186 * k
    cx = x0 + w / 2
    side_r, main_r = 34 * k, 42 * k
    gap = 110 * k
    self._buttons = [(rl.Vector2(cx - gap, cy), side_r), (rl.Vector2(cx, cy), main_r), (rl.Vector2(cx + gap, cy), side_r)]
    for i, (c, r) in enumerate(self._buttons):
      pressed = i == self._pressed_btn and self.is_pressed
      fill = ACCENT if i == 1 else BTN_COLOR
      if pressed:
        fill = rl.Color(fill.r * 3 // 4, fill.g * 3 // 4, fill.b * 3 // 4, 255) if i == 1 else BTN_PRESSED
      rl.draw_circle_v(c, r, fill)
      glyph_color = rl.BLACK if i == 1 else rl.WHITE
      if i == 0:
        glyphs.skip(c.x, c.y, r, glyph_color, forward=False)
      elif i == 1:
        (glyphs.pause if st.is_playing else glyphs.play)(c.x, c.y, r, glyph_color)
      else:
        glyphs.skip(c.x, c.y, r, glyph_color, forward=True)
