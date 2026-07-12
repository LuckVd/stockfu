"""把旧的明文 .json 回测产物迁移为 .json.gz + .meta.json。

对每个 data/backtest/*.json(跳过 .meta.json):
  1. 读旧 json → 补 schema_version
  2. 写 {run_id}.json.gz(gzip,保留全部信息:holdings_curve + pending 意图一个不丢)
  3. 写 {run_id}.meta.json(摘要旁路,供 list_runs 秒级读取,不再全量解析大文件)
  4. 删旧 {run_id}.json
幂等:已存在 {run_id}.json.gz 的跳过。可重复运行。
"""
import gzip
import json
import os

from stockfu.backtest import scheduler


def main() -> None:
    d = scheduler._data_dir()
    moved = skipped = 0
    total_before = total_after = 0
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json") or fn.endswith(".meta.json"):
            continue
        rid = fn[:-len(".json")]
        gz = os.path.join(d, f"{rid}.json.gz")
        if os.path.exists(gz):
            print(f"  skip   {rid:<32} (已有 .json.gz)")
            skipped += 1
            continue
        src = os.path.join(d, fn)
        before = os.path.getsize(src)
        with open(src, encoding="utf-8") as f:
            result = json.load(f)
        result["schema_version"] = result.get("schema_version", 1)
        # 原子写
        tmp = f"{gz}.tmp{os.getpid()}"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str)
        os.replace(tmp, gz)
        scheduler._write_meta(rid, result, gz)
        os.remove(src)
        after = os.path.getsize(gz)
        total_before += before
        total_after += after
        print(f"  moved  {rid:<32} {before/1e6:>6.1f}M -> {after/1e6:>5.1f}M "
              f"gz ({after/before*100:>4.1f}%) + meta")
        moved += 1
    print(f"\n迁移 {moved} 个,跳过 {skipped} 个")
    if total_before:
        print(f"体积: {total_before/1e6:.1f}M -> {total_after/1e6:.1f}M "
              f"(省 {(1-total_after/total_before)*100:.1f}%)")


if __name__ == "__main__":
    main()
