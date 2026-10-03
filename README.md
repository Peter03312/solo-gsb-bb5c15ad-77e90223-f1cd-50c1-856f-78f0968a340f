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

## 并发与一致性设计

- **并发发布不产生桶重叠**：发布/修改在同一事务内先锁层行（`SELECT ... FOR UPDATE`）串行化，再做区间重叠校验并提交。
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
