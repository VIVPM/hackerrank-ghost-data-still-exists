"""Checker: score predictions against sample answers, validate every row, and total tokens/cost.

    python code/evaluation/main.py runs/<tag>                # one run
    python code/evaluation/main.py runs/<tagA> runs/<tagB>   # side-by-side comparison
    python code/evaluation/main.py output.csv                # format check of the final file
"""
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from tools import OP_STATE  # noqa: E402

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
# USD per million tokens (input, output), from the Hugging Face router model list on 2026-10-26.
# shortcut: the router may pick a cheaper provider; these are the highest listed prices, so costs are upper bounds
PRICES = {"deepseek-ai/DeepSeek-V4.1-Flash": (0.30, 1.20), "zai-org/GLM-5.3": (1.40, 4.40),
          "zai-org/GLM-5.3-Flash": (0.15, 0.50)}


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def validate(rows, expected_ids=None):
    """Format problems in a predictions file (the rules the grader parses)."""
    problems = []
    ids = [r["case_id"] for r in rows]
    if ids != sorted(ids, key=lambda c: int(c.split("_")[1])):
        problems.append("rows not sorted by case_id")
    if len(set(ids)) != len(ids):
        problems.append("duplicate case_id")
    if expected_ids is not None and set(ids) != set(expected_ids):
        problems.append(f"case_id set differs: missing {len(set(expected_ids) - set(ids))}, extra {len(set(ids) - set(expected_ids))}")
    for r in rows:
        cid, status = r["case_id"], r["deletion_status"]
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError:
            problems.append(f"{cid}: steps_json is not JSON")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            problems.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            problems.append(f"{cid}: NOT_APPLICABLE with steps")
        if [s.get("step_no") for s in steps] != list(range(1, len(steps) + 1)):
            problems.append(f"{cid}: step numbers not 1..n")
        phases = [s.get("phase") for s in steps]
        if phases != sorted(phases, key=["FINDING", "REMEDIATION", "VERIFICATION"].index):
            problems.append(f"{cid}: phases out of order")
        for s in steps:
            if set(s) != {"step_no", *FIELDS}:
                problems.append(f"{cid}: step {s.get('step_no')} has wrong fields")
            elif OP_STATE.get(s["operation"]) != (s["phase"], s["state"]):
                problems.append(f"{cid}: step {s['step_no']} bad operation/phase/state")
            elif status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
                problems.append(f"{cid}: COMPLETE step {s['step_no']} must be evidence-backed verification")
            elif s["phase"] == "REMEDIATION" and s["evidence_id"]:
                problems.append(f"{cid}: remediation step {s['step_no']} cites evidence")
    return problems


def score(pred, gold):
    """Case, step, field and sequence accuracy on cases present in both."""
    common = sorted(set(pred) & set(gold), key=lambda c: int(c.split("_")[1]))
    key = lambda s: tuple(s[f] for f in FIELDS)  # noqa: E731
    st_ok = tp = n_pred = n_gold = f_ok = f_tot = seq_ok = 0
    per_field = {f: 0 for f in FIELDS}
    confusion = Counter()
    per_case = []
    for c in common:
        p, g = pred[c], gold[c]
        ps, gs = json.loads(p["steps_json"]), json.loads(g["steps_json"])
        st_ok += p["deletion_status"] == g["deletion_status"]
        confusion[(g["deletion_status"], p["deletion_status"])] += 1
        hit = len({key(s) for s in ps} & {key(s) for s in gs})
        tp += hit
        n_pred += len(ps)
        n_gold += len(gs)
        for i, s in enumerate(gs):  # strict field accuracy: compare position by position
            for f in FIELDS:
                f_tot += 1
                f_ok += i < len(ps) and ps[i][f] == s[f]
        # aligned field accuracy: pair each gold step with our step for the same record, so one
        # missing step costs only itself instead of shifting every later position
        free = list(ps)
        for s in gs:
            match = next((q for q in free if q["resource_id"] == s["resource_id"] and q["phase"] == s["phase"]), None) \
                or next((q for q in free if q["resource_id"] == s["resource_id"]), None)
            if match:
                free.remove(match)
                for f in FIELDS:
                    per_field[f] += match[f] == s[f]
        seq = [key(s) for s in ps] == [key(s) for s in gs]
        seq_ok += seq
        per_case.append((c, g["deletion_status"], p["deletion_status"], hit, len(gs), len(ps), seq))
    n = len(common) or 1
    prec, rec = tp / (n_pred or 1), tp / (n_gold or 1)
    field_by_name = {f: v / (n_gold or 1) for f, v in per_field.items()}
    return {"cases": len(common), "status_acc": st_ok / n, "step_precision": prec, "step_recall": rec,
            "step_f1": 2 * prec * rec / ((prec + rec) or 1), "field_acc_strict": f_ok / (f_tot or 1),
            "field_acc": sum(field_by_name.values()) / len(FIELDS), "field_by_name": field_by_name,
            "sequence_acc": seq_ok / n, "confusion": confusion, "per_case": per_case}


def usage(run_dir):
    files = list((run_dir / "cases").glob("*.json"))
    rs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if not rs:
        return None
    model = rs[0].get("model", "")
    tin = sum(u["in"] for r in rs for u in r["usage"])
    tout = sum(u["out"] for r in rs for u in r["usage"])
    pin, pout = PRICES.get(model, (0, 0))
    cost = tin / 1e6 * pin + tout / 1e6 * pout
    n = len(rs)
    return {"model": model, "thinking": rs[0].get("thinking"), "cases": n,
            "model_calls": sum(len(r["usage"]) for r in rs), "in": tin, "out": tout,
            "avg_tokens": (tin + tout) / n, "cost": cost, "cost_per_case": cost / n,
            "avg_seconds": sum(r.get("seconds", 0) for r in rs) / n,
            "avg_tool_calls": sum(r["tool_calls"] for r in rs) / n,
            "hit_tool_limit": sum(r["tool_calls"] >= int(os.environ.get("AGENT_MAX_TOOL_CALLS", "25")) for r in rs),
            "forced_submit": sum(bool(r.get("forced_submit")) for r in rs),
            "retried": sum(r.get("attempts", 1) > 1 for r in rs),
            "failed": [r["case_id"] for r in rs if r["error"]]}


def report(path):
    path = Path(path)
    pred_file = path if path.suffix == ".csv" else path / "predictions.csv"
    pred_rows = read(pred_file)
    pred = {r["case_id"]: r for r in pred_rows}
    gold = {r["case_id"]: r for r in read(ROOT / "dataset" / "sample_output.csv")}
    real_ids = [r["case_id"] for r in read(ROOT / "dataset" / "deletion_requests.csv")]
    print(f"== {path}")
    probs = validate(pred_rows, real_ids if pred_file.name == "output.csv" else None)
    print(f"format problems: {len(probs)}", *probs[:10], sep="\n  ")
    s = score(pred, gold)
    if s["cases"]:
        print(f"samples scored: {s['cases']}  status acc {s['status_acc']:.1%}  step P/R/F1 "
              f"{s['step_precision']:.1%}/{s['step_recall']:.1%}/{s['step_f1']:.1%}  "
              f"field acc {s['field_acc']:.1%} (strict by position {s['field_acc_strict']:.1%})  "
              f"sequence acc {s['sequence_acc']:.1%}")
        print("  per field: " + "  ".join(f"{f} {v:.1%}" for f, v in s["field_by_name"].items()))
        print("  status (gold -> ours): " + "  ".join(f"{g}->{p}: {v}" for (g, p), v in sorted(s["confusion"].items())))
        for c, gst, pst, hit, ng, np_, seq in s["per_case"]:
            if gst != pst or not seq:
                print(f"  {c}: gold {gst} / ours {pst}  steps matched {hit}/{ng} (ours {np_})")
    na = [c for c, r in pred.items() if c not in gold]
    if na:
        print(f"non-sample cases: {len(na)}  marked NOT_APPLICABLE: "
              f"{sum(pred[c]['deletion_status'] == 'NOT_APPLICABLE' for c in na)}")
    u = usage(path) if path.is_dir() else None
    if u:
        print(f"usage: {u['model']} thinking={u['thinking']} cases={u['cases']} calls={u['model_calls']} "
              f"in={u['in']:,} out={u['out']:,} total={u['in'] + u['out']:,} avg/case={u['avg_tokens']:,.0f} tokens "
              f"cost=${u['cost']:.2f} (${u['cost_per_case']:.4f}/case) avg {u['avg_seconds']:.0f}s/case "
              f"avg tools {u['avg_tool_calls']:.1f} hit-limit {u['hit_tool_limit']} "
              f"forced-submit {u['forced_submit']} retried {u['retried']} failed {u['failed']}")
    return s, u


def usage_table(usages):
    """Per-model rows plus an overall row across every run given."""
    by_model = {}
    for u in usages:
        m = by_model.setdefault(u["model"], Counter())
        for k in ("cases", "model_calls", "in", "out", "cost"):
            m[k] += u[k]
    print("\n| model | cases | model calls | input tokens | output tokens | total tokens | avg tokens/case | cost | cost/case |")
    print("|---|---|---|---|---|---|---|---|---|")
    rows = list(by_model.items()) + ([("OVERALL", sum(by_model.values(), Counter()))] if len(by_model) > 1 else [])
    for name, m in rows:
        tot = m["in"] + m["out"]
        print(f"| {name} | {m['cases']} | {m['model_calls']} | {m['in']:,} | {m['out']:,} | {tot:,} | "
              f"{tot / m['cases']:,.0f} | ${m['cost']:.2f} | ${m['cost'] / m['cases']:.4f} |")


if __name__ == "__main__":
    usages = [u for p in (sys.argv[1:] or [str(ROOT / "output.csv")]) for _, u in [report(p)] if u]
    if usages:
        usage_table(usages)
