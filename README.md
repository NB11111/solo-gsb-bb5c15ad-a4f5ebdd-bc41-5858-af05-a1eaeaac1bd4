# 累计水表读数用量复核 API

基于 FastAPI 的用量复核服务，提供两个接口：

- **用量复核**（`/v1/usage/verify`）：输入一组带秒精度 UTC 时间戳的累计水表读数、计数器模数与每秒最大计数增量，对每段相邻读数判定满足**非负增量**与**速率上限**的整圈回绕次数是否唯一。唯一时返回分段用量与总用量；无解或多解时返回出错区间与原因，**不猜测圈数**。
- **账单总量对账**（`/v1/usage/reconcile`）：在相同读数校验基础上附加**账单总用量**（非负整数），以首尾读数差与账单总量约束整条序列的回绕次数总和，通过整数区间运算收紧每段仍可行的圈数范围（不枚举组合）。只对圈数已唯一的区间给出确定用量，其余保留范围；某段原本无解、账单总量与模数不相容或超出可行总量时返回冲突原因。

## 快速启动（Docker，一键）

```bash
docker compose up --build
```

依赖在镜像构建时自动安装，无需本地环境。启动后：

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`

### 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 宿主机端口 | `8000` | 在 `docker-compose.yml` 的 `ports` 中修改，如 `"8080:8000"` |
| 容器内端口 | `8000` | 由 `Dockerfile` 的 `CMD` 指定，一般无需修改 |

无其他必需配置（无数据库、无外部依赖）。

## 本地运行（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## API 说明

### `POST /v1/usage/verify`

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 20,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 900 },
    { "timestamp": "2026-09-24T00:00:10Z", "value": 100 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `modulus` | 整数 | `>= 1`，计数器模数（回绕周期） |
| `max_rate_per_second` | 数值 | `>= 0`，每秒最大计数增量，支持小数（按精确分数计算，无浮点误差） |
| `readings` | 数组 | 至少 2 条；`timestamp` 为秒精度且带时区（建议 UTC，其他时区会换算为 UTC），全序列**严格递增**；`value` 为整数且 `0 <= value < modulus` |

**响应（全部区间唯一，`200`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:00:10Z",
      "start_reading": 900,
      "end_reading": 100,
      "elapsed_seconds": 10,
      "wraps": 1,
      "usage": 200
    }
  ],
  "errors": [],
  "total_usage": 200
}
```

**响应（存在出错区间，`200`）**

```json
{
  "status": "error",
  "modulus": 1000,
  "segments": [],
  "errors": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:16:40Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "reason": "ambiguous",
      "detail": "多解：回绕次数 k 在 [0, 9] 内均满足非负增量与速率上限约束，无法唯一确定用量，不做猜测",
      "min_wraps": 0,
      "max_wraps": 9
    }
  ],
  "total_usage": null
}
```

- `reason = "no_solution"`：没有任何回绕次数同时满足两条约束（通常是增量超过速率上限）。
- `reason = "ambiguous"`：存在多个可行回绕次数，无法唯一确定用量。
- 存在出错区间时，`segments` 仍返回其余唯一判定的区间，但 `total_usage` 为 `null`。

**请求校验失败（`422`）**：时间戳未严格递增、缺时区、含亚秒、读数越界、参数非法等，返回 FastAPI 标准错误明细。

### `POST /v1/usage/reconcile`

账单总量对账。请求字段在 `/v1/usage/verify` 基础上增加 `billed_total`，校验规则完全复用。

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 20,
  "billed_total": 4400,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 900 },
    { "timestamp": "2026-09-24T00:00:10Z", "value": 100 },
    { "timestamp": "2026-09-24T00:16:50Z", "value": 200 },
    { "timestamp": "2026-09-24T00:33:30Z", "value": 300 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `billed_total` | 整数 | `>= 0`，账单总用量 |
| 其余字段 | — | 同 `/v1/usage/verify` |

**响应（对账通过，`200`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "billed_total": 4400,
  "required_total_wraps": 5,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:00:10Z",
      "start_reading": 900,
      "end_reading": 100,
      "elapsed_seconds": 10,
      "wraps": 1,
      "usage": 200,
      "min_wraps": 1,
      "max_wraps": 1
    },
    {
      "index": 1,
      "start_timestamp": "2026-09-24T00:00:10Z",
      "end_timestamp": "2026-09-24T00:16:50Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "wraps": null,
      "usage": null,
      "min_wraps": 0,
      "max_wraps": 4
    },
    {
      "index": 2,
      "start_timestamp": "2026-09-24T00:16:50Z",
      "end_timestamp": "2026-09-24T00:33:30Z",
      "start_reading": 200,
      "end_reading": 300,
      "elapsed_seconds": 1000,
      "wraps": null,
      "usage": null,
      "min_wraps": 0,
      "max_wraps": 4
    }
  ],
  "errors": [],
  "total_usage": 4400
}
```

- `required_total_wraps`：由首尾读数差与账单总量推出的总回绕次数 `K`。
- 对账后 `min_wraps == max_wraps` 的区间圈数唯一，给出 `wraps` 与 `usage`；其余区间保留收紧后的范围（`wraps`/`usage` 为 `null`），**不猜测**。
- 对账通过时 `total_usage` 等于 `billed_total`。

**响应（存在冲突，`200`）**

```json
{
  "status": "error",
  "modulus": 1000,
  "billed_total": 150,
  "required_total_wraps": null,
  "segments": [],
  "errors": [
    {
      "reason": "incompatible_total",
      "detail": "账单总量与模数不相容：总用量 = 首尾读数差(100) + k*1000（k 为整数），账单总量 150 与首尾读数差的差值 50 不能被 1000 整除",
      "index": null
    }
  ],
  "total_usage": null
}
```

- `reason = "segment_no_solution"`：某段原本无解（`index` 指出出错区间），对账前提不成立。
- `reason = "incompatible_total"`：账单总量与模数不相容，无法表示为 `首尾读数差 + k * modulus`。
- `reason = "total_out_of_range"`：账单总量对应的总圈数超出各段可行区间之和（即可行总用量范围之外）。
- 存在冲突时 `segments` 为空、`total_usage` 为 `null`。

**请求校验失败（`422`）**：同 `/v1/usage/verify`，另含 `billed_total` 为负数或缺失等情况。

## 判定规则

对每段相邻读数 `(r1, t1) -> (r2, t2)`，真实增量为 `delta = (r2 - r1) + k * modulus`，其中 `k >= 0` 为整圈回绕次数。约束：

1. 非负增量：`delta >= 0`
2. 速率上限：`delta <= max_rate_per_second * (t2 - t1)`

可行 `k` 构成连续整数区间 `[k_min, k_max]`：

- `k_max < k_min` → 无解（`no_solution`）
- `k_max == k_min` → 唯一解，取该 `k` 计算用量
- `k_max > k_min` → 多解（`ambiguous`），不猜测圈数

### 账单总量对账

整条序列的总用量为 `total = (r_last - r_first) + K * modulus`，其中 `K` 为各段回绕次数之和，故账单总量 `billed_total` 唯一确定 `K = (billed_total - (r_last - r_first)) / modulus`。判定顺序：

1. 某段原本无解 → `segment_no_solution`
2. `(billed_total - 首尾读数差)` 不能被 `modulus` 整除 → `incompatible_total`
3. `K` 落在 `[sum(k_min), sum(k_max)]` 之外 → `total_out_of_range`
4. 否则可行：整数区间连续，必有组合达成 `K`，无需枚举。用区间运算收紧每段可行圈数范围：

   ```
   lo_i = max(k_min_i, K - (K_max_total - k_max_i))
   hi_i = min(k_max_i, K - (K_min_total - k_min_i))
   ```

   收紧后 `lo_i == hi_i` 的区间圈数唯一、给出确定用量；其余保留范围，不猜测。

## 项目结构

```
app/
  main.py   # FastAPI 应用、请求/响应模型与校验（复核 + 账单总量对账）
  core.py   # 回绕次数判定与账单总量对账逻辑（Fraction 精确计算、整数区间运算）
tests/
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt        # 运行时依赖
requirements-dev.txt    # 测试依赖
```
