"""Verify prospective seed routes before writing configuration; no bypass."""
from datetime import datetime, timezone
import json
from pathlib import Path
import httpx
from selectolax.parser import HTMLParser

cities = [
    ("28", "28079", "madrid", "madrid-capital"), ("08", "08019", "barcelona", "barcelona-capital"),
    ("46", "46250", "valencia", "valencia-capital"), ("41", "41091", "sevilla", "sevilla-capital"),
    ("29", "29067", "malaga", "malaga-capital"), ("48", "48020", "bilbao", "bilbao"),
]
results = []
targets = []
with httpx.Client(timeout=25, follow_redirects=True) as client:
    for portal in ("fotocasa", "habitaclia"):
        for operation in ("rent", "sale"):
            for province, municipality, city, segment in cities:
                if portal == "fotocasa":
                    url = f"https://www.fotocasa.es/es/{'alquiler' if operation == 'rent' else 'comprar'}/viviendas/{segment}/todas-las-zonas/l"
                else:
                    url = f"https://www.habitaclia.com/{'alquiler' if operation == 'rent' else 'venta'}-viviendas-{city}.htm"
                observed = {"portal": portal, "operation": operation, "municipality": municipality, "requested_url": url}
                try:
                    response = client.get(url)
                    observed.update(status=response.status_code, final_url=str(response.url))
                    tree = HTMLParser(response.text)
                    if portal == "fotocasa" and tree.css_first("#__initial_props__"):
                        data = json.loads(tree.css_first("#__initial_props__").text())
                        source_code = str(data["initialSearch"]["params"]["locationCode"])
                        count = len(data["initialSearch"]["result"].get("realEstates", []))
                    elif portal == "habitaclia":
                        # Observed geography codes are embedded in the server props.
                        source_code = municipality if f'"code":"{municipality}"' in response.text or f'\\"code\\":\\"{municipality}\\"' in response.text else None
                        count = len(tree.css('article a[data-panot-component="link-box-link"]'))
                    else:
                        source_code, count = None, 0
                    observed.update(source_code=source_code, count=count)
                    if response.status_code == 200 and source_code == municipality and count > 0:
                        targets.append({"portal": portal, "operation": operation, "province": province, "municipality": municipality, "city_slug": city, "priority": "major", "url": str(response.url)})
                        observed["accepted"] = True
                    else:
                        observed["accepted"] = False
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    observed["error"] = type(exc).__name__
                results.append(observed)
root = Path("config")
root.mkdir(exist_ok=True)
(root / "targets.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")
(root / "targets.source.json").write_text(json.dumps({"verified_at": datetime.now(timezone.utc).isoformat(), "method": "plain-httpx; accepted only HTTP200 plus matching embedded municipality code and non-empty results", "results": results}, indent=2), encoding="utf-8")
print(json.dumps({"accepted": len(targets), "checks": len(results), "results": results}))
