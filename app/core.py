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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

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
