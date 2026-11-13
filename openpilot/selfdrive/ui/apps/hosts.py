"""Per-device containers for app content widgets."""
from collections.abc import Callable

import raylib_nb as rl

from openpilot.system.ui.lib.application import gui_app, MousePos
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.nav_widget import NavWidget

# tici close button, matches settings
CLOSE_BTN_SIZE = 200
CLOSE_ICON_SIZE = 70
CLOSE_BTN_COLOR = rl.Color(41, 41, 41, 255)
CLOSE_BTN_PRESSED = rl.Color(59, 59, 59, 255)
FRAME_PADDING = 60

# the app resolves out of its icon color while scaling up slightly, picking up where the launcher's zoom ends
REVEAL_TIME = 0.3  # s
REVEAL_START_SCALE = 0.9


class AppReveal:
  """Draws app content emerging from a solid color (the launched icon filling the screen)."""

  def __init__(self):
    self._t0: float | None = None
    self._color = rl.BLACK

  def start(self, color: rl.Color) -> None:
    self._t0 = rl.get_time()
    self._color = color

  @property
  def active(self) -> bool:
    return self._t0 is not None and rl.get_time() - self._t0 < REVEAL_TIME

  def draw(self, rect: rl.Rectangle, draw_content: Callable[[], object]) -> None:
    if not self.active or self._t0 is None:
      draw_content()
      return

    x = (rl.get_time() - self._t0) / REVEAL_TIME
    ease_out = 1 - (1 - x) ** 3
    s = REVEAL_START_SCALE + (1 - REVEAL_START_SCALE) * ease_out
    cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2

    # scale content about the center of its rect
    rl.rl_push_matrix()
    rl.rl_translatef(cx, cy, 0)
    rl.rl_scalef(s, s, 1.0)
    rl.rl_translatef(-cx, -cy, 0)
    draw_content()
    rl.rl_pop_matrix()

    c = self._color
    rl.draw_rectangle_rec(rect, rl.Color(c.r, c.g, c.b, int(255 * (1 - ease_out))))


class MiciAppPage(NavWidget):
  """Full screen app on mici; swipe down from the top to go back to the launcher."""

  def __init__(self, content: Widget, color: rl.Color = rl.BLACK):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
    self._content = self._child(content)
    self._content.set_enabled(lambda: self.enabled and not self.is_dismissing and not self._reveal.active)
    self._color = color
    self._reveal = AppReveal()
    self._dismiss_start_callback: Callable[[], None] | None = None
    self._was_dismissing = False

  def set_dismiss_start_callback(self, callback: Callable[[], None] | None) -> None:
    """Fires once when the page starts going away (swipe released or programmatic dismiss)."""
    self._dismiss_start_callback = callback

  def show_event(self):
    super().show_event()
    # appear in place, zooming out of the launcher's icon instead of sliding up from the bottom
    self._y_pos_filter.x = 0.0
    self._y_pos_filter.velocity.x = 0.0
    self._was_dismissing = False
    self._reveal.start(self._color)

  def _update_state(self):
    super()._update_state()
    dismissing = self._playing_dismiss_animation
    if dismissing and not self._was_dismissing and self._dismiss_start_callback is not None:
      self._dismiss_start_callback()
    self._was_dismissing = dismissing

  def hide_event(self):
    super().hide_event()
    # instant pops (e.g. screen timeout) skip the dismiss animation; still let the launcher know
    if not self._was_dismissing and self._dismiss_start_callback is not None:
      self._dismiss_start_callback()
    self._was_dismissing = False

  def _render(self, rect: rl.Rectangle):
    self._reveal.draw(rect, lambda: self._content.render(rect))


class TiciAppFrame(Widget):
  """Hosts app content in the main content area on tici, with a close button."""

  def __init__(self, on_close: Callable[[], None]):
    super().__init__()
    self._on_close = on_close
    self._content: Widget | None = None
    self._close_icon = gui_app.texture("icons/close2.png", CLOSE_ICON_SIZE, CLOSE_ICON_SIZE)
    self._close_rect = rl.Rectangle(0, 0, 0, 0)
    self._close_down = False
    self._reveal = AppReveal()

  def set_content(self, content: Widget | None, color: rl.Color = rl.BLACK) -> None:
    if self._content is not None:
      self._content.hide_event()
    self._content = content
    if content is not None:
      content.set_enabled(lambda: self.enabled and not self._reveal.active)
      content.show_event()
      self._reveal.start(color)

  def show_event(self):
    super().show_event()
    if self._content is not None:
      self._content.show_event()

  def hide_event(self):
    super().hide_event()
    if self._content is not None:
      self._content.hide_event()

  def _hit_close(self, pos: MousePos) -> bool:
    return rl.check_collision_point_rec(rl.Vector2(pos.x, pos.y), self._close_rect)

  def _handle_mouse_press(self, mouse_pos: MousePos) -> None:
    self._close_down = self._hit_close(mouse_pos) and not self._reveal.active

  def _handle_mouse_release(self, mouse_pos: MousePos) -> None:
    super()._handle_mouse_release(mouse_pos)
    if self._close_down and self._hit_close(mouse_pos):
      self._on_close()
    self._close_down = False

  def _render(self, rect: rl.Rectangle):
    self._reveal.draw(rect, lambda: self._draw_frame(rect))

  def _draw_frame(self, rect: rl.Rectangle) -> None:
    rl.draw_rectangle_rec(rect, rl.BLACK)
    self._close_rect = rl.Rectangle(rect.x + FRAME_PADDING, rect.y + FRAME_PADDING, CLOSE_BTN_SIZE, CLOSE_BTN_SIZE)
    color = CLOSE_BTN_PRESSED if (self._close_down and self.is_pressed) else CLOSE_BTN_COLOR
    rl.draw_rectangle_rounded(self._close_rect, 1.0, 20, color)
    rl.draw_texture_v(self._close_icon, rl.Vector2(self._close_rect.x + (CLOSE_BTN_SIZE - CLOSE_ICON_SIZE) / 2,
                                                    self._close_rect.y + (CLOSE_BTN_SIZE - CLOSE_ICON_SIZE) / 2), rl.WHITE)

    if self._content is not None:
      top = self._close_rect.y + CLOSE_BTN_SIZE
      self._content.render(rl.Rectangle(rect.x + FRAME_PADDING, top, rect.width - FRAME_PADDING * 2,
                                        rect.y + rect.height - top - FRAME_PADDING))
