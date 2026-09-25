"""累计水表读数用量复核与账单总量对账 API。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import List, Literal, Optional, Tuple

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core import (
    STATUS_AMBIGUOUS,
    STATUS_NO_SOLUTION,
    STATUS_OK,
    SegmentSolution,
    reconcile_bill_total,
    solve_segment,
)

app = FastAPI(
    title="Water Meter Usage Verifier",
    version="1.1.0",
    description=(
        "累计水表读数用量复核：判定相邻读数间的整圈回绕次数是否唯一，唯一时给出分段与总用量；"
        "账单总量对账：以首尾读数差与账单总用量约束整条序列的回绕次数总和，"
        "收紧各段可行圈数范围，仅对圈数唯一的区间给出用量。"
    ),
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class Reading(BaseModel):
    """一条秒精度 UTC 时间戳及其对应读数。"""

    timestamp: datetime = Field(
        description="秒精度时间戳，须带时区（建议 UTC，如 2026-09-24T00:00:00Z）"
    )
    value: int = Field(ge=0, description="计数器读数，须满足 0 <= value < modulus")

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp 必须携带时区信息（UTC 时间戳）")
        if v.microsecond != 0:
            raise ValueError("timestamp 必须为秒精度，不允许亚秒部分")
        return v.astimezone(timezone.utc)


class VerifyRequest(BaseModel):
    modulus: int = Field(ge=1, description="计数器模数（回绕周期）")
    max_rate_per_second: Decimal = Field(
        ge=0, description="每秒最大计数增量，支持小数（按精确分数计算）"
    )
    readings: List[Reading] = Field(
        min_length=2, description="按时间排列的读数序列，至少 2 条"
    )

    @model_validator(mode="after")
    def _check_readings(self) -> "VerifyRequest":
        for r in self.readings:
            if r.value >= self.modulus:
                raise ValueError(
                    f"读数 {r.value} 超出模数范围 [0, {self.modulus})"
                )
        for prev, cur in zip(self.readings, self.readings[1:]):
            if cur.timestamp <= prev.timestamp:
                raise ValueError(
                    "时间戳必须严格递增："
                    f"{prev.timestamp.isoformat()} 之后出现 {cur.timestamp.isoformat()}"
                )
        return self


class ReconcileRequest(VerifyRequest):
    """账单总量对账请求：复用读数、模数与速率校验，附加账单总用量。"""

    billed_total: int = Field(ge=0, description="账单总用量（非负整数）")


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------

class SegmentOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    wraps: int = Field(description="判定的整圈回绕次数")
    usage: int = Field(description="该区间用量（计数增量）")


class SegmentErrorOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    reason: Literal["no_solution", "ambiguous"]
    detail: str
    min_wraps: int = Field(description="满足非负增量约束的最小回绕次数")
    max_wraps: int = Field(description="满足速率上限约束的最大回绕次数")


class VerifyResponse(BaseModel):
    status: Literal["ok", "error"]
    modulus: int
    segments: List[SegmentOut] = Field(description="回绕次数唯一的区间")
    errors: List[SegmentErrorOut] = Field(description="无解或多解的出错区间")
    total_usage: Optional[int] = Field(
        description="全部区间唯一时的总用量；存在出错区间时为 null"
    )


class ReconcileSegmentOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    wraps: Optional[int] = Field(description="对账后圈数唯一时填充；否则为 null")
    usage: Optional[int] = Field(description="圈数唯一时的该段用量；否则为 null")
    min_wraps: int = Field(description="对账后仍可行的最小回绕次数")
    max_wraps: int = Field(description="对账后仍可行的最大回绕次数")


class ReconcileErrorOut(BaseModel):
    reason: Literal["segment_no_solution", "incompatible_total", "total_out_of_range"]
    detail: str
    index: Optional[int] = Field(
        default=None, description="segment_no_solution 时的出错区间序号"
    )


class ReconcileResponse(BaseModel):
    status: Literal["ok", "error"]
    modulus: int
    billed_total: int
    required_total_wraps: Optional[int] = Field(
        description="由首尾读数差与账单总量推出的总回绕次数 K；账单与模数不相容时为 null"
    )
    segments: List[ReconcileSegmentOut] = Field(
        description="对账通过时各段收紧后的可行圈数范围；冲突时为空"
    )
    errors: List[ReconcileErrorOut] = Field(description="冲突原因；对账通过时为空")
    total_usage: Optional[int] = Field(
        description="对账通过时等于 billed_total；冲突时为 null"
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/usage/verify", response_model=VerifyResponse)
def verify_usage(req: VerifyRequest) -> VerifyResponse:
    # Decimal -> Fraction 为精确转换，速率上限不产生浮点误差
    max_rate = Fraction(req.max_rate_per_second)

    segments: List[SegmentOut] = []
    errors: List[SegmentErrorOut] = []

    for i, (a, b) in enumerate(zip(req.readings, req.readings[1:])):
        elapsed = int((b.timestamp - a.timestamp).total_seconds())
        sol = solve_segment(i, a.value, b.value, elapsed, req.modulus, max_rate)

        if sol.status == STATUS_OK:
            segments.append(
                SegmentOut(
                    index=i,
                    start_timestamp=a.timestamp,
                    end_timestamp=b.timestamp,
                    start_reading=a.value,
                    end_reading=b.value,
                    elapsed_seconds=elapsed,
                    wraps=sol.wraps,
                    usage=sol.usage,
                )
            )
            continue

        diff = b.value - a.value
        if sol.status == STATUS_NO_SOLUTION:
            min_delta = diff + sol.min_wraps * req.modulus
            detail = (
                f"无解：最可能增量 {min_delta} 已超出该区间 "
                f"{elapsed}s 内允许的最大增量 {sol.max_allowed}"
                f"（k 需同时满足 k>={sol.min_wraps} 与 k<={sol.max_wraps}，区间为空）"
            )
        else:  # STATUS_AMBIGUOUS
            detail = (
                f"多解：回绕次数 k 在 [{sol.min_wraps}, {sol.max_wraps}] 内均满足"
                f"非负增量与速率上限约束，无法唯一确定用量，不做猜测"
            )
        errors.append(
            SegmentErrorOut(
                index=i,
                start_timestamp=a.timestamp,
                end_timestamp=b.timestamp,
                start_reading=a.value,
                end_reading=b.value,
                elapsed_seconds=elapsed,
                reason=sol.status,  # type: ignore[arg-type]
                detail=detail,
                min_wraps=sol.min_wraps,
                max_wraps=sol.max_wraps,
            )
        )

    has_error = bool(errors)
    return VerifyResponse(
        status="error" if has_error else "ok",
        modulus=req.modulus,
        segments=segments,
        errors=errors,
        total_usage=None if has_error else sum(s.usage for s in segments),
    )


@app.post("/v1/usage/reconcile", response_model=ReconcileResponse)
def reconcile_usage(req: ReconcileRequest) -> ReconcileResponse:
    # Decimal -> Fraction 为精确转换，速率上限不产生浮点误差
    max_rate = Fraction(req.max_rate_per_second)

    solved: List[Tuple[Reading, Reading, int, SegmentSolution]] = []
    for i, (a, b) in enumerate(zip(req.readings, req.readings[1:])):
        elapsed = int((b.timestamp - a.timestamp).total_seconds())
        sol = solve_segment(i, a.value, b.value, elapsed, req.modulus, max_rate)
        solved.append((a, b, elapsed, sol))

    reading_span = req.readings[-1].value - req.readings[0].value
    result = reconcile_bill_total(
        [sol for _, _, _, sol in solved],
        req.modulus,
        req.billed_total,
        reading_span,
    )

    if not result.feasible:
        return ReconcileResponse(
            status="error",
            modulus=req.modulus,
            billed_total=req.billed_total,
            required_total_wraps=result.required_total_wraps,
            segments=[],
            errors=[
                ReconcileErrorOut(
                    reason=c.reason,  # type: ignore[arg-type]
                    detail=c.detail,
                    index=c.index,
                )
                for c in result.conflicts
            ],
            total_usage=None,
        )

    segments: List[ReconcileSegmentOut] = []
    for (a, b, elapsed, sol), (lo, hi) in zip(solved, result.ranges or ()):
        if lo == hi:
            wraps: Optional[int] = lo
            usage: Optional[int] = (b.value - a.value) + lo * req.modulus
        else:
            wraps = None
            usage = None
        segments.append(
            ReconcileSegmentOut(
                index=sol.index,
                start_timestamp=a.timestamp,
                end_timestamp=b.timestamp,
                start_reading=a.value,
                end_reading=b.value,
                elapsed_seconds=elapsed,
                wraps=wraps,
                usage=usage,
                min_wraps=lo,
                max_wraps=hi,
            )
        )

    return ReconcileResponse(
        status="ok",
        modulus=req.modulus,
        billed_total=req.billed_total,
        required_total_wraps=result.required_total_wraps,
        segments=segments,
        errors=[],
        total_usage=req.billed_total,
    )
