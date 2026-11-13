"""Procedural icon glyphs drawn with raylib primitives, so apps don't need image assets.
Each takes (cx, cy, r, color) where r is the radius of the icon circle."""
import math
import raylib_nb as rl


def _v(x: float, y: float) -> rl.Vector2:
  return rl.Vector2(x, y)


def _tri(a: rl.Vector2, b: rl.Vector2, c: rl.Vector2, color: rl.Color) -> None:
  # this raylib build has no draw_triangle; a 3 point fan is equivalent (counter-clockwise on screen)
  rl.draw_triangle_fan([a, b, c], 3, color)


def music_note(cx: float, cy: float, r: float, color: rl.Color) -> None:
  # two beamed eighth notes
  head_r = r * 0.17
  lx, rx = cx - r * 0.28, cx + r * 0.26
  ly, ry = cy + r * 0.32, cy + r * 0.22
  stem_w = max(1.0, r * 0.08)
  top_l, top_r = cy - r * 0.38, cy - r * 0.48
  rl.draw_circle_v(_v(lx, ly), head_r, color)
  rl.draw_circle_v(_v(rx, ry), head_r, color)
  rl.draw_line_ex(_v(lx + head_r * 0.9, ly), _v(lx + head_r * 0.9, top_l), stem_w, color)
  rl.draw_line_ex(_v(rx + head_r * 0.9, ry), _v(rx + head_r * 0.9, top_r), stem_w, color)
  rl.draw_line_ex(_v(lx + head_r * 0.9 - stem_w / 2, top_l), _v(rx + head_r * 0.9 + stem_w / 2, top_r), stem_w * 1.8, color)


def map_pin(cx: float, cy: float, r: float, color: rl.Color) -> None:
  head_cy = cy - r * 0.15
  head_r = r * 0.36
  rl.draw_circle_v(_v(cx, head_cy), head_r, color)
  _tri(_v(cx - head_r * 0.85, head_cy + head_r * 0.5), _v(cx, cy + r * 0.55),
       _v(cx + head_r * 0.85, head_cy + head_r * 0.5), color)
  rl.draw_circle_v(_v(cx, head_cy), head_r * 0.42, rl.Color(0, 0, 0, color.a // 2))


def sun(cx: float, cy: float, r: float, color: rl.Color) -> None:
  rl.draw_circle_v(_v(cx, cy), r * 0.24, color)
  w = max(1.0, r * 0.08)
  for i in range(8):
    a = i * math.pi / 4
    rl.draw_line_ex(_v(cx + math.cos(a) * r * 0.36, cy + math.sin(a) * r * 0.36),
                    _v(cx + math.cos(a) * r * 0.52, cy + math.sin(a) * r * 0.52), w, color)


def clock(cx: float, cy: float, r: float, color: rl.Color) -> None:
  w = max(1.0, r * 0.08)
  rl.draw_ring(_v(cx, cy), r * 0.42, r * 0.42 + w, 0, 360, 32, color)
  rl.draw_line_ex(_v(cx, cy), _v(cx, cy - r * 0.3), w, color)
  rl.draw_line_ex(_v(cx, cy), _v(cx + r * 0.22, cy), w, color)


def bars(cx: float, cy: float, r: float, color: rl.Color) -> None:
  bw = r * 0.16
  base = cy + r * 0.4
  for i, h in enumerate((0.35, 0.6, 0.85)):
    x = cx - r * 0.36 + i * bw * 1.6
    rl.draw_rectangle_rec(rl.Rectangle(x, base - r * h, bw, r * h), color)


def phone(cx: float, cy: float, r: float, color: rl.Color) -> None:
  body = rl.Rectangle(cx - r * 0.24, cy - r * 0.42, r * 0.48, r * 0.84)
  rl.draw_rectangle_rounded(body, 0.35, 8, color)
  rl.draw_rectangle_rec(rl.Rectangle(body.x + r * 0.06, body.y + r * 0.1, body.width - r * 0.12, body.height - r * 0.26),
                        rl.Color(0, 0, 0, color.a // 2))


# ***** media control glyphs *****

def play(cx: float, cy: float, r: float, color: rl.Color) -> None:
  s = r * 0.42
  _tri(_v(cx - s * 0.6, cy - s), _v(cx - s * 0.6, cy + s), _v(cx + s, cy), color)


def pause(cx: float, cy: float, r: float, color: rl.Color) -> None:
  s = r * 0.4
  bw = s * 0.62
  rl.draw_rectangle_rec(rl.Rectangle(cx - s * 0.85, cy - s, bw, s * 2), color)
  rl.draw_rectangle_rec(rl.Rectangle(cx + s * 0.85 - bw, cy - s, bw, s * 2), color)


def skip(cx: float, cy: float, r: float, color: rl.Color, forward: bool = True) -> None:
  s = r * 0.32
  d = 1 if forward else -1
  for off in (-s * 0.9, s * 0.1):
    x0 = cx + d * off
    _tri(*((_v(x0, cy - s), _v(x0, cy + s), _v(x0 + d * s, cy)) if forward else
           (_v(x0, cy - s), _v(x0 + d * s, cy), _v(x0, cy + s))), color)
  bar_x = cx + d * s * 1.1
  rl.draw_rectangle_rec(rl.Rectangle(bar_x - (0 if forward else s * 0.25), cy - s, s * 0.25, s * 2), color)
