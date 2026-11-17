import httpx, json

payload = {
    "message": "你们家沐浴乳有什么款式？",
    "session_id": "test-palm-005",
    # "brand": "新肌秀",
    "bot_id": "muscle-brand"
}

resp = httpx.post("http://localhost:8000/cs", json=payload, timeout=120.0)
print(f"Status: {resp.status_code}")
print(json.dumps(resp.json(), ensure_ascii=False, indent=2))