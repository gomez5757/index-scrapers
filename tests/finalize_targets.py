"""Generate config from saved verified observations; no further HTTP requests."""
import json
from pathlib import Path

root = Path("config")
source = json.loads((root / "targets.source.json").read_text(encoding="utf-8"))
cities = {"28079": ("28", "madrid"), "08019": ("08", "barcelona"), "46250": ("46", "valencia"), "41091": ("41", "sevilla"), "29067": ("29", "malaga"), "48020": ("48", "bilbao")}
targets = []
for result in source["results"] + source["followup"]["results"]:
    if result.get("status") != 200 or result.get("count", 0) <= 0 or str(result.get("source_code", "")).zfill(5) != result["municipality"]:
        continue
    if result["portal"] == "habitaclia" and "source_operation" not in result:
        continue
    operation = {"rent": "rent", "buy": "sale", "sale": "sale"}.get(result.get("source_operation", result["operation"]))
    if operation != result["operation"]:
        continue
    province, city = cities[result["municipality"]]
    targets.append({"portal": result["portal"], "operation": operation, "province": province, "municipality": result["municipality"], "city_slug": city, "priority": "major", "url": result["final_url"]})
targets.extend([
    {"portal": "idealista", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "priority": "major", "url": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"},
    {"portal": "pisoscom", "operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid", "priority": "major", "url": "https://www.pisos.com/alquiler/pisos-madrid_capital_zona_urbana/"},
])
source["normalization"] = {"municipality": "INE5 padded with leading zero", "habitaclia_operation": {"rent": "rent", "buy": "sale"}, "browser_only_targets": ["tests/fixtures/captures/idealista.browser-20261012.source.json", "tests/fixtures/captures/pisoscom.browser-20261012.source.json"], "browser_only_runtime": "HTTP blocked; health probes remain enabled; no bypass"}
(root / "targets.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")
(root / "targets.source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
print(json.dumps({"configured_targets": len(targets), "six_cities": len({item['municipality'] for item in targets})}))
