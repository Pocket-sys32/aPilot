"""Pure geometry for the honeycomb (watch-style) app launcher.

World space: icons on a hex grid, center icon at (0, 0).
Screen space: world shifted by the pan offset, then passed through a fisheye
so icons shrink and bunch up toward the viewport edge.
"""
import math
from dataclasses import dataclass

SQRT3_2 = math.sqrt(3) / 2

# Axial neighbor directions, walked in order to trace a ring
_HEX_DIRS = ((1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1))


def spiral_hex_coords(n: int) -> list[tuple[int, int]]:
  """First n axial (q, r) coords ordered center outward: ring 0 (1), ring 1 (6), ring 2 (12), ..."""
  coords: list[tuple[int, int]] = []
  if n <= 0:
    return coords
  coords.append((0, 0))
  ring = 1
  while len(coords) < n:
    # start at the ring's "upper-right" corner and walk its 6 sides
    q, r = ring, -ring
    for dq, dr in (_HEX_DIRS[1], _HEX_DIRS[2], _HEX_DIRS[3], _HEX_DIRS[4], _HEX_DIRS[5], _HEX_DIRS[0]):
      for _ in range(ring):
        coords.append((q, r))
        if len(coords) == n:
          return coords
        q, r = q + dq, r + dr
    ring += 1
  return coords


def hex_to_world(q: int, r: int, spacing: float) -> tuple[float, float]:
  """Center-to-center distance between neighbors is `spacing`."""
  return spacing * (q + r / 2), spacing * r * SQRT3_2


@dataclass(frozen=True)
class FisheyeParams:
  # fraction of the (elliptical) viewport radius at which icons stop moving outward
  edge: float = 1.0
  # how strongly icons shrink with distance from the focal point
  shrink: float = 0.75
  min_scale: float = 0.2
  # normalized radius beyond which icons fade out
  fade_start: float = 0.8
  fade_end: float = 1.0


DEFAULT_FISHEYE = FisheyeParams()


def fisheye(dx: float, dy: float, half_w: float, half_h: float,
            params: FisheyeParams = DEFAULT_FISHEYE) -> tuple[float, float, float, float]:
  """Map an icon's undistorted offset from the focal point to (dx', dy', scale, alpha).

  The viewport is treated as an ellipse so wide screens use their full width.
  Radius is compressed with tanh: near the center icons move ~linearly, toward
  the edge they slow down and pile up, never passing `edge`.
  """
  if half_w <= 0 or half_h <= 0:
    return dx, dy, 1.0, 1.0

  nx, ny = dx / half_w, dy / half_h
  r = math.hypot(nx, ny)
  if r < 1e-9:
    return dx, dy, 1.0, 1.0

  r_c = params.edge * math.tanh(r / params.edge)
  k = r_c / r
  scale = max(params.min_scale, min(1.0, 1.0 - params.shrink * r_c * r_c))

  if r_c <= params.fade_start:
    alpha = 1.0
  elif r_c >= params.fade_end:
    alpha = 0.0
  else:
    alpha = 1.0 - (r_c - params.fade_start) / (params.fade_end - params.fade_start)

  return dx * k, dy * k, scale, alpha


def hex_ring(q: int, r: int) -> int:
  """Which ring around the center a cell is in (0 = center)."""
  return (abs(q) + abs(r) + abs(q + r)) // 2


def spring_response(t: float, zeta: float, period: float) -> float:
  """Displacement of an underdamped spring released from 1 at t=0, heading to 0.
  Overshoots slightly below 0 before settling, which gives animations their 'pop'.
  Returns 1 for t <= 0 so staggered items hold their start pose until their turn."""
  if t <= 0:
    return 1.0
  w = 2 * math.pi / period
  wd = w * math.sqrt(1 - zeta * zeta)
  return math.exp(-zeta * w * t) * (math.cos(wd * t) + (zeta * w / wd) * math.sin(wd * t))


def ease_in_out(x: float) -> float:
  """Cubic ease in/out on [0, 1]."""
  x = max(0.0, min(1.0, x))
  return 4 * x * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def nearest_point(points: list[tuple[float, float]], ox: float, oy: float) -> int:
  """Index of the world point that sits closest to the focal point for pan offset (ox, oy)."""
  best_i, best_d = -1, math.inf
  for i, (x, y) in enumerate(points):
    d = (x + ox) ** 2 + (y + oy) ** 2
    if d < best_d:
      best_i, best_d = i, d
  return best_i


def bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
  """(min_x, max_x, min_y, max_y) of points; zeros when empty."""
  if not points:
    return 0.0, 0.0, 0.0, 0.0
  xs = [p[0] for p in points]
  ys = [p[1] for p in points]
  return min(xs), max(xs), min(ys), max(ys)
