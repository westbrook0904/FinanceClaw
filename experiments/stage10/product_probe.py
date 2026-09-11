"""Exercise the actual product routes on the PostgreSQL Agent Server image."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

client = httpx.Client(
    base_url="http://localhost:8000",
    headers={"Authorization": "Bearer stage10-product-test"},
    timeout=10,
)
for _attempt in range(50):
    try:
        if client.get("/v1/health/ready").status_code == 200:
            break
    except httpx.TransportError:
        pass
    time.sleep(0.2)
else:
    raise RuntimeError("API is not ready")
report = {"native_external_denied": client.post("/threads", json={}).status_code == 403}
response = client.post("/v1/conversations", json={})
assert response.status_code == 201, response.text
conversation_id = response.json()["conversation_id"]
started = time.monotonic()
response = client.post(
    f"/v1/conversations/{conversation_id}/turns",
    json={"message": "请计算 2 + 3"},
    headers={"Idempotency-Key": str(uuid4())},
)
assert response.status_code == 202, response.text
accepted = response.json()
report.update(accepted=accepted, acceptance_ms=round((time.monotonic() - started) * 1000, 2))
path = f"/v1/conversations/{conversation_id}/turns/{accepted['turn_id']}"
for _attempt in range(120):
    response = client.get(path)
    assert response.status_code == 200, response.text
    snapshot = response.json()
    if snapshot["status"] in {"completed", "failed", "blocked"}:
        break
    time.sleep(0.25)
report["snapshot"] = snapshot
report["messages"] = client.get(f"/v1/conversations/{conversation_id}/messages").json()
print(json.dumps(report, ensure_ascii=False, indent=2))
Path("/project/.redesign/evidence/stage10/product-contract.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
)
assert snapshot["status"] == "completed", snapshot
assert report["native_external_denied"]
assert len(report["messages"]["messages"]) == 2
print("product contract passed")
