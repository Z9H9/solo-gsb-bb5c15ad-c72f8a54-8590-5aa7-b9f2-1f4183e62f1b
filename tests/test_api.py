"""API 端到端测试（需要 fastapi / httpx；缺依赖时自动跳过）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pytest  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: E402

    from app.main import app  # noqa: E402
except ImportError:  # 纯标准库环境下整体跳过（python -m unittest 亦可运行）
    raise unittest.SkipTest("fastapi/httpx 未安装，跳过 API 端到端测试")

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


# ---------------------------------------------------------------------------
# 多阶段流水线
# ---------------------------------------------------------------------------

def test_pipeline_happy_path_and_stage_chaining():
    payload = {
        "text": "张三 13800001111",
        "stages": [
            {"rules": [{"id": "p", "pattern": r"1\d{10}", "priority": 10,
                        "replacement": "[PHONE]"}]},
            {"rules": [{"id": "g", "pattern": "PHONE", "replacement": "***"}]},
        ],
    }
    resp = client.post("/api/v1/desensitize/pipeline", json=payload)
    assert resp.status_code == 200
    d = resp.json()
    assert d["masked_text"] == "张三 [***]"
    assert [s["masked_text"] for s in d["stages"]] == ["张三 [PHONE]", "张三 [***]"]
    # 阶段 0：命中原文
    m0 = d["stages"][0]["matches"][0]
    assert m0["source_span"] == {"start": 3, "end": 14}
    assert m0["input_span"] == {"start": 3, "end": 14}
    assert m0["generated"] is False
    assert m0["stage_index"] == 0
    # 阶段 1：命中前序替换新生内容，无初始原文区间
    m1 = d["stages"][1]["matches"][0]
    assert m1["input_span"] == {"start": 4, "end": 9}
    assert m1["source_span"] is None
    assert m1["generated"] is True
    # 汇总统计
    assert d["stats"]["stages_total"] == 2
    assert d["stats"]["matches_applied"] == 2
    # 阶段统计的 text_length 是阶段输入长度
    assert d["stages"][1]["stats"]["text_length"] == len("张三 [PHONE]")


def test_pipeline_group_reference_maps_back_to_original():
    payload = {
        "text": "alice@example.com",
        "stages": [
            {"rules": [{"pattern": r"(\w+)@(\w+\.\w+)", "replacement": r"\1::\2"}]},
            {"rules": [{"pattern": "example", "replacement": "EX"}]},
        ],
    }
    d = client.post("/api/v1/desensitize/pipeline", json=payload).json()
    m = d["stages"][1]["matches"][0]
    assert m["source_span"] is not None
    lo, hi = m["source_span"]["start"], m["source_span"]["end"]
    assert "alice@example.com"[lo:hi] == "example"
    assert m["generated"] is False


def test_pipeline_deletion_enclosing_span():
    payload = {
        "text": "ab12cd34ef",
        "stages": [
            {"rules": [{"pattern": r"\d+", "replacement": ""}]},
            {"rules": [{"pattern": "bcde", "priority": 10, "replacement": "Z"}]},
        ],
    }
    d = client.post("/api/v1/desensitize/pipeline", json=payload).json()
    assert d["masked_text"] == "aZf"
    m = d["stages"][1]["matches"][0]
    # 外包含已删除字符：b,c 与 d,e 之间隔着被删的 12/34
    assert m["source_span"] == {"start": 1, "end": 9}


def test_pipeline_invalid_rules_scoped_to_stage():
    payload = {
        "text": "abc123",
        "stages": [
            {"rules": [
                {"id": "bad", "pattern": "(", "replacement": "X"},
                {"pattern": r"\d", "replacement": "#"},
            ]},
            {"rules": [{"pattern": r"#", "replacement": "N"}]},
            {"rules": [{"pattern": "N", "replacement": r"\g<9>"}]},
        ],
    }
    resp = client.post("/api/v1/desensitize/pipeline", json=payload)
    assert resp.status_code == 200
    d = resp.json()
    assert d["masked_text"] == "abcNNN"
    e0 = d["stages"][0]["rule_errors"]
    assert len(e0) == 1 and e0[0]["rule_id"] == "bad" and e0[0]["stage"] == "compile"
    assert d["stages"][1]["rule_errors"] == []
    e2 = d["stages"][2]["rule_errors"]
    assert len(e2) == 1 and e2[0]["stage"] == "replacement"
    assert d["stats"]["rules_invalid"] == 2


def test_pipeline_overlap_rejection_with_generated_content():
    payload = {
        "text": "x123456y",
        "stages": [
            {"rules": [{"pattern": r"\d+", "replacement": "[NN]"}]},
            {"rules": [
                {"pattern": r"\[NN\]", "priority": 0, "replacement": "Z"},
                {"pattern": r"x\[N", "priority": 5, "replacement": "W"},
            ]},
        ],
    }
    d = client.post("/api/v1/desensitize/pipeline", json=payload).json()
    assert d["stages"][1]["masked_text"] == "WN]y"
    rej = [m for m in d["stages"][1]["matches"]
           if m["status"] == "rejected_overlap"][0]
    win = [m for m in d["stages"][1]["matches"] if m["status"] == "applied"][0]
    assert rej["generated"] is True and rej["source_span"] is None
    assert rej["result_span"] == {"start": 1, "end": 3}
    assert win["source_span"] == {"start": 0, "end": 1}
    assert win["generated"] is False
    assert rej["overlapped_by"]["rule_index"] == 1


def test_pipeline_validation_errors():
    # 空 stages
    assert client.post("/api/v1/desensitize/pipeline",
                       json={"text": "x", "stages": []}).status_code == 422
    # 缺 text
    assert client.post("/api/v1/desensitize/pipeline",
                       json={"stages": [{"rules": []}]}).status_code == 422
    # 阶段数超限（MAX_STAGES 默认 20）
    too_many = {"text": "x", "stages": [{"rules": []} for _ in range(21)]}
    r = client.post("/api/v1/desensitize/pipeline", json=too_many)
    assert r.status_code == 422 and "stages" in r.json()["detail"]
    # 单阶段规则数超限（MAX_RULES 默认 500）
    payload = {"text": "x", "stages": [
        {"rules": [{"pattern": "a", "replacement": "b"} for _ in range(501)]}]}
    r = client.post("/api/v1/desensitize/pipeline", json=payload)
    assert r.status_code == 422 and "阶段 #0" in r.json()["detail"]


def test_pipeline_root_lists_new_endpoint():
    info = client.get("/").json()
    assert info["pipeline"] == "/api/v1/desensitize/pipeline"
