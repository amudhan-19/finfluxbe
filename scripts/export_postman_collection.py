"""One-off: build Postman Collection v2.1 from FastAPI OpenAPI."""
import json
import uuid
from collections import defaultdict

from app.main import app


def main() -> None:
    s = app.openapi()
    rows: list[tuple[str, str, str]] = []
    for path, ops in s["paths"].items():
        for method, meta in ops.items():
            if str(method).startswith("x-") or method == "parameters":
                continue
            rows.append((method.upper(), path, meta.get("summary") or path))

    def folder_title(path: str) -> str:
        if path == "/":
            return "Health"
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api":
            if parts[1] == "admin" and len(parts) >= 3:
                return "Admin: " + parts[2].replace("-", " ").title()
            if parts[1] == "auth":
                return "Auth"
            if parts[1] == "entities":
                return "Entities & domain"
            if parts[1] == "reminders":
                return "Reminders"
        return "Other"

    groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for method, path, summary in sorted(rows, key=lambda x: (x[1], x[0])):
        groups[folder_title(path)].append((method, path, summary))

    def req(method: str, path: str, name: str) -> dict:
        raw = "{{baseUrl}}" + path
        parts = [p for p in path.split("/") if p]
        segs: list[str] = []
        variable: list[dict] = []
        for p in parts:
            if p.startswith("{") and p.endswith("}"):
                k = p[1:-1]
                segs.append(":" + k)
                variable.append({"key": k, "value": ""})
            else:
                segs.append(p)
        url: dict = {"raw": raw, "host": ["{{baseUrl}}"], "path": segs if segs else [""]}
        if variable:
            url["variable"] = variable
        item: dict = {
            "name": name[:120],
            "request": {
                "method": method,
                "header": [],
                "url": url,
            },
        }
        if method in ("POST", "PUT", "PATCH"):
            item["request"]["header"].append(
                {"key": "Content-Type", "value": "application/json"}
            )
            item["request"]["body"] = {"mode": "raw", "raw": "{\n  \n}"}
        return item

    order = ["Health", "Auth", "Entities & domain", "Reminders"]
    admin_keys = sorted(k for k in groups if k.startswith("Admin:"))
    folder_order = order + admin_keys + sorted(
        k for k in groups if k not in order and not k.startswith("Admin:")
    )

    items = []
    for title in folder_order:
        if title not in groups:
            continue
        sub = [req(m, p, s or f"{m} {p}") for m, p, s in groups[title]]
        items.append({"name": title, "item": sub})

    collection = {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": "FinFlux API",
            "description": (
                "FinFlux Backend — import into Postman. Variables: baseUrl, access_token. "
                "Login via Auth folder then paste access_token for Bearer auth."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000"},
            {"key": "access_token", "value": ""},
        ],
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
        },
        "item": items,
    }

    out = "FinFlux.postman_collection.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(collection, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out} ({len(rows)} requests)")


if __name__ == "__main__":
    main()
