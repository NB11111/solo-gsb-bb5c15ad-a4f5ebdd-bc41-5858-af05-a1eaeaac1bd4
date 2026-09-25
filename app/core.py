"""用量复核核心判定逻辑。

对每段相邻读数 (r1, r2)，真实增量为 delta = (r2 - r1) + k * modulus，
其中 k 为整圈回绕次数（非负整数）。约束条件：

    delta >= 0                          （非负增量）
    delta <= max_rate * elapsed_seconds （速率上限）

所有满足约束的 k 构成一个连续整数区间 [k_min, k_max]：

    k_min = 满足 diff + k*M >= 0 的最小非负整数
    k_max = 满足 diff + k*M <= max_delta 的最大整数

判定：
    k_max <  k_min  -> 无解 (no_solution)
    k_max == k_min  -> 唯一解，取该 k
    k_max >  k_min  -> 多解 (ambiguous)，不猜测圈数

速率上限使用 Fraction 精确计算，避免浮点边界误差。

账单总量对账：整条序列的总用量为

    total = (r_last - r_first) + K * modulus   （K 为各段回绕次数之和）

故账单总量 billed_total 唯一确定 K = (billed_total - (r_last - r_first)) / modulus。
K 为非负整数且落在 [sum(k_min), sum(k_max)] 内时可行（整数区间连续，此时必有
组合达成 K，无需枚举），并用区间运算收紧每段可行圈数范围：

    lo_i = max(k_min_i, K - (K_max_total - k_max_i))
    hi_i = min(k_max_i, K - (K_min_total - k_min_i))

收紧后 lo_i == hi_i 的段圈数唯一、可确定用量；其余保留范围，不猜测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Sequence, Tuple

STATUS_OK = "ok"
STATUS_NO_SOLUTION = "no_solution"
STATUS_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SegmentSolution:
    """单个相邻读数区间的判定结果。"""

    index: int
    status: str
    elapsed_seconds: int
    # 唯一解时填充
    wraps: Optional[int]
    usage: Optional[int]
    # 可行回绕次数区间 [min_wraps, max_wraps]；无解时 max_wraps < min_wraps
    min_wraps: int
    max_wraps: int
    max_allowed: Fraction  # 该区间允许的最大增量（速率上限 * 秒数）


def solve_segment(
    index: int,
    start_value: int,
    end_value: int,
    elapsed_seconds: int,
    modulus: int,
    max_rate_per_second: Fraction,
) -> SegmentSolution:
    """判定一段相邻读数的回绕次数是否唯一。"""
    diff = end_value - start_value
    max_allowed = max_rate_per_second * elapsed_seconds

    # k_min: diff + k*M >= 0 的最小非负整数 k
    if diff >= 0:
        k_min = 0
    else:
        # ceil(-diff / M)，此时 0 < -diff < M，结果恒为 1
        k_min = (-diff + modulus - 1) // modulus

    # k_max: diff + k*M <= max_allowed 的最大整数 k
    k_max = math.floor((max_allowed - diff) / modulus)

    if k_max < k_min:
        status, wraps, usage = STATUS_NO_SOLUTION, None, None
    elif k_max == k_min:
        status, wraps = STATUS_OK, k_min
        usage = diff + k_min * modulus
    else:
        status, wraps, usage = STATUS_AMBIGUOUS, None, None

    return SegmentSolution(
        index=index,
        status=status,
        elapsed_seconds=elapsed_seconds,
        wraps=wraps,
        usage=usage,
        min_wraps=k_min,
        max_wraps=k_max,
        max_allowed=max_allowed,
    )


# ---------------------------------------------------------------------------
# 账单总量对账
# ---------------------------------------------------------------------------

REASON_SEGMENT_NO_SOLUTION = "segment_no_solution"
REASON_INCOMPATIBLE_TOTAL = "incompatible_total"
REASON_TOTAL_OUT_OF_RANGE = "total_out_of_range"


@dataclass(frozen=True)
class ReconcileConflict:
    """账单总量对账的冲突原因。"""

    reason: str
    detail: str
    index: Optional[int]  # 仅 segment_no_solution 时指向出错区间


@dataclass(frozen=True)
class ReconcileResult:
    """账单总量对账结果。

    feasible 为 True 时，ranges 给出每段收紧后的可行圈数区间 [lo, hi]；
    为 False 时 conflicts 非空，ranges 为 None。
    """

    feasible: bool
    required_total_wraps: Optional[int]  # 账单推出的总圈数 K；与模数不相容时为 None
    ranges: Optional[Tuple[Tuple[int, int], ...]]
    conflicts: Tuple[ReconcileConflict, ...]


def reconcile_bill_total(
    solutions: Sequence[SegmentSolution],
    modulus: int,
    billed_total: int,
    reading_span: int,
) -> ReconcileResult:
    """用首尾读数差与账单总量约束整条序列的回绕次数总和。

    总用量 = reading_span + modulus * K（K 为各段回绕次数之和），账单总量唯一
    确定 K。K 落在各段可行区间之和内时，用整数区间运算收紧每段范围（不枚举
    组合）；否则返回冲突原因。
    """
    # 1) 某段原本无解：对账前提不成立
    conflicts = tuple(
        ReconcileConflict(
            reason=REASON_SEGMENT_NO_SOLUTION,
            detail=(
                f"第 {sol.index} 段原本无解：回绕次数 k 需同时满足 "
                f"k>={sol.min_wraps} 与 k<={sol.max_wraps}，区间为空，"
                "账单总量对账无法进行"
            ),
            index=sol.index,
        )
        for sol in solutions
        if sol.status == STATUS_NO_SOLUTION
    )
    if conflicts:
        return ReconcileResult(False, None, None, conflicts)

    # 2) 账单总量与模数不相容：无法表示为 reading_span + K * modulus
    remainder = billed_total - reading_span
    if remainder % modulus != 0:
        return ReconcileResult(
            False,
            None,
            None,
            (
                ReconcileConflict(
                    reason=REASON_INCOMPATIBLE_TOTAL,
                    detail=(
                        f"账单总量与模数不相容：总用量 = 首尾读数差({reading_span}) "
                        f"+ k*{modulus}（k 为整数），账单总量 {billed_total} 与"
                        f"首尾读数差的差值 {remainder} 不能被 {modulus} 整除"
                    ),
                    index=None,
                ),
            ),
        )

    # 3) 账单推出的总圈数超出各段可行区间之和
    k_total = remainder // modulus
    k_min_total = sum(sol.min_wraps for sol in solutions)
    k_max_total = sum(sol.max_wraps for sol in solutions)
    if not k_min_total <= k_total <= k_max_total:
        min_total = reading_span + k_min_total * modulus
        max_total = reading_span + k_max_total * modulus
        return ReconcileResult(
            False,
            k_total,
            None,
            (
                ReconcileConflict(
                    reason=REASON_TOTAL_OUT_OF_RANGE,
                    detail=(
                        f"账单总量超出可行总量范围：账单总量 {billed_total} 对应"
                        f"总圈数 K={k_total}，但各段可行总圈数范围为 "
                        f"[{k_min_total}, {k_max_total}]（即可行总用量范围为 "
                        f"[{min_total}, {max_total}]）"
                    ),
                    index=None,
                ),
            ),
        )

    # 4) 区间运算收紧每段圈数范围：其余段须能凑出 K - k_i
    ranges = tuple(
        (
            max(sol.min_wraps, k_total - (k_max_total - sol.max_wraps)),
            min(sol.max_wraps, k_total - (k_min_total - sol.min_wraps)),
        )
        for sol in solutions
    )
    return ReconcileResult(True, k_total, ranges, ())
