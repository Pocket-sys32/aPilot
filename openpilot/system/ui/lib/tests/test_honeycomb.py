import math
import unittest

from openpilot.system.ui.lib.application import MouseEvent, MousePos
from openpilot.system.ui.lib.honeycomb import (FisheyeParams, spiral_hex_coords, hex_to_world, fisheye, nearest_point, bounds,
                                               hex_ring, spring_response, ease_in_out)
from openpilot.system.ui.lib.scroll_panel2 import ScrollState
from openpilot.system.ui.lib.scroll_panel_2d import GuiScrollPanel2D

DT = 1 / 60


def _hex_dist(a, b):
  dq, dr = a[0] - b[0], a[1] - b[1]
  return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


class TestHoneycombGeometry(unittest.TestCase):
  def test_spiral_rings(self):
    coords = spiral_hex_coords(19)
    self.assertEqual(len(set(coords)), 19)
    self.assertEqual(coords[0], (0, 0))
    self.assertTrue(all(_hex_dist(c, (0, 0)) == 1 for c in coords[1:7]))
    self.assertTrue(all(_hex_dist(c, (0, 0)) == 2 for c in coords[7:19]))
    # consecutive cells within a ring are neighbors
    self.assertTrue(all(_hex_dist(coords[i], coords[i + 1]) == 1 for i in range(1, 6)))
    self.assertEqual(spiral_hex_coords(0), [])
    self.assertEqual(len(spiral_hex_coords(8)), 8)

  def test_neighbors_equidistant(self):
    for q, r in spiral_hex_coords(7)[1:]:
      x, y = hex_to_world(q, r, 100.0)
      self.assertAlmostEqual(math.hypot(x, y), 100.0)

  def test_fisheye_center_identity(self):
    self.assertEqual(fisheye(0.0, 0.0, 268, 120), (0.0, 0.0, 1.0, 1.0))

  def test_fisheye_monotonic_and_bounded(self):
    prev_r, prev_scale = -1.0, 2.0
    for i in range(1, 60):
      dx = i * 20.0
      ox, oy, scale, alpha = fisheye(dx, 0.0, 268, 120)
      self.assertEqual(oy, 0.0)
      self.assertGreater(ox, prev_r)  # order preserved
      self.assertLess(ox, 268)  # never pushed past the edge
      self.assertLessEqual(scale, prev_scale)
      self.assertGreaterEqual(scale, FisheyeParams().min_scale)
      self.assertTrue(0.0 <= alpha <= 1.0)
      prev_r, prev_scale = ox, scale
    self.assertEqual(fisheye(10_000.0, 0.0, 268, 120)[3], 0.0)  # far icons fade out

  def test_fisheye_symmetric(self):
    a = fisheye(80.0, 40.0, 268, 120)
    b = fisheye(-80.0, -40.0, 268, 120)
    self.assertAlmostEqual(a[0], -b[0])
    self.assertAlmostEqual(a[1], -b[1])
    self.assertAlmostEqual(a[2], b[2])

  def test_nearest_and_bounds(self):
    pts = [(0.0, 0.0), (100.0, 0.0), (-50.0, 86.6)]
    self.assertEqual(nearest_point(pts, 0, 0), 0)
    self.assertEqual(nearest_point(pts, -90, 0), 1)  # panned left: point at x=100 is now near center
    self.assertEqual(bounds(pts), (-50.0, 100.0, 0.0, 86.6))
    self.assertEqual(nearest_point([], 0, 0), -1)


class TestHoneycombAnimation(unittest.TestCase):
  def test_hex_ring(self):
    self.assertEqual([hex_ring(q, r) for q, r in spiral_hex_coords(19)], [0] + [1] * 6 + [2] * 12)

  def test_spring_holds_then_settles_with_overshoot(self):
    self.assertEqual(spring_response(-0.1, 0.55, 0.5), 1.0)  # staggered items wait in their start pose
    self.assertEqual(spring_response(0.0, 0.55, 0.5), 1.0)
    samples = [spring_response(i / 120, 0.55, 0.5) for i in range(1, 240)]
    self.assertLess(min(samples), 0.0)  # overshoots past rest: the "pop"
    self.assertGreater(min(samples), -0.2)  # but only a little
    self.assertLess(abs(samples[-1]), 0.01)  # settled within 2s

  def test_ease_in_out(self):
    self.assertEqual(ease_in_out(0.0), 0.0)
    self.assertEqual(ease_in_out(1.0), 1.0)
    self.assertAlmostEqual(ease_in_out(0.5), 0.5)
    self.assertEqual(ease_in_out(-1.0), 0.0)
    self.assertEqual(ease_in_out(2.0), 1.0)
    xs = [ease_in_out(i / 50) for i in range(51)]
    self.assertEqual(xs, sorted(xs))


class TestScrollPanel2D(unittest.TestCase):
  BOUNDS = (-300.0, 300.0, -200.0, 200.0)

  def setUp(self):
    self.t = 0.0
    self.p = GuiScrollPanel2D()

  def _ev(self, x, y, pressed=False, released=False, down=True):
    return MouseEvent(MousePos(x, y), 0, pressed, released, down and not released, self.t)

  def _frame(self, events=(), snap=None, accept=None):
    self.t += DT
    return self.p.update(self.BOUNDS, snap_target=snap, accept_press=accept, events=list(events), dt=DT)

  def _drag(self, x0, y0, x1, y1, steps=10, release=True):
    self._frame([self._ev(x0, y0, pressed=True)])
    for i in range(1, steps + 1):
      self._frame([self._ev(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)])
    if release:
      self._frame([self._ev(x1, y1, released=True)])

  def test_tap_does_not_move(self):
    self._frame([self._ev(10, 10, pressed=True)])
    self.assertEqual(self.p.state, ScrollState.PRESSED)
    self._frame([self._ev(12, 11, released=True)])
    self.assertEqual(self.p.state, ScrollState.STEADY)
    self.assertEqual(self.p.offset, (0.0, 0.0))

  def test_drag_moves_both_axes(self):
    self._drag(0, 0, 60, -40, release=False)
    self.assertEqual(self.p.state, ScrollState.MANUAL_SCROLL)
    self.assertFalse(self.p.is_touch_valid())
    ox, oy = self.p.offset
    # first MIN_DRAG_PIXELS are consumed deciding it's a drag
    self.assertGreater(ox, 40)
    self.assertLess(oy, -25)

  def test_fling_coasts_and_stops(self):
    # quick swipe: 200px in 4 frames
    self._drag(0, 0, 200, 0, steps=4)
    self.assertEqual(self.p.state, ScrollState.AUTO_SCROLL)
    after_release = self.p.offset[0]
    for _ in range(300):
      self._frame()
    self.assertGreater(self.p.offset[0], after_release)  # momentum carried it further
    self.assertEqual(self.p.state, ScrollState.STEADY)
    self.assertLessEqual(self.p.offset[0], self.BOUNDS[1])  # and it came back within bounds

  def test_rubber_band_returns(self):
    self.p.set_offset(250.0, 0.0)
    self._drag(0, 0, 300, 0, steps=30)
    self.assertGreater(self.p.offset[0], 300.0)
    self.assertLess(self.p.offset[0], 250.0 + 300.0)  # resisted past the edge
    for _ in range(300):
      self._frame()
    self.assertAlmostEqual(self.p.offset[0], 300.0, delta=1.0)
    self.assertEqual(self.p.state, ScrollState.STEADY)

  def test_snaps_to_target(self):
    self.p.set_offset(37.0, -12.0)
    for _ in range(300):
      self._frame(snap=(0.0, 0.0))
    self.assertEqual(self.p.offset, (0.0, 0.0))
    self.assertEqual(self.p.state, ScrollState.STEADY)

  def test_accept_press_filter(self):
    self._frame([self._ev(5, 5, pressed=True)], accept=lambda pos: pos.x > 50)
    self.assertEqual(self.p.state, ScrollState.STEADY)
    self._frame([self._ev(5, 5, released=True)])
    self._frame([self._ev(100, 5, pressed=True)], accept=lambda pos: pos.x > 50)
    self.assertEqual(self.p.state, ScrollState.PRESSED)

  def test_disabled_ignores_input(self):
    self.p.set_enabled(False)
    self._drag(0, 0, 100, 0)
    self.assertEqual(self.p.offset, (0.0, 0.0))


if __name__ == "__main__":
  unittest.main()
