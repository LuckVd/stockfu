#!/usr/bin/env bash
# 深历史三复权回补监管 — setsid 全脱离会话 + 超时硬上限 + 幂等重跑循环。
#
# 为什么用 --full:backfill_adj_prices._complete_codes 的完成判定只看
# "raw/hfq 行数 >= qfq 行数",不校验是否覆盖到请求的 start。库内已有
# 2020+ 三复权 → resume 会把 ~800 只误判"已完成"跳过,2013-2019 一行不补。
# --full 关 resume 强制全量重抓;幂等(preserve_qfq + MERGE_ADJ),重跑安全。
#
# 不卡死三板斧:direct 模式(无代理池 spin)+ fetch_timeout=60s 线程看门狗
# + 每票单独 commit(进度即时落盘)+ 每趟 timeout 硬上限,挂了就重跑续上。
set -u
cd /opt/pro/stockfu || { echo "no repo"; exit 1; }

START="2013-01-01"
END="2026-07-24"
LOG="data/backfill_deep_2013.log"
RUN_CAP=10800          # 每趟 3h 硬上限(direct 正常 ~15-30min,留 6x 余量)
MAX_ITERS=50
MODES=(direct clash free)
mi=0
best_fail=999999
stale_passes=0
ok=0; fail=999

echo "##### SUPERVISOR START $(date '+%F %T') start=$START end=$END pid=$$ #####" >> "$LOG"

iter=0
while [ $iter -lt $MAX_ITERS ]; do
  iter=$((iter+1))
  mode="${MODES[$mi]}"
  echo "===== ITER $iter mode=$mode $(date '+%F %T') =====" >> "$LOG"

  timeout "${RUN_CAP}" python3 -u main.py --backfill-adj-prices \
      --start "$START" --end "$END" --full --proxy-mode "$mode" >> "$LOG" 2>&1
  rc=$?

  # 解析末尾 "=== 完成 ok=N fail=M rows=K ... ==="
  last=$(grep -E '=== 完成 ok=' "$LOG" | tail -1)
  ok=$(echo "$last"  | grep -oE 'ok=[0-9]+'   | head -1 | grep -oE '[0-9]+'); ok=${ok:-0}
  fail=$(echo "$last" | grep -oE 'fail=[0-9]+' | head -1 | grep -oE '[0-9]+'); fail=${fail:-999}
  echo "===== ITER $iter exit=$rc ok=$ok fail=$fail $(date '+%F %T') =====" >> "$LOG"

  # 成功收口:全部 ok 且无失败
  if [ "$fail" -eq 0 ] && [ "$ok" -gt 0 ]; then
    echo "##### ALL COMPLETE iter=$iter ok=$ok $(date '+%F %T') #####" >> "$LOG"
    break
  fi

  if [ "$ok" -eq 0 ]; then
    # 本趟零进展(baostock 不可达 / 直连被封)→ 升级代理模式
    mi=$((mi+1))
    if [ "$mi" -ge "${#MODES[@]}" ]; then mi=$(( ${#MODES[@]} - 1 )); fi
    echo "  [no progress] escalate mode -> ${MODES[$mi]}" >> "$LOG"
  else
    # 有进展但有失败:若 fail 不再下降,计为顽固失败
    if [ "$fail" -lt "$best_fail" ]; then
      best_fail=$fail; stale_passes=0
    else
      stale_passes=$((stale_passes+1))
      echo "  [stale] fail=$fail not improving ($stale_passes/3)" >> "$LOG"
      if [ "$stale_passes" -ge 3 ]; then
        echo "##### ACCEPT remaining failures: best_fail=$best_fail after $iter iters $(date '+%F %T') #####" >> "$LOG"
        break
      fi
    fi
  fi

  sleep 5
done

echo "##### SUPERVISOR EXIT iter=$iter ok=$ok fail=$fail $(date '+%F %T') #####" >> "$LOG"
