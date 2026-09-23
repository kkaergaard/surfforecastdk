#!/usr/bin/env python3
"""Quick sanity check of frontend/public/data/forecast.json (dev helper)."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = json.loads((ROOT / "frontend/public/data/forecast.json").read_text(encoding="utf-8"))
print("generated_at:", d["generated_at"], "| sources:", d["sources"])
for s in d["spots"]:
    nulls = sum(1 for e in s["forecast"] if e["hs"] is None)
    print(f"{s['id']}: status={s['status']} null-hs {nulls}/{len(s['forecast'])}")
print()
s = next(x for x in d["spots"] if x["id"] == "klitmoller-bugten")
for e in s["forecast"][1:7]:
    print(
        e["time"], "| hs", e["hs"], "| swell", e["swell_h"], "m /",
        e["swell_period"], "s /", e["swell_dir"], "| wind", e["wind_ms"],
        "m/s", e["wind_dir"], "| gust", e["gust_ms"], "|", e["air_temp_c"],
        "C |", e["rating"], e["rating_label"],
    )
labels = Counter(e["rating_label"] for e in s["forecast"])
print("rating range:", min(e["rating"] for e in s["forecast"]), "-",
      max(e["rating"] for e in s["forecast"]), "| labels:", dict(labels))
print("tide pts:", len(s["tides"]), "| high/low entries:", len(s["high_low"]),
      "| next:", s["high_low"][0] if s["high_low"] else None)
