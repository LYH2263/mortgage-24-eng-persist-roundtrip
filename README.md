# 15-mortgage（房贷月供）

Mortgage — 等额本息月供与逐期本金利息拆分

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:4400 |
| API | http://localhost:9400 |

## 主链

贷额期限利率 → 等额本息还款表 → 利息合计

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 落库往返一致性门禁

脚本：`scripts/roundtrip_gate.py`（纯 Python 标准库，无需安装第三方包）。

**前置依赖：API 必须已经启动且可访问。** 门禁只走 HTTP，不会替你起服务。

- 默认地址 `http://localhost:9400`（即上表的 API）；地址不同时用环境变量覆盖：
  `MORTGAGE_API_BASE=http://<host>:<port>`。
- 运行前可先 `curl http://localhost:9400/api/health` 确认存活。

```bash
# docker compose 起好后直接跑
python3 scripts/roundtrip_gate.py
```

门禁做的事：

1. 向 `POST /api/schedule` 写入一笔**已知**本金/利率/期数（`persist=true`），
   拿到 `run_id` 与回包月供后，立刻按编号（`id == run_id`）从
   `GET /api/history` 取回该条，断言：
   - 落库的 `result.monthly_payment` 与写入回包月供一致（钉选月供）；
   - 落库的 `input.principal` / `input.annual_rate` / `input.months`
     与写入的本金/利率/期数一致。
2. 再发**两次** `persist=false`，断言 `calc_runs` 条数不再增加，且回包
   `run_id` 为 `null`。
3. 首次运行会把该笔 `run_id` 与回包月供**钉**到
   `scripts/.roundtrip_gate_pin.json`；之后默认复检同一笔。人为改库里该条
   `result.monthly_payment` 后再跑会**失败并点名字段**；恢复该条，或加
   `--rewrite` 改用一笔新写入重新钉选，再跑即通过。

退出码：通过 `0`；断言失败 `1`（stderr 点名字段，如
`result.monthly_payment 不一致…`）；API 不可达等环境问题 `2`。
连续两次运行通过，退出码均为 `0`：

```bash
python3 scripts/roundtrip_gate.py; echo $?   # 0
python3 scripts/roundtrip_gate.py; echo $?   # 0
```

> 门禁只比对「写入回包」与「落库取回」，**不会**为了变绿去改引擎公式。

### 人为篡改 / 恢复演练

`scripts/gatedb.py` 直连 SQLite 改 `result.monthly_payment`（原值备份到侧表
`gate_backup`，可精确恢复）。连库方式二选一：

- 本地 uvicorn：`export MORTGAGE_DB=backend/data/app.db`
  （或你用 `DATA_DIR` 指定的数据目录下的 `app.db`）；
- docker compose 部署：不设 `MORTGAGE_DB`，把脚本经 stdin 喂进容器，例如

```bash
RID=3
docker compose exec -T backend python - tamper  "$RID" 1234.56 < scripts/gatedb.py
python3 scripts/roundtrip_gate.py            # 退出码 1，点名 result.monthly_payment
docker compose exec -T backend python - restore "$RID"        < scripts/gatedb.py
python3 scripts/roundtrip_gate.py            # 退出码 0
# 或不恢复旧条，改用一笔新写入：
python3 scripts/roundtrip_gate.py --rewrite  # 退出码 0
```

一键自校验（自动串起 通过→篡改→失败点名字段→恢复→通过→再篡改→`--rewrite`
通过）。本地设 `MORTGAGE_DB`；docker 部署留空即自动走 `docker compose exec`：

```bash
MORTGAGE_DB=backend/data/app.db bash scripts/selftest_roundtrip.sh
```

