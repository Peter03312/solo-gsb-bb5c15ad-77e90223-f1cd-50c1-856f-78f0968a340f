# 匿名用户实验分流服务

基于 **Python + FastAPI + PostgreSQL** 的匿名用户实验分流（A/B 分桶）服务。

## 核心概念

| 概念 | 说明 |
|---|---|
| 互斥层（Layer） | 每层固定 **10000 个桶**（`[0, 10000)`），创建时生成**固定盐**，永不变更 |
| 实验（Experiment） | 属于某一层，配置：半开桶区间 `[bucket_start, bucket_end)`、属性键值**全匹配**条件、组权重（**总和=10000**）。同层 ACTIVE 实验的桶区间**互不重叠**，发布时原子校验 |
| 分流（Assignment） | `bucket = sha256(layer_salt \| "bucket" \| user_key) mod 10000` 稳定落桶；命中区间且条件全匹配后，用独立哈希域按组权重确定性分组。返回**实验版本、组别、分配时规则快照**；无命中明确返回 `assigned=false` |
| 分配持久性 | 同层同用户最多一条 **active** 分配（数据库部分唯一索引保证）。修改桶区间/权重/条件会 bump 版本，但**只影响尚未分配的用户**；已分配用户永远返回旧分配与旧快照 |
| 结束（End） | 结束后不再分新用户；旧分配变为 `closed`、**仍可查询**；用户可进入同层另一实验 |
| 曝光（Exposure） | 凭 `assignment_id + event_no` 登记。**同号同内容重试幂等**（返回 `deduplicated=true`）；**同号异内容 409 冲突**；**无效或已结束分配的曝光拒绝**（404/409） |

## 快速开始（一键）

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 16 并等其健康后启动应用；应用启动时自动建表建索引（含部分唯一索引），无需手工迁移。

- 服务地址： **http://localhost:8000**
- 交互式文档： http://localhost:8000/docs （Swagger UI）
- 健康检查： http://localhost:8000/health
- PostgreSQL： `localhost:5432`（用户/密码/库：`exp` / `exp` / `experiments`）

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://exp:exp@localhost:5432/experiments` | SQLAlchemy 连接串；Compose 中注入为 `...@db:5432/...` |

端口映射（`docker-compose.yml`）：应用 `8000:8000`，数据库 `5432:5432`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/layers` | 创建互斥层（自动生成固定盐） |
| GET | `/layers` / `/layers/{id}` | 查询层 |
| POST | `/experiments` | 创建实验（draft） |
| GET | `/experiments/{id}` / `/layers/{id}/experiments` | 查询实验 |
| POST | `/experiments/{id}/publish` | 发布：原子校验桶占用，版本 +1 |
| PUT | `/experiments/{id}` | 修改桶/权重/条件（active 实验会原子重校验并 bump 版本；只影响未分配用户） |
| POST | `/experiments/{id}/end` | 结束实验：不再分新用户，旧分配置为 closed |
| POST | `/layers/{id}/rearrangements` | **同层批量重排**：一次调整多个实验的桶区间（可交换区间、可纳入草稿），整批原子校验提交 |
| POST | `/layers/{id}/assignments` | 分流：入参 `user_key` + `attributes` |
| GET | `/assignments/{id}` | 查询单条分配（含已结束的旧分配） |
| GET | `/layers/{id}/assignments?user_key=...` | 查询用户在该层的全部分配 |
| POST | `/exposures` | 曝光登记（幂等） |

错误码：`404` 资源不存在；`409` 业务冲突（桶重叠 / 已结束 / 曝光内容冲突等）；`422` 参数校验失败（区间非法、权重和≠10000 等）。

## 请求示例（完整流程）

```bash
BASE=http://localhost:8000

# 1. 创建互斥层（10000 桶，固定盐）
LAYER=$(curl -s $BASE/layers -H 'Content-Type: application/json' \
  -d '{"name":"homepage"}')
LID=$(echo $LAYER | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2. 创建实验：桶 [0,5000)，条件 country=US，两组各 5000 权重
EXP=$(curl -s $BASE/experiments -H 'Content-Type: application/json' -d "{
  \"layer_id\": \"$LID\",
  \"name\": \"new-banner\",
  \"bucket_start\": 0, \"bucket_end\": 5000,
  \"conditions\": {\"country\": \"US\"},
  \"groups\": [{\"name\":\"control\",\"weight\":5000},
              {\"name\":\"treatment\",\"weight\":5000}]}")
EID=$(echo $EXP | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 3. 发布（原子校验桶占用；与 ACTIVE 实验重叠则 409）
curl -s -X POST $BASE/experiments/$EID/publish

# 4. 分流
curl -s $BASE/layers/$LID/assignments -H 'Content-Type: application/json' \
  -d '{"user_key":"anon-123","attributes":{"country":"US"}}')
# 命中 => {"assigned":true,"assignment_id":"...","experiment_version":1,
#          "group":"treatment","bucket":..., "snapshot":{...}, ...}
# 未命中 => {"assigned":false,"bucket":...,"reason":"no_experiment_covers_bucket"
#           | "conditions_not_matched", ...}

# 5. 曝光（同 assignment_id + event_no 重试幂等；异内容 409）
curl -s $BASE/exposures -H 'Content-Type: application/json' -d '{
  "assignment_id": "<assignment_id>", "event_no": "evt-001",
  "content": {"banner": "v1"}}'

# 6. 修改权重（version 2；已分配用户不受影响，新用户按新规则）
curl -s -X PUT $BASE/experiments/$EID -H 'Content-Type: application/json' \
  -d '{"groups":[{"name":"control","weight":3000},
                 {"name":"treatment","weight":7000}]}'

# 7. 结束实验（不再分新用户；旧分配可查；用户可进入同层其他实验）
curl -s -X POST $BASE/experiments/$EID/end

# 8. 查询旧分配 / 用户全部分配
curl -s $BASE/assignments/<assignment_id>
curl -s "$BASE/layers/$LID/assignments?user_key=anon-123"
```

## 同层批量重排（运营一键调桶）

运营在**同一互斥层**内一次重排多个实验的桶区间：请求指定层、待调整实验列表，
以及每个实验的 `expected_version`（乐观版本，草稿为 `0`，可从 `GET /experiments/{id}`
获取）与目标半开区间 `[bucket_start, bucket_end)`。支持：

- **同批交换**两个 active 实验的现有区间（逐个改会触发 409，批量内按最终布局统一校验）；
- **把草稿纳入发布**：列表中的 draft 在同一事务内发布为 active（版本 `0 -> 1`）。

整批在**单个事务**内提交，校验全部通过才落库，任一条件不满足则整批失败、**无部分变更**：

| 失败情形 | 状态码 |
|---|---|
| 批次内 `experiment_id` 重复；区间非法（越界 / 空区间） | `422` |
| `expected_version` 过期（与当前版本不一致） | `409` |
| 实验不属于该层 | `409` |
| 实验状态已结束（ended） | `409` |
| 列表内目标区间互相重叠，或与**未列入**的 active 实验重叠 | `409` |
| 实验 / 层不存在 | `404` |

成功返回每个实验的新状态、版本、区间，以及 `changed`（该实验版本是否实际递增）：
区间真正移动的 active 实验 `version + 1`；草稿发布 `0 -> 1`；区间无变化的实验
`changed=false` 且版本不动。**已分配用户仍返回原分配与原快照；新用户按切换后的布局
分流；并发切换不会产生区间外的新分配。**

```bash
# 设层内 A 占 [0,2500)（version 1）、B 占 [2500,5000)（version 1），C 为草稿
# 一次请求：A、B 交换区间，并把草稿 C 发布到 [7500,10000)
curl -s -X POST $BASE/layers/$LID/rearrangements \
  -H 'Content-Type: application/json' -d '{
    "items": [
      {"experiment_id": "<A-id>", "expected_version": 1,
       "bucket_start": 2500, "bucket_end": 5000},
      {"experiment_id": "<B-id>", "expected_version": 1,
       "bucket_start": 0,    "bucket_end": 2500},
      {"experiment_id": "<C-id>", "expected_version": 0,
       "bucket_start": 7500, "bucket_end": 10000}
    ]}'
# 200 => {"layer_id":"...","experiments":[
#   {"id":"<A-id>","status":"active","version":2,"bucket_start":2500,"bucket_end":5000,"changed":true},
#   {"id":"<B-id>","status":"active","version":2,"bucket_start":0,   "bucket_end":2500,"changed":true},
#   {"id":"<C-id>","status":"active","version":1,"bucket_start":7500,"bucket_end":10000,"changed":true}]}
#
# 409 示例：版本过期
#   {"detail":"experiment ... version conflict: expected 1, current 2"}
# 409 示例：与未列入的活跃实验重叠
#   {"detail":"bucket range [0, 1000) of experiment ... overlaps active experiment ... [0, 2500) not in the batch"}
```

调用方先用 `GET /experiments/{id}` 读取 `version` 作为 `expected_version`；并发下
若有他人先改成功，本批得到 `409`，重新读取最新版本/布局后重试即可。

## 并发与一致性设计

- **并发发布不产生桶重叠**：发布/修改/批量重排在同一事务内先锁层行（`SELECT ... FOR UPDATE`）串行化，再做区间重叠校验并提交。
- **批量重排整批原子**：批量重排先锁层行，再按实验 id 顺序锁全部实验行（与结束事务锁序一致，避免死锁），随后按**最终布局**统一校验：列表内区间两两不重叠（允许交换），且每个目标区间不与未列入的 active 实验重叠。任一失败整批回滚；全部通过后一次性写入提交。
- **全局「层 → 实验」加锁顺序**：分流在锁实验行（`FOR SHARE`）前先锁层行（`FOR SHARE`）。写入方（发布/修改/重排）拿层行 `FOR UPDATE` 后等待在途分流提交；分流若在写入提交后才拿到层锁，读到的已是切换后的新布局——因此并发切换不会产生落在区间外的新分配，也不会与写入方形成锁序环（死锁）。
- **同用户不会留下两个有效分配**：`(layer_id, user_key) WHERE status='active'` 部分唯一索引兜底；分流时锁实验行 `FOR SHARE`，结束实验锁 `FOR UPDATE` 并在同一事务内关闭全部分配——分流与结束互斥，要么先分后被关闭，要么结束后不再分。
- **同用户并发分流**：唯一索引冲突的一方回滚后重读，返回胜者的同一条分配。
- **曝光幂等**：`(assignment_id, event_no)` 唯一约束；登记前锁分配行，与"结束"串行化，已结束分配的曝光一律拒绝。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://exp:exp@localhost:5432/experiments'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 冒烟测试

服务运行后执行（仅依赖 Python 标准库），覆盖发布校验、稳定分桶、条件匹配、分配持久化、结束语义、曝光幂等/冲突及并发场景：

```bash
BASE_URL=http://localhost:8000 python3 scripts/smoke_test.py
```

批量重排专项测试（交换区间、草稿纳入发布、整批失败无部分变更、旧/新用户分流语义，
以及重排 vs 分流、重排 vs 重排的并发不产生区间外分配）：

```bash
BASE_URL=http://localhost:8000 python3 scripts/rearrange_test.py
```
