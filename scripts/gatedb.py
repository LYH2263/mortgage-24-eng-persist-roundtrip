#!/usr/bin/env python3
"""门禁配套的库操作小工具（仅用于演练篡改/恢复，业务代码不会用到）。

连库方式（按此优先级解析 SQLite 路径）：
  1. 环境变量 MORTGAGE_DB 指向可直接打开的 app.db
     （本地 uvicorn 场景，例如 MORTGAGE_DB=backend/data/app.db）；
  2. 否则取容器内 DATA_DIR/app.db（docker 场景，DATA_DIR=/data）。

在 docker compose 部署里，把本脚本经 stdin 喂给容器内的 python 即可，例如：
    docker compose exec -T backend python - count < scripts/gatedb.py
    docker compose exec -T backend python - tamper 5 9999.99 < scripts/gatedb.py
    docker compose exec -T backend python - restore 5       < scripts/gatedb.py

子命令：
  count                          返回 calc_runs 条数
  get <run_id> [field]          打印该条 result_json（或其中某个字段）
  tamper <run_id> <new_value>   把 result.monthly_payment 改成 new_value，
                                原值备份到侧表 gate_backup（幂等，不覆盖首份备份）
  restore <run_id>              用备份把 result.monthly_payment 精确恢复
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


def resolve_db_path() -> str:
    db = os.environ.get("MORTGAGE_DB", "").strip()
    if db:
        return db
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return str(Path(data_dir) / "app.db")
    raise SystemExit("请设置 MORTGAGE_DB=<app.db 路径>（或在容器内经 DATA_DIR 推导）")


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(resolve_db_path())
    c.row_factory = sqlite3.Row
    return c


def ensure_backup_table(c: sqlite3.Connection) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS gate_backup("
        "run_id INTEGER PRIMARY KEY, original_result_json TEXT NOT NULL)"
    )


def row_result(c: sqlite3.Connection, run_id: int) -> dict:
    row = c.execute("SELECT * FROM calc_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"calc_runs 中不存在 id={run_id}")
    return json.loads(row["result_json"])


def cmd_count() -> None:
    with connect() as c:
        n = c.execute("SELECT COUNT(*) n FROM calc_runs").fetchone()["n"]
    print(n)


def cmd_get(run_id: int, field: str | None) -> None:
    with connect() as c:
        result = row_result(c, run_id)
    print(json.dumps(result, ensure_ascii=False) if field is None else result.get(field))


def cmd_tamper(run_id: int, new_value: float) -> None:
    with connect() as c:
        ensure_backup_table(c)
        result = row_result(c, run_id)
        if c.execute("SELECT 1 FROM gate_backup WHERE run_id=?", (run_id,)).fetchone() is None:
            # 只备份第一次篡改前的原始值，保证 restore 总能回到门禁写入时的值。
            c.execute(
                "INSERT INTO gate_backup(run_id, original_result_json) VALUES(?,?)",
                (run_id, json.dumps(result, ensure_ascii=False)),
            )
        result["monthly_payment"] = new_value
        c.execute("UPDATE calc_runs SET result_json=? WHERE id=?",
                  (json.dumps(result, ensure_ascii=False), run_id))
        c.commit()
    print(f"tampered run_id={run_id} result.monthly_payment -> {new_value}")


def cmd_restore(run_id: int) -> None:
    with connect() as c:
        ensure_backup_table(c)
        b = c.execute(
            "SELECT original_result_json FROM gate_backup WHERE run_id=?", (run_id,)
        ).fetchone()
        if b is None:
            raise SystemExit(f"gate_backup 中没有 run_id={run_id} 的备份，无法恢复")
        c.execute("UPDATE calc_runs SET result_json=? WHERE id=?",
                  (b["original_result_json"], run_id))
        c.execute("DELETE FROM gate_backup WHERE run_id=?", (run_id,))
        c.commit()
    print(f"restored run_id={run_id} result.monthly_payment 已还原")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    op = args[0]
    if op == "count":
        cmd_count()
    elif op == "get" and len(args) >= 2:
        cmd_get(int(args[1]), args[2] if len(args) > 2 else None)
    elif op == "tamper" and len(args) == 3:
        cmd_tamper(int(args[1]), float(args[2]))
    elif op == "restore" and len(args) == 2:
        cmd_restore(int(args[1]))
    else:
        raise SystemExit(f"无法解析参数：{args}\n\n{__doc__}")


if __name__ == "__main__":
    main()
