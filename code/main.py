"""Run the deletion-audit agent.

    python code/main.py                       # all 518 requests -> output.csv
    python code/main.py --cases eval          # 32 samples + 10 non-deletion requests (model comparison)
    python code/main.py --cases case_30,case_533 --tag try1

Settings (environment or .env): HF_TOKEN, AGENT_MODEL, AGENT_THINKING, AGENT_WORKERS,
AGENT_MAX_TOOL_CALLS, AGENT_MAX_RETRIES, AGENT_MAX_ROUNDS.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "3"))  # automatic retry rounds for failed cases
sys.path.insert(0, str(Path(__file__).parent))


def load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.*)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")


def read_csv(name):
    with open(ROOT / "dataset" / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_predictions(path, results):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in sorted(results, key=lambda r: int(r["case_id"].split("_")[1])):
            w.writerow([r["case_id"], r["status"], json.dumps(r["steps"], separators=(",", ":"))])


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="real", help="real | sample | eval | comma-separated case ids")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "zai-org/GLM-5.3"))
    ap.add_argument("--thinking", default=os.environ.get("AGENT_THINKING", "low"), help="low | high | none")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("AGENT_WORKERS", "4")))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    if not os.environ.get("HF_TOKEN"):
        sys.exit("HF_TOKEN is not set (environment or .env).")

    samples, real = read_csv("sample_deletion_requests.csv"), read_csv("deletion_requests.csv")
    by_id = {r["case_id"]: r for r in samples + real}
    if args.cases == "real":
        todo = real
    elif args.cases == "sample":
        todo = samples
    elif args.cases == "eval":
        # every sample, plus the real requests whose wording is not one of the standard deletion phrasings
        openers = {}
        for r in real:
            openers.setdefault(re.split(r"(?<=[.!?。])\s*", r["request_text"])[0], []).append(r)
        todo = samples + [r for group in openers.values() if len(group) == 1 for r in group]
    else:
        todo = [by_id[c.strip()] for c in args.cases.split(",")]

    tag = args.tag or f"{args.model.split('/')[-1]}_{args.thinking}"
    run_dir = ROOT / "runs" / tag / "cases"
    run_dir.mkdir(parents=True, exist_ok=True)
    thinking = None if args.thinking == "none" else args.thinking

    t0 = time.time()
    from agent import run_case
    from tools import Data
    data = Data(samples + real)  # pairing accounts needs every request competing together
    print(f"data loaded in {time.time() - t0:.0f}s; {len(todo)} cases; model {args.model}; thinking {args.thinking}")

    results = {}
    pending = []
    for c in todo:
        f = run_dir / f"{c['case_id']}.json"
        if f.exists() and not json.loads(f.read_text(encoding="utf-8")).get("error"):
            results[c["case_id"]] = json.loads(f.read_text(encoding="utf-8"))  # resume
        else:
            pending.append(c)

    def work(c):
        f = run_dir / f"{c['case_id']}.json"
        # tokens spent on earlier failed attempts were still paid for: carry them into the usage totals
        prev = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        start = time.time()
        try:
            r = run_case(data, c, args.model, thinking)
        except Exception as e:  # one case failing must not stop the run; it is retried, never guessed
            r = {"case_id": c["case_id"], "status": "", "steps": [], "usage": getattr(e, "usage", []),
                 "tool_calls": 0, "error": str(e)}
        r["usage"] = prev.get("usage", []) + r["usage"]
        r["attempts"] = prev.get("attempts", 0) + 1
        r.update(model=args.model, thinking=args.thinking,
                 seconds=round(prev.get("seconds", 0) + time.time() - start, 1))
        f.write_text(json.dumps(r, indent=1), encoding="utf-8")
        return r

    for round_no in range(1, MAX_ROUNDS + 1):
        if not pending:
            break
        print(f"round {round_no}: {len(pending)} cases")
        with ThreadPoolExecutor(args.workers) as pool:
            for n, fut in enumerate(as_completed([pool.submit(work, c) for c in pending]), 1):
                r = fut.result()
                results[r["case_id"]] = r
                print(f"[{n}/{len(pending)}] {r['case_id']} {r['status'] or 'FAILED'} "
                      f"tools={r['tool_calls']} {r['seconds']}s {r['error'][:80]}")
        pending = [by_id[cid] for cid, r in results.items() if r["error"]]
        if pending and round_no < MAX_ROUNDS:
            time.sleep(30)  # give a rate limit or provider outage time to clear

    done = [r for r in results.values() if not r["error"]]
    failed = [r["case_id"] for r in results.values() if r["error"]]
    write_predictions(ROOT / "runs" / tag / "predictions.csv", done)
    print(f"done {len(done)}, failed {len(failed)} {failed[:10]}; total {time.time() - t0:.0f}s")
    if args.cases == "real":
        if failed:
            print("output.csv NOT written: re-run the same command to retry the failed cases.")
        else:
            write_predictions(ROOT / "output.csv", done)
            print("wrote output.csv")


if __name__ == "__main__":
    main()
