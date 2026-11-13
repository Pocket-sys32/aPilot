"""Watch-style honeycomb launcher: circular icons on a hex grid that shrink toward
the edges, pan freely in 2D with momentum, and settle with the nearest icon in focus.

Animations:
- arrival: icons start enlarged and transparent, then spring out to their resting size,
  each ring trailing the one inside it, so the hive "blooms" from the center
- launch: the tapped icon glides to the center and grows to fill the screen in its color
  while its neighbors fly outward; on_launch fires once the screen is covered
- return: the reverse, springing back out of the app's icon (call begin_return())
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

import raylib_nb as rl

from openpilot.system.ui.lib.application import gui_app, FontWeight, FONT_SCALE, MousePos
from openpilot.system.ui.lib.honeycomb import (FisheyeParams, spiral_hex_coords, hex_to_world, hex_ring, fisheye, nearest_point,
                                               bounds, spring_response, ease_in_out)
from openpilot.system.ui.lib.scroll_panel2 import MIN_DRAG_PIXELS, MIN_VELOCITY_FOR_CLICKING, ScrollState
from openpilot.system.ui.lib.scroll_panel_2d import GuiScrollPanel2D
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

GlyphDrawer = Callable[[float, float, float, rl.Color], None]  # (cx, cy, radius, color)

# arrival: zoom out from ENTRY_ZOOM to 1 on a slightly bouncy spring, outer rings delayed
ENTRY_ZOOM = 2.4
ENTRY_ZETA = 0.55
ENTRY_PERIOD = 0.5  # s
ENTRY_STAGGER = 0.035  # s per ring
# launch: ease into the icon, handing off to the app as soon as the icon covers the screen
LAUNCH_TIME = 0.3  # s
LAUNCH_HANDOFF = 0.95  # zoom progress at which the screen is covered
# springs settle with a slight undershoot; express it as a gentle scale dip rather than in zoom units
UNDERSHOOT_SCALE = 0.6
# return: spring back out of the app's icon, a little calmer than arrival
RETURN_ZETA = 0.72
RETURN_PERIOD = 0.55  # s
SETTLE = 0.01  # spring displacement treated as finished

# not rendered for longer than this -> the page was away, replay the arrival next time it's seen
AWAY_TIME = 0.1  # s


@dataclass
class HoneycombItem:
  id: str
  name: str
  color: rl.Color
  glyph: str | GlyphDrawer = ""
  icon: str | None = None  # asset path, drawn instead of glyph when set
  glyph_color: rl.Color = rl.WHITE


@dataclass
class _Placed:
  item: HoneycombItem
  wx: float  # world position
  wy: float
  ring: int = 0
  # resting layout (pan + fisheye)
  sx: float = 0.0
  sy: float = 0.0
  scale: float = 1.0
  alpha: float = 1.0
  # what's actually drawn this frame (resting layout + animation)
  dx: float = 0.0
  dy: float = 0.0
  dscale: float = 1.0
  dalpha: float = 1.0
  glyph_alpha: float = 1.0


class _Anim(Enum):
  IDLE = 0
  ENTERING = 1  # arrival zoom; waits for the page to be half visible before starting
  LAUNCHING = 2  # zooming into the tapped icon
  LAUNCHED = 3  # holding the zoomed-in pose while the app is shown
  RETURNING = 4  # springing back out of the app's icon


class HoneycombLauncher(Widget):
  def __init__(self, items: Sequence[HoneycombItem], on_launch: Callable[[HoneycombItem], None] | None = None,
               edge_zone: float = 0.0):
    """edge_zone: px at the left/right edges where presses are ignored, leaving those swipes to a parent pager."""
    super().__init__()
    big = gui_app.big_ui()
    self._diameter = 230.0 if big else 86.0
    self._spacing = self._diameter * 1.2
    self._label_size = 56 if big else 26
    self._fisheye = FisheyeParams()
    self._edge_zone = edge_zone
    self._on_launch = on_launch

    self._panel = GuiScrollPanel2D()
    self._panel.set_enabled(lambda: self.enabled)

    self._font = gui_app.font(FontWeight.BOLD)
    self._label_font = gui_app.font(FontWeight.MEDIUM)
    self._placed: list[_Placed] = []
    self._points: list[tuple[float, float]] = []
    self._offset_bounds = (0.0, 0.0, 0.0, 0.0)
    self._textures: dict[str, rl.Texture] = {}
    self.set_items(items)

    self._press_pos: MousePos | None = None
    self._press_index: int | None = None
    self._focus_index = 0
    self._label_alpha = 0.0

    self._anim = _Anim.ENTERING
    self._anim_t0: float | None = None  # None while ENTERING waits for the page to show
    self._anim_index = 0  # icon being launched / returned to
    self._launch_fired = False
    self._last_update_t: float | None = None

  # ***** public API *****

  def set_items(self, items: Sequence[HoneycombItem]) -> None:
    coords = spiral_hex_coords(len(items))
    self._placed = [_Placed(item, *hex_to_world(q, r, self._spacing), ring=hex_ring(q, r))
                    for item, (q, r) in zip(items, coords, strict=True)]
    self._points = [(p.wx, p.wy) for p in self._placed]
    min_x, max_x, min_y, max_y = bounds(self._points)
    # any icon can be brought to the focal point
    self._offset_bounds = (-max_x, -min_x, -max_y, -min_y)
    for p in self._placed:
      if p.item.icon and p.item.icon not in self._textures:
        size = int(self._diameter * 0.6)
        self._textures[p.item.icon] = gui_app.texture(p.item.icon, size, size)

  def set_launch_callback(self, on_launch: Callable[[HoneycombItem], None] | None) -> None:
    self._on_launch = on_launch

  @property
  def focused_item(self) -> HoneycombItem | None:
    return self._placed[self._focus_index].item if self._placed else None

  def is_capturing_touch(self) -> bool:
    """True while a touch that belongs to the honeycomb is active. Safe to call before this widget
    renders in a frame (e.g. from a parent pager), since it also peeks at this frame's new presses."""
    if self._panel.state in (ScrollState.PRESSED, ScrollState.MANUAL_SCROLL):
      return True
    return any(ev.slot == 0 and ev.left_pressed and self._accepts_press(ev.pos) for ev in gui_app.mouse_events)

  def begin_return(self) -> None:
    """Call as the launched app starts closing: the hive springs back out of its icon."""
    if self._anim in (_Anim.LAUNCHING, _Anim.LAUNCHED):
      self._set_anim(_Anim.RETURNING, rl.get_time())

  # ***** animation state *****

  def _set_anim(self, anim: _Anim, t0: float | None) -> None:
    self._anim, self._anim_t0 = anim, t0

  def _start_entry(self) -> None:
    self._set_anim(_Anim.ENTERING, None)

  def _input_blocked(self) -> bool:
    # no taps or pans while zooming into / out of an app
    if self._anim in (_Anim.LAUNCHING, _Anim.LAUNCHED):
      return True
    return self._anim == _Anim.RETURNING and self._elapsed() < RETURN_PERIOD * 0.4

  def _elapsed(self) -> float:
    return 0.0 if self._anim_t0 is None else rl.get_time() - self._anim_t0

  def _half_visible(self) -> bool:
    return self._parent_rect is None or abs(self._rect.x - self._parent_rect.x) <= self._rect.width / 2

  def _update_anim(self) -> None:
    now = rl.get_time()
    away = self._last_update_t is None or now - self._last_update_t > AWAY_TIME
    self._last_update_t = now
    # coming back after being off screen replays the arrival (unless returning from an app)
    if away and self._anim != _Anim.RETURNING:
      self._start_entry()

    if self._anim == _Anim.ENTERING:
      if self._anim_t0 is None:
        if self._half_visible():
          self._anim_t0 = now
      else:
        outer = max((p.ring for p in self._placed), default=0)
        if abs(spring_response(self._elapsed() - outer * ENTRY_STAGGER, ENTRY_ZETA, ENTRY_PERIOD)) < SETTLE and \
           self._elapsed() > ENTRY_PERIOD:
          self._set_anim(_Anim.IDLE, None)

    elif self._anim == _Anim.LAUNCHING:
      if not self._launch_fired and self._zoom_progress() >= LAUNCH_HANDOFF:
        # the app takes over from the solid color while the zoom finishes underneath it
        self._launch_fired = True
        if self._on_launch is not None:
          self._on_launch(self._placed[self._anim_index].item)
      if self._anim == _Anim.LAUNCHING and self._elapsed() >= LAUNCH_TIME:
        self._set_anim(_Anim.LAUNCHED, None)

    elif self._anim == _Anim.LAUNCHED:
      # safety net for launches that don't call begin_return (e.g. settings): once nothing covers
      # us anymore, zoom back out. Under a pushed widget the pager makes our touch invalid.
      if self._touch_valid() and self._in_view():
        self.begin_return()

    elif self._anim == _Anim.RETURNING:
      if self._elapsed() > RETURN_PERIOD and abs(spring_response(self._elapsed(), RETURN_ZETA, RETURN_PERIOD)) < SETTLE:
        self._set_anim(_Anim.IDLE, None)

  # ***** input *****

  def _in_view(self) -> bool:
    # inside a pager, only take input once this page has snapped into place
    return self._parent_rect is None or abs(self._rect.x - self._parent_rect.x) < 1

  def _accepts_press(self, pos: MousePos) -> bool:
    if not (self.is_visible and self.enabled and self._in_view() and self._touch_valid()) or self._input_blocked():
      return False
    if not rl.check_collision_point_rec(rl.Vector2(pos.x, pos.y), self._rect):
      return False
    rel_x = pos.x - self._rect.x
    return self._edge_zone <= rel_x <= self._rect.width - self._edge_zone

  def _hit_test(self, pos: MousePos) -> int | None:
    # largest (front-most) first, using where icons are actually drawn
    for i in sorted(range(len(self._placed)), key=lambda i: -self._placed[i].dscale):
      p = self._placed[i]
      if p.dalpha < 0.3:
        continue
      r = self._diameter / 2 * p.dscale
      if (pos.x - p.dx) ** 2 + (pos.y - p.dy) ** 2 <= r * r:
        return i
    return None

  def _handle_mouse_press(self, mouse_pos: MousePos) -> None:
    # a press that catches a fast fling only stops it (panel flips to MANUAL_SCROLL)
    if self._accepts_press(mouse_pos) and self._panel.is_touch_valid() and self._panel.speed <= MIN_VELOCITY_FOR_CLICKING:
      self._press_pos = mouse_pos
      self._press_index = self._hit_test(mouse_pos)
    else:
      self._press_pos = self._press_index = None

  def _handle_mouse_release(self, mouse_pos: MousePos) -> None:
    press_pos, press_index = self._press_pos, self._press_index
    self._press_pos = self._press_index = None
    if press_pos is None or press_index is None or self._input_blocked():
      return
    moved = ((mouse_pos.x - press_pos.x) ** 2 + (mouse_pos.y - press_pos.y) ** 2) ** 0.5
    if moved > MIN_DRAG_PIXELS or self._hit_test(mouse_pos) != press_index:
      return
    self._panel.stop()
    self._anim_index = press_index
    self._launch_fired = False
    self._label_alpha = 0.0
    self._set_anim(_Anim.LAUNCHING, rl.get_time())

  # ***** layout *****

  def show_event(self):
    super().show_event()
    if self._anim == _Anim.RETURNING:
      # keep where the user was so we zoom back out of the right icon
      return
    self._recenter()
    self._start_entry()

  def _recenter(self) -> None:
    self._panel.stop()
    self._panel.set_offset(0.0, 0.0)
    self._press_pos = self._press_index = None

  def _update_state(self):
    self._update_anim()

    # while a pager slides this page in (or it's off screen), hold the hive centered on the
    # first app so it always arrives in the same spot; it only moves once fully in view
    if not self._in_view():
      self._recenter()

    ox, oy = self._panel.offset
    focus = nearest_point(self._points, ox, oy)
    snap = (-self._points[focus][0], -self._points[focus][1]) if focus >= 0 else None
    ox, oy = self._panel.update(self._offset_bounds, snap_target=snap, accept_press=self._accepts_press)

    # dragging cancels a pending tap
    if self._panel.state == ScrollState.MANUAL_SCROLL:
      self._press_index = None

    self._focus_index = max(0, nearest_point(self._points, ox, oy))

  def _layout(self):
    cx = self._rect.x + self._rect.width / 2
    cy = self._rect.y + self._rect.height / 2
    half_w, half_h = self._rect.width / 2, self._rect.height / 2
    ox, oy = self._panel.offset
    for p in self._placed:
      dx, dy, p.scale, p.alpha = fisheye(p.wx + ox, p.wy + oy, half_w, half_h, self._fisheye)
      p.sx, p.sy = cx + dx, cy + dy
      p.dx, p.dy, p.dscale, p.dalpha, p.glyph_alpha = p.sx, p.sy, p.scale, p.alpha, 1.0

    if self._anim == _Anim.ENTERING:
      self._layout_entry(cx, cy)
    elif self._anim in (_Anim.LAUNCHING, _Anim.LAUNCHED, _Anim.RETURNING):
      self._layout_zoom(cx, cy, self._zoom_progress())

  def _layout_entry(self, cx: float, cy: float) -> None:
    started = self._anim_t0 is not None
    for p in self._placed:
      # each ring starts a beat after the one inside it; spring_response holds 1 until then
      d = spring_response(self._elapsed() - p.ring * ENTRY_STAGGER, ENTRY_ZETA, ENTRY_PERIOD) if started else 1.0
      z = 1.0 + (ENTRY_ZOOM - 1.0) * d if d >= 0 else 1.0 + d * UNDERSHOOT_SCALE
      p.dx, p.dy = cx + (p.sx - cx) * z, cy + (p.sy - cy) * z
      p.dscale = p.scale * z
      p.dalpha = p.alpha * max(0.0, min(1.0, 1.0 - d)) ** 0.6

  def _zoom_progress(self) -> float:
    """0 = resting hive, 1 = launched icon fills the screen."""
    if self._anim == _Anim.LAUNCHING:
      return ease_in_out(self._elapsed() / LAUNCH_TIME)
    if self._anim == _Anim.LAUNCHED:
      return 1.0
    # may dip slightly below 0 at the end: a soft bounce
    return spring_response(self._elapsed(), RETURN_ZETA, RETURN_PERIOD)

  def _layout_zoom(self, cx: float, cy: float, u: float) -> None:
    target = self._placed[self._anim_index]
    # zoom until the launched icon covers the whole screen
    cover = 1.08 * math.hypot(self._rect.width / 2, self._rect.height / 2) / max(1.0, self._diameter / 2 * target.scale)
    z = 1.0 + (cover - 1.0) * u if u >= 0 else 1.0 + u * UNDERSHOOT_SCALE
    # the launched icon glides to the center while everything scales around it
    m = max(0.0, u)
    fx = target.sx + (cx - target.sx) * m
    fy = target.sy + (cy - target.sy) * m
    for p in self._placed:
      p.dx, p.dy = fx + (p.sx - target.sx) * z, fy + (p.sy - target.sy) * z
      p.dscale = p.scale * z
    target.dalpha = 1.0
    # the glyph dissolves as the icon becomes the app's backdrop
    target.glyph_alpha = max(0.0, min(1.0, 1.0 - m * 1.4))

  # ***** render *****

  def _render(self, rect: rl.Rectangle):
    rl.begin_scissor_mode(int(rect.x), int(rect.y), int(rect.width), int(rect.height))

    zooming = self._anim in (_Anim.LAUNCHING, _Anim.LAUNCHED, _Anim.RETURNING)
    order = sorted(range(len(self._placed)), key=lambda i: self._placed[i].dscale)
    if zooming:
      # the launched icon is always drawn on top
      order.remove(self._anim_index)
      order.append(self._anim_index)
    for i in order:
      p = self._placed[i]
      if p.dalpha <= 0.01:
        continue
      pressed = i == self._press_index and self.is_pressed
      self._draw_icon(p, pressed)

    # name of the focused app fades in once settled
    settled = self._panel.state == ScrollState.STEADY and self._anim == _Anim.IDLE
    if zooming:
      self._label_alpha = 0.0
    self._label_alpha += ((1.0 if settled and self._placed else 0.0) - self._label_alpha) * 0.2
    if self._label_alpha > 0.02 and self._placed:
      self._draw_label(self._placed[self._focus_index].item.name, rect, self._label_alpha)

    rl.end_scissor_mode()

  def _draw_icon(self, p: _Placed, pressed: bool) -> None:
    k = p.dscale * (0.92 if pressed else 1.0)
    r = self._diameter / 2 * k
    a = p.dalpha * (0.7 if pressed else 1.0)
    center = rl.Vector2(p.dx, p.dy)
    c = p.item.color
    rl.draw_circle_v(center, r, rl.Color(c.r, c.g, c.b, int(c.a * a)))

    ga = a * p.glyph_alpha
    if ga <= 0.01:
      return
    gc = p.item.glyph_color
    glyph_color = rl.Color(gc.r, gc.g, gc.b, int(gc.a * ga))
    if p.item.icon:
      tex = self._textures[p.item.icon]
      s = (r * 1.2) / max(tex.width, tex.height)
      rl.draw_texture_ex(tex, rl.Vector2(p.dx - tex.width * s / 2, p.dy - tex.height * s / 2), 0.0, s, glyph_color)
    elif callable(p.item.glyph):
      p.item.glyph(p.dx, p.dy, r, glyph_color)
    elif p.item.glyph:
      base = int(self._diameter * 0.45)
      size = measure_text_cached(self._font, p.item.glyph, base)
      rl.draw_text_ex(self._font, p.item.glyph, rl.Vector2(p.dx - size.x * k / 2, p.dy - size.y * k / 2),
                      base * FONT_SCALE * k, 0, glyph_color)

  def _draw_label(self, text: str, rect: rl.Rectangle, alpha: float) -> None:
    size = measure_text_cached(self._label_font, text, self._label_size)
    pad_x, pad_y = self._label_size * 0.6, self._label_size * 0.2
    margin = self._label_size * 0.4
    pill = rl.Rectangle(rect.x + margin + self._edge_zone, rect.y + margin, size.x + pad_x * 2, size.y + pad_y * 2)
    rl.draw_rectangle_rounded(pill, 1.0, 12, rl.Color(0, 0, 0, int(170 * alpha)))
    rl.draw_text_ex(self._label_font, text, rl.Vector2(pill.x + pad_x, pill.y + pad_y),
                    self._label_size * FONT_SCALE, 0, rl.Color(255, 255, 255, int(230 * alpha)))
