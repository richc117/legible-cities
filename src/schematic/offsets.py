"""Parallel-track geometry.

After the ``loom`` stage, each edge's ``lines`` array is in solved left-to-right
order across the edge's from->to direction. Drawing is then a matter of pushing
each line's polyline sideways off the edge centreline by a fixed spacing:

    offset(i) = (i - (N - 1) / 2) * spacing

The offset polyline is built with mitred joins so consecutive segments meet
cleanly -- important here because octilinear output turns at 45 degrees, where a
naive per-segment offset leaves visible notches at every bend.
"""

from __future__ import annotations

import math

Coord = tuple[float, float]

# Beyond this the mitre spike at a hairpin bend is longer than it is useful;
# clamp instead so a near-reversal does not fling a vertex across the map.
MAX_MITRE = 4.0


def _unit(a: Coord, b: Coord) -> tuple[float, float] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-12 else None


def _normal(u: tuple[float, float]) -> tuple[float, float]:
    """Left-hand normal of a unit vector."""
    return (-u[1], u[0])


def dedupe(points: list[Coord], tol: float = 1e-12) -> list[Coord]:
    out: list[Coord] = []
    for p in points:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > tol:
            out.append(p)
    return out


def offset_polyline(points: list[Coord], distance: float) -> list[Coord]:
    """Offset a polyline sideways by ``distance`` (positive = left of travel)."""
    pts = dedupe(points)
    if len(pts) < 2:
        return list(pts)
    if abs(distance) < 1e-12:
        return pts

    # Unit direction and left normal of each segment.
    dirs = [d for d in (_unit(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if d]
    if not dirs:
        return pts
    normals = [_normal(d) for d in dirs]

    out: list[Coord] = [(pts[0][0] + normals[0][0] * distance,
                         pts[0][1] + normals[0][1] * distance)]

    for i in range(1, len(pts) - 1):
        n0, n1 = normals[i - 1], normals[i]
        # Mitre direction is the normalised sum of the two segment normals; the
        # mitre length compensates for the angle between them.
        mx, my = n0[0] + n1[0], n0[1] + n1[1]
        m = math.hypot(mx, my)
        if m < 1e-9:  # exact reversal, no sane mitre
            out.append((pts[i][0] + n1[0] * distance, pts[i][1] + n1[1] * distance))
            continue
        mx, my = mx / m, my / m
        # cos of the half-angle between the two normals; 1/cos is the mitre
        # stretch that lands the vertex on the intersection of the offset legs.
        cos_half = n0[0] * mx + n0[1] * my
        scale = min(1.0 / cos_half, MAX_MITRE) if cos_half > 1e-6 else MAX_MITRE
        out.append((pts[i][0] + mx * distance * scale, pts[i][1] + my * distance * scale))

    out.append((pts[-1][0] + normals[-1][0] * distance,
                pts[-1][1] + normals[-1][1] * distance))
    return out


def track_offset(index: int, count: int, spacing: float) -> float:
    """Signed sideways offset for track ``index`` of ``count`` on one edge."""
    return (index - (count - 1) / 2.0) * spacing


def polyline_length(points: list[Coord]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def cumulative_lengths(points: list[Coord]) -> list[float]:
    out = [0.0]
    for i in range(len(points) - 1):
        out.append(out[-1] + math.dist(points[i], points[i + 1]))
    return out


def point_at(points: list[Coord], length: float) -> Coord:
    """Point at arc length ``length`` along a polyline (clamped at both ends)."""
    cum = cumulative_lengths(points)
    if length <= 0:
        return points[0]
    if length >= cum[-1]:
        return points[-1]
    for i in range(len(cum) - 1):
        if cum[i + 1] >= length:
            t = (length - cum[i]) / (cum[i + 1] - cum[i])
            return (points[i][0] + t * (points[i + 1][0] - points[i][0]),
                    points[i][1] + t * (points[i + 1][1] - points[i][1]))
    return points[-1]
