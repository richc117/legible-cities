"""Coordinate transforms.

LOOM computes in Web Mercator metres but writes lon/lat, so a 45-degree
schematic edge comes back looking like 39 degrees at LA's latitude. Anything
that measures angles or draws pixels has to project first.
"""

from __future__ import annotations

import math

R = 6378137.0  # Web Mercator sphere radius, metres

Coord = tuple[float, float]


def to_mercator(c: Coord) -> Coord:
    lon, lat = c
    lat = max(min(lat, 89.9), -89.9)
    return (math.radians(lon) * R,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R)


def to_wgs84(c: Coord) -> Coord:
    x, y = c
    return (math.degrees(x / R),
            math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2))
