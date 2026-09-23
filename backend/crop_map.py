#!/usr/bin/env python3
"""One-off: crop ne_50m_land_raw.geojson to the Denmark bbox and write
frontend/public/data/DNK.geo.json (small, 4-dp rounded coordinates).
The johan/world.geo.json DNK file was valid but too coarse (~25 vertices)
for spot-level map zoom, so Natural Earth 50m land is used instead."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "frontend" / "public" / "data"
LON0, LON1, LAT0, LAT1 = 5.0, 16.0, 54.0, 58.5


def ring_hits(ring):
    return any(LON0 <= c[0] <= LON1 and LAT0 <= c[1] <= LAT1 for c in ring)


def main():
    raw = json.loads((DATA / "ne_50m_land_raw.geojson").read_text(encoding="utf-8"))
    kept = []
    for feat in raw.get("features", []):
        g = feat.get("geometry") or {}
        polys = []
        if g.get("type") == "Polygon":
            polys = [g["coordinates"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["coordinates"]
        for poly in polys:
            rings = [
                [[round(c[0], 4), round(c[1], 4)] for c in r]
                for r in poly if ring_hits(r)
            ]
            if rings:
                kept.append(rings)
    out = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "Denmark region (Natural Earth 50m land, cropped)"},
            "geometry": {"type": "MultiPolygon", "coordinates": kept},
        }],
    }
    (DATA / "DNK.geo.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    xs = [c[0] for p in kept for r in p for c in r]
    ys = [c[1] for p in kept for r in p for c in r]
    print("polygons kept:", len(kept),
          "| size:", (DATA / "DNK.geo.json").stat().st_size, "bytes",
          "| bbox:", round(min(xs), 2), round(max(xs), 2), round(min(ys), 2), round(max(ys), 2))


if __name__ == "__main__":
    main()
