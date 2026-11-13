"""Free 2D panning with momentum, rubber-banding and settle-snap.

Mirrors GuiScrollPanel2's state machine but on both axes at once.
"""
import math
from collections import deque
from collections.abc import Callable

import raylib_nb as rl

from openpilot.system.ui.lib.application import gui_app, MouseEvent, MousePos
from openpilot.system.ui.lib.scroll_panel2 import (ScrollState, fit_velocity, MIN_VELOCITY, MIN_VELOCITY_FOR_CLICKING,
                                                   MIN_DRAG_PIXELS, AUTO_SCROLL_TC, BOUNCE_RETURN_RATE, SNAP_RATE,
                                                   MAX_SPEED, FLING_VELOCITY_SAMPLES)

RUBBER_BAND = 0.25


class GuiScrollPanel2D:
  def __init__(self) -> None:
    self._state = ScrollState.STEADY
    self._ox = 0.0
    self._oy = 0.0
    self._vx = 0.0
    self._vy = 0.0
    self._initial_pos: MousePos | None = None
    self._prev_event: MouseEvent | None = None
    self._samples_x: deque[tuple[float, float]] = deque(maxlen=FLING_VELOCITY_SAMPLES)
    self._samples_y: deque[tuple[float, float]] = deque(maxlen=FLING_VELOCITY_SAMPLES)
    self._enabled: bool | Callable[[], bool] = True

  # ***** public API *****

  def set_enabled(self, enabled: bool | Callable[[], bool]) -> None:
    self._enabled = enabled

  @property
  def enabled(self) -> bool:
    return self._enabled() if callable(self._enabled) else self._enabled

  @property
  def state(self) -> ScrollState:
    return self._state

  @property
  def speed(self) -> float:
    return math.hypot(self._vx, self._vy)

  @property
  def offset(self) -> tuple[float, float]:
    return self._ox, self._oy

  def set_offset(self, ox: float, oy: float) -> None:
    self._ox, self._oy = ox, oy

  def stop(self) -> None:
    self._state = ScrollState.STEADY
    self._vx = self._vy = 0.0
    self._samples_x.clear()
    self._samples_y.clear()

  def is_touch_valid(self) -> bool:
    return self._state != ScrollState.MANUAL_SCROLL

  def update(self, offset_bounds: tuple[float, float, float, float],
             snap_target: tuple[float, float] | None = None,
             accept_press: Callable[[MousePos], bool] | None = None,
             events: list[MouseEvent] | None = None, dt: float | None = None) -> tuple[float, float]:
    """offset_bounds = (min_ox, max_ox, min_oy, max_oy). accept_press filters which presses start a pan.
    events/dt are injectable for tests; default to this frame's gui_app events and raylib frame time."""
    for ev in (gui_app.mouse_events if events is None else events):
      if ev.slot == 0:
        self._handle_event(ev, offset_bounds, accept_press)
        self._prev_event = ev

    self._step(offset_bounds, snap_target, rl.get_frame_time() if dt is None else dt)
    return self._ox, self._oy

  # ***** internals *****

  @staticmethod
  def _out_of_bounds(v: float, lo: float, hi: float) -> bool:
    return v < lo or v > hi

  def _oob(self, b: tuple[float, float, float, float]) -> bool:
    return self._out_of_bounds(self._ox, b[0], b[1]) or self._out_of_bounds(self._oy, b[2], b[3])

  def _handle_event(self, ev: MouseEvent, b: tuple[float, float, float, float],
                    accept_press: Callable[[MousePos], bool] | None) -> None:
    if not self.enabled:
      self.stop()
      return

    accepted = accept_press is None or accept_press(ev.pos)

    if self._state == ScrollState.STEADY:
      if ev.left_pressed and accepted:
        self._state = ScrollState.PRESSED
        self._initial_pos = ev.pos

    elif self._state == ScrollState.PRESSED:
      assert self._initial_pos is not None
      dist = math.hypot(ev.pos.x - self._initial_pos.x, ev.pos.y - self._initial_pos.y)
      if ev.left_released:
        self._state = ScrollState.AUTO_SCROLL if self._oob(b) else ScrollState.STEADY
      elif dist > MIN_DRAG_PIXELS:
        self._state = ScrollState.MANUAL_SCROLL

    elif self._state == ScrollState.MANUAL_SCROLL:
      if ev.left_released:
        self._vx = max(-MAX_SPEED, min(MAX_SPEED, fit_velocity(self._samples_x)))
        self._vy = max(-MAX_SPEED, min(MAX_SPEED, fit_velocity(self._samples_y)))
        if self.speed <= MIN_VELOCITY_FOR_CLICKING * 1.5:
          self._vx = self._vy = 0.0
        self._state = ScrollState.AUTO_SCROLL
        self._samples_x.clear()
        self._samples_y.clear()
      elif self._prev_event is not None:
        prev = self._prev_event
        if not self._samples_x:
          self._samples_x.append((prev.t, prev.pos.x))
          self._samples_y.append((prev.t, prev.pos.y))
        self._samples_x.append((ev.t, ev.pos.x))
        self._samples_y.append((ev.t, ev.pos.y))

        dx, dy = ev.pos.x - prev.pos.x, ev.pos.y - prev.pos.y
        if self._out_of_bounds(self._ox, b[0], b[1]):
          dx *= RUBBER_BAND
        if self._out_of_bounds(self._oy, b[2], b[3]):
          dy *= RUBBER_BAND
        self._ox += dx
        self._oy += dy

    elif self._state == ScrollState.AUTO_SCROLL:
      if ev.left_pressed and accepted:
        if self.speed <= MIN_VELOCITY_FOR_CLICKING:
          self._state = ScrollState.PRESSED
          self._initial_pos = ev.pos
        else:
          # catch the fling: stop and treat as a drag, never a tap
          self._vx = self._vy = 0.0
          self._state = ScrollState.MANUAL_SCROLL

  def _step(self, b: tuple[float, float, float, float], snap_target: tuple[float, float] | None, dt: float) -> None:
    dt = dt or 1e-6
    if self._state == ScrollState.STEADY:
      if self._oob(b) or (snap_target is not None and (abs(snap_target[0] - self._ox) >= 1 or abs(snap_target[1] - self._oy) >= 1)):
        self._state = ScrollState.AUTO_SCROLL
      return
    if self._state != ScrollState.AUTO_SCROLL:
      return

    # coast
    self._ox += self._vx * dt
    self._oy += self._vy * dt
    decay = 1 - dt / (AUTO_SCROLL_TC + dt)
    self._vx *= decay
    self._vy *= decay

    # pull back inside bounds, per axis
    pull = 1.0 - math.exp(-BOUNCE_RETURN_RATE * dt)
    for axis, lo, hi in ((0, b[0], b[1]), (1, b[2], b[3])):
      v = self._ox if axis == 0 else self._oy
      if self._out_of_bounds(v, lo, hi):
        target = lo if v < lo else hi
        v += (target - v) * pull
        if axis == 0:
          self._ox, self._vx = v, self._vx * (1 - pull)
        else:
          self._oy, self._vy = v, self._vy * (1 - pull)

    # settle onto the focal icon once momentum yields to it
    if snap_target is not None and self.speed < 400:
      tx = max(b[0], min(b[1], snap_target[0]))
      ty = max(b[2], min(b[3], snap_target[1]))
      snap = 1.0 - math.exp(-SNAP_RATE * dt)
      self._ox += (tx - self._ox) * snap
      self._oy += (ty - self._oy) * snap
      if abs(tx - self._ox) < 1 and abs(ty - self._oy) < 1 and self.speed < MIN_VELOCITY:
        self._ox, self._oy = tx, ty
        self.stop()
      return

    if self.speed < MIN_VELOCITY and not self._oob(b):
      self.stop()
    elif self._oob(b) and self.speed < MIN_VELOCITY:
      # finish the return to the edge
      nx = max(b[0], min(b[1], self._ox))
      ny = max(b[2], min(b[3], self._oy))
      if abs(nx - self._ox) < 1 and abs(ny - self._oy) < 1:
        self._ox, self._oy = nx, ny
        self.stop()
