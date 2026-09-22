#!/usr/bin/env python3
"""落库往返一致性门禁（round-trip persistence gate）。

依赖一个已启动的 Mortgage API（默认 http://localhost:9400，可用环境变量
API_BASE 覆盖）。脚本只通过 HTTP 与 API 交互：不改引擎公式、不直接读写
数据库文件。

默认模式（写入并校验）：
  1. 以已知本金/利率/期数 POST /api/schedule（persist=true），拿到 run_id
     与回包月供；
  2. 立刻从 GET /api/history 按 run_id 取回该行，断言：
     - 落库 result.monthly_payment（钉选月供）== 写入回包月供；
     - 落库 input 的 principal / annual_rate / months 与写入值一致；
  3. 再发两笔 persist=false，断言历史条数不增加。

--run-id N 模式（复检已有记录，不写入新记录）：
  按该条落库 input 以 persist=false 重算，断言落库钉选月供与重算值一致。
  用于"人为改库后须失败并点名字段"的负向验证。

退出码：0 = 通过；1 = 一致性校验失败；2 = 环境/用法错误（API 不可达、
run 不存在等）。连续运行任意次，通过时退出码均为 0。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 已知用例：本金 / 年利率% / 期数
KNOWN = {"principal": 1_000_000, "annual_rate": 3.5, "months": 360}
HISTORY_LIMIT = 100_000
API_BASE = os.environ.get("API_BASE", "http://localhost:9400").rstrip("/")


def http_json(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(
            f"ERROR: 无法访问 API {API_BASE}（门禁依赖已启动的 API，见 README）: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def history_items() -> list[dict]:
    return http_json("GET", f"/api/history?limit={HISTORY_LIMIT}")["items"]


def find_run(run_id: int) -> dict | None:
    for item in history_items():
        if item.get("id") == run_id:
            return item
    return None


def num_eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def mismatch(run_id, field: str, stored, expected) -> int:
    print(
        f"FAIL run_id={run_id} field={field}: 落库值={stored!r} 期望值={expected!r}",
        file=sys.stderr,
    )
    return 1


def parse_row(run: dict) -> tuple[dict, dict]:
    try:
        return json.loads(run["input_json"]), json.loads(run["result_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAIL run_id={run.get('id')}: 行 JSON 无法解析: {exc}", file=sys.stderr)
        raise SystemExit(1)


def cmd_write_and_verify() -> int:
    # 1) 写入已知用例（persist=true）
    resp = http_json("POST", "/api/schedule", {**KNOWN, "persist": True, "preview_rows": 3})
    run_id = resp.get("run_id")
    if not isinstance(run_id, int):
        print(f"FAIL field=run_id: 写入回包缺少 run_id: {resp!r}", file=sys.stderr)
        return 1
    echoed_pay = resp.get("monthly_payment")

    # 2) 立刻按历史列表取回该行，断言钉选月供与 input 三要素
    run = find_run(run_id)
    if run is None:
        print(f"FAIL run_id={run_id}: 历史列表中找不到刚写入的记录", file=sys.stderr)
        return 1
    stored_input, stored_result = parse_row(run)
    for field, want in KNOWN.items():
        got = stored_input.get(field, "<missing>")
        if not num_eq(got, want):
            return mismatch(run_id, f"input.{field}", got, want)
    got_pay = stored_result.get("monthly_payment", "<missing>")
    if not num_eq(got_pay, echoed_pay):
        return mismatch(run_id, "result.monthly_payment", got_pay, echoed_pay)

    # 3) 再发两笔 persist=false，历史条数不得增加
    before = len(history_items())
    for _ in range(2):
        dry = http_json("POST", "/api/schedule", {**KNOWN, "persist": False, "preview_rows": 1})
        if dry.get("run_id") is not None:
            return mismatch(run_id, "persist", dry.get("run_id"), None)
    after = len(history_items())
    if after != before:
        print(
            f"FAIL field=history_count: persist=false x2 后条数由 {before} 变为 {after}（不得增加）",
            file=sys.stderr,
        )
        return 1

    print(
        f"PASS run_id={run_id} 钉选月供={echoed_pay} "
        f"input={KNOWN} persist=false x2 条数不变({after})"
    )
    print(f"复检该条: API_BASE={API_BASE} python3 scripts/roundtrip_gate.py --run-id {run_id}")
    return 0


def cmd_verify_run(run_id: int) -> int:
    run = find_run(run_id)
    if run is None:
        print(f"ERROR: run_id={run_id} 不在历史列表中", file=sys.stderr)
        return 2
    stored_input, stored_result = parse_row(run)
    for field in ("principal", "annual_rate", "months"):
        if field not in stored_input:
            return mismatch(run_id, f"input.{field}", "<missing>", "<required>")
    # 按落库 input 以 persist=false 重算（不产生新记录），与钉选月供比对
    fresh = http_json(
        "POST",
        "/api/schedule",
        {
            "principal": stored_input["principal"],
            "annual_rate": stored_input["annual_rate"],
            "months": stored_input["months"],
            "persist": False,
            "preview_rows": 1,
        },
    )
    got = stored_result.get("monthly_payment", "<missing>")
    want = fresh.get("monthly_payment")
    if not num_eq(got, want):
        return mismatch(run_id, "result.monthly_payment", got, want)
    print(f"PASS run_id={run_id} 钉选月供={got} 与按落库 input 重算值一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="落库往返一致性门禁（依赖已启动的 API）")
    ap.add_argument("--run-id", type=int, default=None, help="复检已有 run（不写入新记录）")
    args = ap.parse_args()
    if args.run_id is not None:
        return cmd_verify_run(args.run_id)
    return cmd_write_and_verify()


if __name__ == "__main__":
    sys.exit(main())
