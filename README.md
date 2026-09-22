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

`scripts/roundtrip_gate.py` 校验"写入 → 落库 → 取回"链路一致。**依赖已启动的 API**（脚本只走 HTTP，纯标准库、无额外依赖）：

```bash
docker compose up --build           # 先起 API（或本地: cd backend && uvicorn app.main:app --port 9400）
python3 scripts/roundtrip_gate.py   # 跑门禁；默认打 http://localhost:9400，可用 API_BASE 环境变量覆盖
```

默认模式流程：

1. 以已知本金/利率/期数（100 万 / 3.5% / 360 期）`POST /api/schedule`（`persist=true`），拿到 `run_id` 与回包月供；
2. 立刻从 `GET /api/history` 按 `run_id` 取回该行，断言落库 `result.monthly_payment`（钉选月供）与写入回包月供一致，且落库 `input` 的本金/利率/期数与写入值一致；
3. 再发两笔 `persist=false`，断言历史条数不增加。

退出码：`0` 通过（连续运行任意次均为 0）；`1` 一致性校验失败；`2` 环境/用法错误（API 不可达、run 不存在等）。

### 篡改复检（负向验证）

门禁只通过 HTTP 访问 API，不改引擎公式、不碰数据库文件。人为改库可验证门禁能抓住不一致——以篡改 `run_id=4` 的落库月供为例：

```bash
# docker compose 部署（库在 backend 容器 /data/app.db）：
docker compose exec backend python -c "import sqlite3,json; c=sqlite3.connect('/data/app.db'); d=json.loads(c.execute('SELECT result_json FROM calc_runs WHERE id=4').fetchone()[0]); d['monthly_payment']+=0.01; c.execute('UPDATE calc_runs SET result_json=? WHERE id=4',(json.dumps(d),)); c.commit()"
# 本地 uvicorn（库在 backend/data/app.db，或 $DATA_DIR/app.db）：把连接路径换掉即可

python3 scripts/roundtrip_gate.py --run-id 4
# 须退出码 1，并点名: FAIL run_id=4 field=result.monthly_payment ...
```

恢复该条（把月供改回去）后 `--run-id 4` 须通过；或直接再跑默认模式（新写入一笔）亦须通过。
