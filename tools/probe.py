"""Fetch URLs from GitHub Actions and save the raw responses under probe/.

Used to look at hut booking engines the cloud dev container can't reach.
Read-only: GET, or POST with a JSON/form body. Never books anything.
Usage: python tools/probe.py '<json list of {"url", "method"?, "json"?, "data"?, "headers"?}>'
"""
import hashlib
import json
import pathlib
import sys

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
out = pathlib.Path(__file__).resolve().parent.parent / "probe"
out.mkdir(exist_ok=True)
reqs = json.loads(sys.argv[1])
index = []
for r in reqs:
    if isinstance(r, str):
        r = {"url": r}
    name = r.get("name") or hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()[:10]
    headers = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9,it;q=0.8", **r.get("headers", {})}
    try:
        resp = requests.request(r.get("method", "GET"), r["url"], headers=headers, json=r.get("json"),
                                data=r.get("data"), timeout=(10, 30))
        body, status = resp.text, resp.status_code
    except Exception as e:  # noqa: BLE001
        body, status = f"ERROR {type(e).__name__}: {e}", None
    (out / f"{name}.txt").write_text(body, encoding="utf-8")
    index.append({"name": name, "status": status, "bytes": len(body), **r})
    print(status, len(body), name, r["url"])
(out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
