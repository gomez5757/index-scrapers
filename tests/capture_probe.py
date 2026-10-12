"""One plain HTTP request per portal; no anti-bot circumvention."""
import json
from pathlib import Path
import httpx

ROOT = Path(__file__).parent / "fixtures" / "captures"
ROOT.mkdir(exist_ok=True)
urls = {
    "idealista": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/",
    "fotocasa": "https://www.fotocasa.es/es/alquiler/viviendas/madrid-capital/todas-las-zonas/l",
    "pisoscom": "https://www.pisos.com/alquiler/pisos-madrid_capital/",
    "habitaclia": "https://www.habitaclia.com/alquiler-viviendas-madrid.htm",
}
results = []
with httpx.Client(timeout=20, follow_redirects=True) as client:
    for portal, url in urls.items():
        try:
            response = client.get(url)
            filename = f"{portal}.http-{response.status_code}.html"
            (ROOT / filename).write_text(response.text, encoding="utf-8")
            results.append({"portal": portal, "requested_url": url, "final_url": str(response.url), "status": response.status_code, "bytes": len(response.content), "file": filename})
        except httpx.HTTPError as exc:
            results.append({"portal": portal, "requested_url": url, "error": type(exc).__name__})
(ROOT / "manifest.json").write_text(json.dumps({"captured_at": "2026-10-12", "method": "plain-httpx; no retry; no bypass", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results))
