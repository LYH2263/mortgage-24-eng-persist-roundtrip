#!/usr/bin/env python3
"""落库往返一致性门禁（round-trip persistence gate）。

依赖：一个已经启动、可访问的 Mortgage API（默认 http://localhost:9400，
可用环境变量 MORTGAGE_API_BASE 覆盖）。本脚本只走 HTTP，不直接碰数据库，
也**不会**修改引擎公式；它只比对「写入回包」与「落库后取回」两者是否一致。

门禁内容
--------
1. 向 POST /api/schedule 写入一笔*已知*本金/利率/期数（persist=true），
   拿到 run_id 与回包月供 monthly_payment，随后立刻按编号（id==run_id）
   从 GET /api/history 取回该条：
     - 钉选月供 == 写入回包月供（result.monthly_payment）
     - input.principal / input.annual_rate / input.months 与写入值一致
   首次运行会把该笔 run_id 与回包月供「钉」到 pin 文件，之后默认钉选同一
   笔复检——因此人为篡改库里该条 result 月供后，再次运行必然失败。
2. 再发两次 persist=false 的请求，calc_runs 条数不得增加（且 run_id 为 null）。
3. 「恢复该条」后再跑通过；或加 --rewrite「改用新写入」重新钉选后通过。

退出码：全部断言通过为 0；任一断言失败为 1（信息中点名出错字段）；
环境/连通问题为 2。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 一笔固定的、已知的输入（本金/利率/期数）。月供不在这里硬编码——
# 期望值取自当次写入回包并钉选，避免与引擎公式耦合。
KNOWN_PRINCIPAL = 800000.0
KNOWN_ANNUAL_RATE = 4.2
KNOWN_MONTHS = 240

API_BASE = os.environ.get("MORTGAGE_API_BASE", "http://localhost:9400").rstrip("/")
PIN_PATH = Path(__file__).with_name(".roundtrip_gate_pin.json")
# history 只支持 limit 拉取，给一个足够大的上限以覆盖全表，用于按编号定位与计数。
BIG_LIMIT = 1_000_000
EPS = 1e-9


class GateFailure(AssertionError):
    """断言失败：信息需点名出错字段。"""


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx：把响应体带出来便于定位
        body = e.read().decode("utf-8", "replace")
        raise GateFailure(f"HTTP {method} {path} -> {e.code}: {body}") from None
    except (urllib.error.URLError, OSError) as e:
        raise GateFailure(f"无法访问 API {url}：{e}") from None


def health() -> None:
    try:
        r = _request("GET", "/api/health")
    except GateFailure:
        raise
    if not r.get("ok"):
        raise GateFailure(f"/api/health 返回异常：{r}")


def fetch_history() -> list[dict]:
    return _request("GET", f"/api/history?limit={BIG_LIMIT}")["items"]


def find_run(items: list[dict], run_id: int) -> dict | None:
    for it in items:
        if it.get("id") == run_id:
            return it
    return None


def load_pin() -> dict | None:
    if PIN_PATH.exists():
        try:
            return json.loads(PIN_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


def save_pin(pin: dict) -> None:
    PIN_PATH.write_text(json.dumps(pin, ensure_ascii=False, indent=2), encoding="utf-8")


def numbers_close(a, b) -> bool:
    return abs(float(a) - float(b)) <= EPS


def check_stored(run: dict, expected_monthly: float,
                 principal: float, annual_rate: float, months: int) -> None:
    """对取回的一条记录做字段级断言，失败时点名字段。"""
    try:
        result = json.loads(run["result_json"])
    except (ValueError, TypeError, KeyError):
        raise GateFailure(f"run_id={run.get('id')} 的 result_json 无法解析") from None
    try:
        inp = json.loads(run["input_json"])
    except (ValueError, TypeError, KeyError):
        raise GateFailure(f"run_id={run.get('id')} 的 input_json 无法解析") from None

    stored_monthly = result.get("monthly_payment")
    if not isinstance(stored_monthly, (int, float)) or not numbers_close(stored_monthly, expected_monthly):
        raise GateFailure(
            f"result.monthly_payment 不一致：落库存的是 {stored_monthly!r}，"
            f"钉选/写入回包应为 {expected_monthly!r}（run_id={run.get('id')}）"
        )

    if not numbers_close(inp.get("principal"), principal):
        raise GateFailure(
            f"input.principal 不一致：落库存的是 {inp.get('principal')!r}，应为 {principal!r}"
            f"（run_id={run.get('id')}）"
        )
    if not numbers_close(inp.get("annual_rate"), annual_rate):
        raise GateFailure(
            f"input.annual_rate 不一致：落库存的是 {inp.get('annual_rate')!r}，应为 {annual_rate!r}"
            f"（run_id={run.get('id')}）"
        )
    if inp.get("months") != months:
        raise GateFailure(
            f"input.months 不一致：落库存的是 {inp.get('months')!r}，应为 {months!r}"
            f"（run_id={run.get('id')}）"
        )


def run_gate(force_rewrite: bool) -> None:
    health()

    pin = None if force_rewrite else load_pin()
    pinned_run = None
    if pin is not None:
        pinned_run = find_run(fetch_history(), pin["run_id"])
        if pinned_run is None:
            # 钉选记录已不在历史（如库被重建）→ 退化为新写入。
            pin = None

    if pin is None:
        # —— 写入往返：persist=true，拿 run_id，立刻按编号取回 ——
        resp = _request("POST", "/api/schedule", {
            "principal": KNOWN_PRINCIPAL,
            "annual_rate": KNOWN_ANNUAL_RATE,
            "months": KNOWN_MONTHS,
            "persist": True,
        })
        run_id = resp.get("run_id")
        resp_monthly = resp.get("monthly_payment")
        if not isinstance(run_id, int):
            raise GateFailure(f"persist=true 未返回整数 run_id：{run_id!r}")
        if not isinstance(resp_monthly, (int, float)):
            raise GateFailure(f"写入回包缺少数值型 monthly_payment：{resp_monthly!r}")

        run = find_run(fetch_history(), run_id)
        if run is None:
            raise GateFailure(f"写入返回 run_id={run_id}，但 history 中按编号取回不到该条")
        check_stored(run, float(resp_monthly),
                     KNOWN_PRINCIPAL, KNOWN_ANNUAL_RATE, KNOWN_MONTHS)
        save_pin({
            "run_id": run_id,
            "monthly_payment": float(resp_monthly),
            "principal": KNOWN_PRINCIPAL,
            "annual_rate": KNOWN_ANNUAL_RATE,
            "months": KNOWN_MONTHS,
        })
        mode = f"新写入并钉选 run_id={run_id}"
        baseline_items = fetch_history()
    else:
        # —— 钉选复检：锁定上一次写入的同一笔，抓对该条的人为篡改 ——
        expected = float(pin["monthly_payment"])
        check_stored(pinned_run, expected,
                     float(pin["principal"]), float(pin["annual_rate"]), int(pin["months"]))
        run_id = pin["run_id"]
        mode = f"钉选复检 run_id={run_id}"
        baseline_items = fetch_history()

    baseline_count = len(baseline_items)

    # —— 两次 persist=false：不得新增任何行，且回包 run_id 必须为 null ——
    for i in (1, 2):
        r = _request("POST", "/api/schedule", {
            "principal": KNOWN_PRINCIPAL + i * 1000.0,  # 换不同入参，确保即便误落库也可辨
            "annual_rate": KNOWN_ANNUAL_RATE,
            "months": KNOWN_MONTHS,
            "persist": False,
        })
        if r.get("run_id") is not None:
            raise GateFailure(
                f"persist=false 第 {i} 次竟返回了 run_id={r.get('run_id')!r}（不应落库）"
            )

    after_count = len(fetch_history())
    if after_count != baseline_count:
        raise GateFailure(
            f"persist=false 后条数增加：{baseline_count} -> {after_count}"
            f"（calc_runs 不应因 persist=false 新增行）"
        )

    monthly = pin["monthly_payment"] if pin is not None else resp_monthly
    print(
        f"PASS [{mode}] monthly_payment={monthly} "
        f"input=(principal={KNOWN_PRINCIPAL}, annual_rate={KNOWN_ANNUAL_RATE}, "
        f"months={KNOWN_MONTHS}) persist=false×2 条数保持 {after_count}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="落库往返一致性门禁")
    ap.add_argument("--rewrite", action="store_true",
                    help="强制新写一笔并重新钉选（恢复手段之一：改用新写入）")
    args = ap.parse_args()
    try:
        run_gate(force_rewrite=args.rewrite)
    except GateFailure as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    except Exception as e:  # 连通/环境类问题与断言失败区分开
        print(f"ERROR 门禁无法运行：{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
