#!/usr/bin/env bash
# 落库往返门禁的端到端自校验：
#   1) 门禁通过（钉选一笔）
#   2) 人为篡改该条 result.monthly_payment
#   3) 门禁必须失败（退出码非 0），且错误中点名字段 monthly_payment
#   4) 恢复该条 -> 门禁通过
#   5) 再次篡改 -> 门禁失败；改用新写入 (--rewrite) -> 门禁通过
#
# 连库：
#   本地 uvicorn：export MORTGAGE_DB=/path/to/app.db
#   docker 部署：不设 MORTGAGE_DB，自动走 `docker compose exec`
# 门禁目标 API：MORTGAGE_API_BASE（默认 http://localhost:9400）
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
TAMPER_VALUE="1234.56"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

dbop() {
  if [ -n "${MORTGAGE_DB:-}" ]; then
    MORTGAGE_DB="$MORTGAGE_DB" "$PY" "$HERE/gatedb.py" "$@"
  else
    docker compose exec -T backend python - "$@" < "$HERE/gatedb.py"
  fi
}

run_gate() { "$PY" "$HERE/roundtrip_gate.py" "$@"; }

pin_run_id() {
  "$PY" - "$HERE" <<'PYEOF'
import json, pathlib, sys
here = pathlib.Path(sys.argv[1])
pin = json.loads((here / ".roundtrip_gate_pin.json").read_text(encoding="utf-8"))
print(pin["run_id"])
PYEOF
}

FAIL=0
echo "== [1/6] 首次运行门禁（应通过，钉选一笔） =="
out="$(run_gate)"; rc=$?; echo "$out"
[ $rc -eq 0 ] || { red "  ✗ 期望退出码 0，实际 $rc"; FAIL=1; }

RID="$(pin_run_id)"
echo "   钉选 run_id=$RID；库内当前 monthly_payment=$(dbop get "$RID" monthly_payment)"
COUNT_BEFORE="$(dbop count)"
echo "   calc_runs 条数=$COUNT_BEFORE（persist=false×2 已在门禁内断言不增）"

echo "== [2/6] 人为篡改 run_id=$RID 的 result.monthly_payment -> $TAMPER_VALUE =="
dbop tamper "$RID" "$TAMPER_VALUE"
echo "   篡改后库内值=$(dbop get "$RID" monthly_payment)"

echo "== [3/6] 再跑门禁（必须失败，且点名字段 monthly_payment） =="
out="$(run_gate 2>&1)"; rc=$?; echo "$out"
if [ $rc -eq 0 ]; then red "  ✗ 篡改后门禁竟然通过了"; FAIL=1; fi
if ! printf '%s' "$out" | grep -q "monthly_payment"; then
  red "  ✗ 失败信息未点名字段 monthly_payment"; FAIL=1
fi

echo "== [4/6] 恢复该条后再跑（应通过） =="
dbop restore "$RID"
out="$(run_gate)"; rc=$?; echo "$out"
[ $rc -eq 0 ] || { red "  ✗ 恢复后期望退出码 0，实际 $rc"; FAIL=1; }

echo "== [5/6] 再次篡改 -> 失败；改用 --rewrite 新写入（应通过） =="
dbop tamper "$RID" "$TAMPER_VALUE"
out="$(run_gate 2>&1)"; rc=$?; echo "$out"
[ $rc -ne 0 ] || { red "  ✗ 第二次篡改后门禁竟然通过"; FAIL=1; }
out="$(run_gate --rewrite)"; rc=$?; echo "$out"
[ $rc -eq 0 ] || { red "  ✗ --rewrite 新写入后期望退出码 0，实际 $rc"; FAIL=1; }
NEW_RID="$(pin_run_id)"
echo "   重新钉选 run_id=$NEW_RID"

echo "== [6/6] 收尾：还原旧条 $RID，保持库干净 =="
dbop restore "$RID"

echo
COUNT_AFTER="$(dbop count)"
echo "calc_runs 条数：开始 $COUNT_BEFORE -> 结束 $COUNT_AFTER（persist=false 全程不增行）"
if [ "$FAIL" -eq 0 ]; then
  green "SELFTEST PASS：篡改被点名字段拦截，恢复/新写入后均通过。"
  exit 0
fi
red "SELFTEST FAIL"
exit 1
