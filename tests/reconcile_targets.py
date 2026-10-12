"""Reuse observed Fotocasa responses; verify Habitaclia canonical search context."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import httpx
from selectolax.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from index_scrapers.extractors import initial_data

root = Path("config")
source = json.loads((root / "targets.source.json").read_text())
cities = {"28079": ("28", "madrid"), "08019": ("08", "barcelona"), "46250": ("46", "valencia"), "41091": ("41", "sevilla"), "29067": ("29", "malaga"), "48020": ("48", "bilbao")}
targets = []
followup = []
for observed in source["results"]:
    if observed["portal"] == "fotocasa" and observed.get("status") == 200 and str(observed.get("source_code", "")).zfill(5) == observed["municipality"] and observed.get("count", 0) > 0:
        province, city = cities[observed["municipality"]]
        targets.append({"portal": "fotocasa", "operation": observed["operation"], "province": province, "municipality": observed["municipality"], "city_slug": city, "priority": "major", "url": observed["final_url"]})
with httpx.Client(timeout=25, follow_redirects=True) as client:
    for observed in source["results"]:
        if observed["portal"] != "habitaclia" or observed["operation"] != "rent" or observed.get("status") != 200:
            continue
        province, city = cities[observed["municipality"]]
        for operation in ("rent", "sale"):
            url = observed["final_url"] if operation == "rent" else observed["final_url"].replace("/alquiler/", "/comprar/")
            result = {"portal": "habitaclia", "operation": operation, "municipality": observed["municipality"], "requested_url": url}
            try:
                response = client.get(url)
                result.update(status=response.status_code, final_url=str(response.url))
                data = initial_data(HTMLParser(response.text))
                context = data["initialSearchResultsPage"]["initialSearchContext"]
                code = str(context["geography"]["code"]).zfill(5)
                source_op = context["filters"]["transactionType"]
                count = len(context["results"]["items"])
                result.update(source_code=code, source_operation=source_op, count=count)
                result["accepted"] = response.status_code == 200 and code == observed["municipality"] and source_op == operation and count > 0
                if result["accepted"]:
                    targets.append({"portal": "habitaclia", "operation": operation, "province": province, "municipality": observed["municipality"], "city_slug": city, "priority": "major", "url": str(response.url)})
            except (httpx.HTTPError, TypeError, KeyError, ValueError) as exc:
                result["error"] = type(exc).__name__
            followup.append(result)
source["followup"] = {"verified_at": datetime.now(timezone.utc).isoformat(), "method": "INE codes zero-padded to five digits; Habitaclia selected search geography AND transaction match; all plain HTTP200 non-empty", "results": followup}
(root / "targets.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")
(root / "targets.source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
print(json.dumps({"configured": len(targets), "followup_checks": len(followup), "failures": [item for item in followup if not item.get("accepted")]}))
