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


# ---- POST /api/v1/desensitize/pipeline --------------------------------------

PIPELINE_URL = "/api/v1/desensitize/pipeline"


def test_pipeline_happy_path():
    payload = {
        "text": "手机13800001111手机13900002222",
        "stages": [
            {"rules": [
                {"id": "phone", "pattern": r"1\d{10}", "priority": 10,
                 "replacement": "[手机]"},
            ]},
            {"rules": [
                {"id": "token", "pattern": r"\[手机\]", "replacement": "<P>"},
            ]},
        ],
    }
    resp = client.post(PIPELINE_URL, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["final_text"] == "手机<P>手机<P>"
    assert len(data["stages"]) == 2
    st0, st1 = data["stages"]
    assert st0["input_text"] == payload["text"]
    assert st0["output_text"] == "手机[手机]手机[手机]"
    assert st1["input_text"] == "手机[手机]手机[手机]"
    assert st1["output_text"] == data["final_text"]
    # 第 0 阶段：命中原生存内容，可映射回初始原文
    for m in st0["matches"]:
        assert m["generated"] is False
        assert m["source_span"] is not None
        assert m["stage_input_span"] == m["source_span"]
    # 第 1 阶段：命中上一阶段替换的新生内容
    for m in st1["matches"]:
        assert m["generated"] is True
        assert m["source_span"] is None
        assert m["matched_text"] == "[手机]"
        assert m["result_span"]["start"] < m["result_span"]["end"]
    assert data["stats"]["stages_total"] == 2
    assert data["stats"]["matches_applied"] == 4


def test_pipeline_backreference_keeps_source_span():
    payload = {
        "text": "电话 13800001111 结束",
        "stages": [
            {"rules": [{"pattern": r"1(\d{9})1", "replacement": r"[\1]"}]},
            {"rules": [{"pattern": r"\d{9}", "replacement": "NINES"}]},
        ],
    }
    data = client.post(PIPELINE_URL, json=payload).json()
    assert data["final_text"] == "电话 [NINES] 结束"
    m = data["stages"][1]["matches"][0]
    assert m["generated"] is False
    # 回填组对应初始原文中的 9 位数字 [4,13)
    assert m["source_span"] == {"start": 4, "end": 13}


def test_pipeline_overlap_rejection_and_length_change():
    payload = {
        "text": "0123456789",
        "stages": [
            {"rules": [
                {"pattern": r"012", "priority": 5, "replacement": "LONGER"},
                {"pattern": r"\d", "priority": 0, "replacement": "X"},
            ]},
            {"rules": [{"pattern": r"LONGER", "replacement": "Q"}]},
        ],
    }
    data = client.post(PIPELINE_URL, json=payload).json()
    assert data["stages"][0]["output_text"] == "LONGERXXXXXXX"
    assert data["final_text"] == "QXXXXXXX"
    rejected = [m for m in data["stages"][0]["matches"]
                if m["status"] == "rejected_overlap"]
    assert [m["matched_text"] for m in rejected] == ["0", "1", "2"]
    # 被否决命中仍映射回初始原文
    assert rejected[0]["source_span"] == {"start": 0, "end": 1}
    assert rejected[0]["overlapped_by"]["rule_index"] == 0
    # 第二阶段命中的 LONGER 是替换新生内容
    assert data["stages"][1]["matches"][0]["generated"] is True


def test_pipeline_invalid_rule_isolated_per_stage():
    payload = {
        "text": "abc",
        "stages": [
            {"rules": [
                {"id": "bad", "pattern": "(", "replacement": "X"},
                {"id": "a", "pattern": "a", "replacement": "A"},
            ]},
            {"rules": [
                {"id": "bad-tpl", "pattern": "b", "replacement": r"\g<9>"},
            ]},
        ],
    }
    resp = client.post(PIPELINE_URL, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["final_text"] == "Abc"
    assert data["stats"]["rules_invalid"] == 2
    errs0 = data["stages"][0]["rule_errors"]
    errs1 = data["stages"][1]["rule_errors"]
    assert [(e["rule_index"], e["stage_index"], e["stage"]) for e in errs0] == \
        [(0, 0, "compile")]
    assert [(e["rule_index"], e["stage_index"], e["stage"]) for e in errs1] == \
        [(0, 1, "replacement")]
    assert data["stages"][0]["stats"]["rules_invalid"] == 1
    assert data["stages"][1]["stats"]["rules_invalid"] == 1


def test_pipeline_empty_stages_and_empty_text():
    data = client.post(PIPELINE_URL, json={"text": "原文", "stages": []}).json()
    assert data["final_text"] == "原文"
    assert data["stages"] == []
    data2 = client.post(PIPELINE_URL,
                        json={"text": "", "stages": [{"rules": []}]}).json()
    assert data2["final_text"] == ""
    assert len(data2["stages"]) == 1


def test_pipeline_text_too_long_returns_422():
    resp = client.post(PIPELINE_URL,
                       json={"text": "x" * 1_000_001, "stages": []})
    assert resp.status_code == 422


def test_pipeline_rules_limit_per_stage_returns_422(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_RULES", 1)
    resp = client.post(PIPELINE_URL, json={
        "text": "ab",
        "stages": [{"rules": [
            {"pattern": "a", "replacement": "X"},
            {"pattern": "b", "replacement": "Y"},
        ]}],
    })
    assert resp.status_code == 422
    assert "0" in resp.json()["detail"]


def test_pipeline_intermediate_text_too_long_returns_422(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_TEXT_LENGTH", 5)
    resp = client.post(PIPELINE_URL, json={
        "text": "xx",
        "stages": [{"rules": [{"pattern": "x", "replacement": "yyy"}]}],
    })
    assert resp.status_code == 422
    assert "1" in resp.json()["detail"]


def test_pipeline_stages_limit_returns_422(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_STAGES", 1)
    resp = client.post(PIPELINE_URL, json={
        "text": "a",
        "stages": [{"rules": []}, {"rules": []}],
    })
    assert resp.status_code == 422


def test_pipeline_legacy_endpoint_unchanged():
    # 旧端点响应结构保持兼容（不出现 pipeline 专属字段）
    resp = client.post("/api/v1/desensitize", json={
        "text": "abc",
        "rules": [{"pattern": "b", "replacement": "X"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"masked_text", "matches", "rule_errors", "stats"}
    m = body["matches"][0]
    assert set(m.keys()) == {
        "rule_index", "rule_id", "status", "original_span", "result_span",
        "matched_text", "replacement", "reason", "overlapped_by",
    }
    assert body["masked_text"] == "aXc"
