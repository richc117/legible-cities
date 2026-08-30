"""Station label placement.

Pinning every label to the east of its node is fine on a diagonal but collapses
on a horizontal run of stations, where consecutive labels share a baseline and
overwrite each other -- LA's E Line is twenty stations in a straight row. Real
transit maps solve this by rotating labels off the line, which works because two
parallel 45-degree labels are thin strips that slide past one another.

Capturing that requires oriented boxes: an axis-aligned approximation of a
rotated label is nearly square and reports collisions that do not exist, which
in practice throws away most of the labels on exactly the runs that need help.
So collision is tested with the separating-axis theorem on the true rotated
quads, with an AABB pre-filter to keep it cheap.

The placer tries a preference-ordered set of candidates per station and takes
the first that hits nothing already on the canvas. Stations are placed most
important first, so interchanges get the good positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class Quad:
    """A convex quad plus its cached AABB."""

    pts: tuple[Point, Point, Point, Point]
    aabb: Box = field(compare=False)

    @classmethod
    def of(cls, pts: list[Point]) -> Quad:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return cls(tuple(pts), (min(xs), min(ys), max(xs), max(ys)))

    @classmethod
    def rect(cls, x0: float, y0: float, x1: float, y1: float) -> Quad:
        return cls.of([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _aabb_hit(a: Box, b: Box, pad: float) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _axes(pts: tuple[Point, ...]) -> list[Point]:
    out = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        ex, ey = x1 - x0, y1 - y0
        length = math.hypot(ex, ey)
        if length > 1e-12:
            out.append((-ey / length, ex / length))
    return out


def collide(a: Quad, b: Quad, pad: float = 0.0) -> bool:
    """Separating-axis test between two convex quads, inflated by ``pad``."""
    if not _aabb_hit(a.aabb, b.aabb, pad):
        return False
    for ax, ay in _axes(a.pts) + _axes(b.pts):
        amin = min(px * ax + py * ay for px, py in a.pts)
        amax = max(px * ax + py * ay for px, py in a.pts)
        bmin = min(px * ax + py * ay for px, py in b.pts)
        bmax = max(px * ax + py * ay for px, py in b.pts)
        if amax + pad <= bmin or bmax + pad <= amin:
            return False  # found a separating axis
    return True


@dataclass(frozen=True)
class Candidate:
    """A label position relative to its node."""

    dx: float
    dy: float
    anchor: str      # SVG text-anchor
    rotate: float    # degrees, clockwise-positive as in SVG


@dataclass
class Placement:
    text: str
    x: float
    y: float
    anchor: str
    rotate: float
    quad: Quad
    # Set when the label had to be allowed over a line to be placed at all. The
    # renderer draws these with a halo so they stay readable against the colour.
    haloed: bool = False


def text_width(text: str, size: float, char_width: float) -> float:
    return len(text) * size * char_width


def label_quad(x: float, y: float, w: float, h: float, anchor: str, rotate: float) -> Quad:
    """The true rotated glyph box of a label anchored at (x, y)."""
    x0 = {"start": 0.0, "middle": -w / 2, "end": -w}[anchor]
    # Baseline-relative: most of the box sits above the baseline.
    corners = [(x0, -h * 0.78), (x0 + w, -h * 0.78), (x0 + w, h * 0.22), (x0, h * 0.22)]
    if rotate:
        r = math.radians(rotate)
        cos, sin = math.cos(r), math.sin(r)
        corners = [(cx * cos - cy * sin, cx * sin + cy * cos) for cx, cy in corners]
    return Quad.of([(x + cx, y + cy) for cx, cy in corners])


# Distances to try, as multiples of a station's own clearance. A label that will
# not fit snug against the line often fits one step out, and pushing it out is
# far better than dropping the station's name entirely.
_RINGS = (1.0, 1.4, 1.9, 2.6, 3.4, 4.4)

# Angles to try, in preference order within a ring. 0 and +/-45 carry almost
# every label; the shallower and steeper angles only come into play in the
# downtown core, where four lines converge and the obvious slots are all taken.
_ANGLES = (0, -45, 45, -30, 30, -60, 60)


def candidates(offset: float, prefer_rotated: bool) -> list[Candidate]:
    """Preference-ordered placements, best first.

    Horizontal reads best, so it leads -- except at a station sitting on a
    horizontal run, where horizontal is exactly what will not fit and the
    rotated variants are tried first instead. Each ring is exhausted before
    moving further out, so labels stay as close to their station as they can.
    """
    out: list[Candidate] = []
    for ring in _RINGS:
        d = offset * ring
        flat: list[Candidate] = []
        rot: list[Candidate] = []
        for angle in _ANGLES:
            # Push the label out along its own bearing, so a rotated label sits
            # on the ray it reads along rather than crossing back over the node.
            r = math.radians(angle)
            dx, dy = d * math.cos(r), d * math.sin(r)
            bucket = flat if angle == 0 else rot
            bucket.append(Candidate(dx, dy, "start", angle))
            bucket.append(Candidate(-dx, -dy, "end", angle))
        stacked = [Candidate(0.0, -d, "middle", 0), Candidate(0.0, d * 1.5, "middle", 0)]
        out += (rot + flat + stacked) if prefer_rotated else (flat + rot + stacked)
    return out


@dataclass
class Station:
    text: str
    x: float
    y: float
    importance: int = 0
    # True when the station's own lines run roughly east-west, which is when
    # horizontal labels collide with the neighbours.
    on_horizontal_run: bool = False
    # How far out the drawn tracks reach at this station. A label pinned at a
    # fixed distance lands inside the bundle wherever several lines run
    # together, so each station clears its own.
    clearance: float = 0.0


def place(
    stations: list[Station],
    obstacles: list[Quad],
    *,
    size: float,
    char_width: float,
    offset: float,
    marker_radius: float,
    canvas: tuple[float, float] | None = None,
    pad: float = 1.5,
) -> tuple[list[Placement], list[Station]]:
    """Place labels, most important station first.

    Returns the placements and the stations that could not be placed anywhere
    without a collision.
    """
    markers: list[Quad] = []
    for s in stations:
        r = marker_radius
        markers.append(Quad.rect(s.x - r, s.y - r, s.x + r, s.y + r))

    # Hard blockers can never be crossed; soft ones (the drawn lines) can be, as
    # a last resort, because an unnamed station is worse than a name sitting
    # over its own line with a halo behind it.
    hard: list[Quad] = list(markers)
    soft: list[Quad] = list(obstacles)

    placed: list[Placement] = []
    dropped: list[Station] = []

    def attempt(s: Station, allow_over_lines: bool) -> Placement | None:
        w = text_width(s.text, size, char_width)
        for c in candidates(offset + s.clearance, s.on_horizontal_run):
            q = label_quad(s.x + c.dx, s.y + c.dy, w, size, c.anchor, c.rotate)
            if canvas and (q.aabb[0] < 0 or q.aabb[1] < 0
                           or q.aabb[2] > canvas[0] or q.aabb[3] > canvas[1]):
                continue
            if any(collide(q, o, pad) for o in hard):
                continue
            if not allow_over_lines and any(collide(q, o, pad) for o in soft):
                continue
            return Placement(s.text, s.x + c.dx, s.y + c.dy, c.anchor, c.rotate, q,
                             haloed=allow_over_lines)
        return None

    ordered = sorted(stations, key=lambda s: -s.importance)
    leftover: list[Station] = []
    for s in ordered:
        chosen = attempt(s, allow_over_lines=False)
        if chosen is None:
            leftover.append(s)
            continue
        placed.append(chosen)
        hard.append(chosen.quad)

    # Second pass, over the lines. Runs after every clean placement is locked in,
    # so a station that could sit clear never gets pushed onto a line.
    for s in leftover:
        chosen = attempt(s, allow_over_lines=True)
        if chosen is None:
            dropped.append(s)
            continue
        placed.append(chosen)
        hard.append(chosen.quad)

    return placed, dropped


def polyline_quads(points: list[Point], width: float) -> list[Quad]:
    """Approximate a drawn polyline as a chain of oriented boxes."""
    half = width / 2
    out: list[Quad] = []
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = -dy / length * half, dx / length * half
        out.append(Quad.of([(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny),
                            (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]))
    return out
