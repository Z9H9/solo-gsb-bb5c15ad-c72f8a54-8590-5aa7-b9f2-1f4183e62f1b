"""API 端到端测试（需要 fastapi / httpx）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_desensitize_happy_path():
    payload = {
        "text": "手机 13800001111，邮箱 alice@example.com",
        "rules": [
            {"id": "phone", "pattern": r"1\d{10}", "priority": 10,
             "replacement": "[手机]"},
            {"id": "email", "pattern": r"(\w+)@[\w.]+", "priority": 5,
             "replacement": r"\1@***"},
        ],
    }
    resp = client.post("/api/v1/desensitize", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["masked_text"] == "手机 [手机]，邮箱 alice@***"
    assert data["stats"]["matches_applied"] == 2
    assert data["rule_errors"] == []
    statuses = {m["rule_id"]: m["status"] for m in data["matches"]}
    assert statuses == {"phone": "applied", "email": "applied"}
    for m in data["matches"]:
        assert m["original_span"]["start"] < m["original_span"]["end"]
        assert m["result_span"]["start"] < m["result_span"]["end"]


def test_overlap_resolution_via_api():
    payload = {
        "text": "abcdefgh",
        "rules": [
            {"pattern": r"abc", "priority": 1, "replacement": "X"},
            {"pattern": r"bcd", "priority": 5, "replacement": "YY"},
        ],
    }
    data = client.post("/api/v1/desensitize", json=payload).json()
    assert data["masked_text"] == "aYYefgh"
    by_rule = {m["rule_index"]: m for m in data["matches"]}
    assert by_rule[1]["status"] == "applied"
    assert by_rule[0]["status"] == "rejected_overlap"
    assert by_rule[0]["overlapped_by"]["rule_index"] == 1
    assert by_rule[0]["reason"]


def test_invalid_regex_reported_without_affecting_others():
    payload = {
        "text": "电话 13800001111",
        "rules": [
            {"id": "bad", "pattern": "(", "priority": 0, "replacement": "X"},
            {"id": "phone", "pattern": r"1\d{10}", "priority": 0,
             "replacement": "[手机]"},
        ],
    }
    resp = client.post("/api/v1/desensitize", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["masked_text"] == "电话 [手机]"
    assert len(data["rule_errors"]) == 1
    err = data["rule_errors"][0]
    assert err["rule_index"] == 0
    assert err["rule_id"] == "bad"
    assert err["stage"] == "compile"
    assert data["stats"]["rules_invalid"] == 1


def test_empty_rules_returns_original_text():
    resp = client.post("/api/v1/desensitize", json={"text": "原文", "rules": []})
    assert resp.status_code == 200
    data = resp.json()
    assert data["masked_text"] == "原文"
    assert data["matches"] == []


def test_missing_required_field_returns_422():
    resp = client.post("/api/v1/desensitize",
                       json={"text": "x", "rules": [{"pattern": "a"}]})
    assert resp.status_code == 422
