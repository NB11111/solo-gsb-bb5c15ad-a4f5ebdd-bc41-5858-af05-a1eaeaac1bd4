"""接口与判定逻辑测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/v1/usage/verify", json=payload)


# ---------------------------------------------------------------------------
# 正常场景
# ---------------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_no_wrap_unique():
    """无回绕，增量在速率上限内（60s * 10/s = 600，不足以多绕一圈）。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:01:00Z", "value": 600},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 500
    assert body["errors"] == []
    assert body["segments"][0]["wraps"] == 0
    assert body["segments"][0]["usage"] == 500


def test_single_wrap_unique():
    """一次回绕且唯一：900 -> 100，M=1000，10s 内上限 200。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 900},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["wraps"] == 1
    assert body["segments"][0]["usage"] == 200
    assert body["total_usage"] == 200


def test_multi_segment_total():
    """多段混合（回绕 + 不回绕）全部唯一，总用量为各段之和。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 80,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 250},  # +150
                {"timestamp": "2026-09-24T00:00:20Z", "value": 50},   # 回绕 +800
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["usage"] for s in body["segments"]] == [150, 800]
    assert body["segments"][1]["wraps"] == 1
    assert body["total_usage"] == 950


def test_zero_rate_equal_readings():
    """速率为 0 且读数不变：唯一解，用量 0。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 0,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 42},
                {"timestamp": "2026-09-24T01:00:00Z", "value": 42},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 0


def test_fractional_rate_boundary():
    """小数速率按精确分数判定：0.5/s * 10s = 5，增量 5 恰好唯一。"""
    resp = _post(
        {
            "modulus": 100,
            "max_rate_per_second": 0.5,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 10},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 15},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 5


def test_timezone_offset_normalized_to_utc():
    """带偏移的时区会被换算为 UTC 后再判定。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T08:00:00+08:00", "value": 0},
                {"timestamp": "2026-09-24T00:00:30Z", "value": 5},  # 同一时刻后 30s
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["elapsed_seconds"] == 30
    assert body["total_usage"] == 5


# ---------------------------------------------------------------------------
# 无解 / 多解
# ---------------------------------------------------------------------------

def test_no_solution():
    """增量超过速率上限：1s 内最多 10，但需 800。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 900},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert body["segments"] == []
    err = body["errors"][0]
    assert err["reason"] == "no_solution"
    assert err["index"] == 0
    assert err["max_wraps"] < err["min_wraps"]


def test_ambiguous():
    """时间跨度过大导致多个回绕次数都可行。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},  # 1000s, 上限 10000
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    err = body["errors"][0]
    assert err["reason"] == "ambiguous"
    assert err["min_wraps"] == 0
    assert err["max_wraps"] == 9  # k=0..9 均满足


def test_mixed_segments_partial_errors():
    """部分区间唯一、部分出错：返回已判定分段与出错区间，总用量为 null。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},   # 唯一 +200
                {"timestamp": "2026-09-24T00:00:11Z", "value": 900},   # 无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert len(body["segments"]) == 1
    assert body["segments"][0]["usage"] == 200
    assert len(body["errors"]) == 1
    assert body["errors"][0]["index"] == 1
    assert body["errors"][0]["reason"] == "no_solution"


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------

def test_timestamps_not_strictly_increasing():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reading_out_of_modulus_range():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422


def test_naive_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_subsecond_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00.500Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_too_few_readings():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [{"timestamp": "2026-09-24T00:00:00Z", "value": 100}],
        }
    )
    assert resp.status_code == 422


def test_negative_rate_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422
