"""Plain HTTP detail captures; source URLs are read from saved real cards."""
from datetime import datetime, timezone
import json
from pathlib import Path
import httpx
from selectolax.parser import HTMLParser

root = Path(__file__).parent / "fixtures/captures"
results = []
with httpx.Client(timeout=25, follow_redirects=True) as client:
    for portal in ("fotocasa", "habitaclia"):
        tree = HTMLParser((root / f"{portal}.http-200.html").read_text(encoding="utf-8"))
        link = tree.css_first('article a[data-panot-component="link-box-link"]')
        url = f"https://www.{portal}.{'es' if portal == 'fotocasa' else 'com'}" + link.attributes["href"]
        try:
            response = client.get(url)
            filename = f"{portal}.detail.http-{response.status_code}.html"
            (root / filename).write_text(response.text, encoding="utf-8")
            results.append({"portal": portal, "requested_url": url, "final_url": str(response.url), "status": response.status_code, "bytes": len(response.content), "file": filename})
        except httpx.HTTPError as exc:
            results.append({"portal": portal, "requested_url": url, "error": type(exc).__name__})
(root / "details.manifest.json").write_text(json.dumps({"captured_at": datetime.now(timezone.utc).isoformat(), "method": "plain-httpx; one request; no bypass", "results": results}, indent=2), encoding="utf-8")
print(json.dumps(results))
