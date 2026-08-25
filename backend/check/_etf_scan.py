"""临时脚本：扫描 ETF 前复权断层（>30% 隔夜跳变）。抽样运行。"""
import json
import random

from app.data_sources.market_data import fetch_a_share_daily

THRESH = 30.0
OUT = "_etf_scan_result.jsonl"
SAMPLE = 400
SEED = 42


def main() -> None:
    symbols = json.load(open("_etf_scan_list.json", encoding="utf-8"))
    random.Random(SEED).shuffle(symbols)
    sample = symbols[:SAMPLE]
    n = len(sample)
    bad = 0
    errors = 0
    with open(OUT, "w", encoding="utf-8") as f:
        for i, s in enumerate(sample, 1):
            try:
                df = fetch_a_share_daily(s, start="2020-01-01", end="2026-12-31")
                ret = df["close"].pct_change() * 100
                jumps = [(d.strftime("%Y-%m-%d"), round(float(r), 2)) for d, r in ret[ret.abs() > THRESH].items()]
                if jumps:
                    bad += len(jumps)
                    f.write(json.dumps({"symbol": s, "jumps": jumps}, ensure_ascii=False) + "\n")
                    f.flush()
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 30:
                    f.write(json.dumps({"symbol": s, "error": str(e)[:60]}, ensure_ascii=False) + "\n")
                f.flush()
            if i % 50 == 0:
                print(f"[{i}/{n}] done, jumps so far: {bad}, errors: {errors}", flush=True)
    print(f"DONE {n} ETFs, jumps>30%: {bad}, errors: {errors}")


if __name__ == "__main__":
    main()

