"""Hand-built evaluation cases (no model calls, no cost).

Each case is a small invented scenario that reproduces a failure found during development and
checks that the agent's tools now handle it. Run:  python code/evaluation/custom_cases.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from rules import load_rules  # noqa: E402
from tools import Data, describe, find_records, get_record_state, submit_answer  # noqa: E402


def fake_data(rows, requests, file_time, linked=None, activity=()):
    """A Data object built from a few hand-written rows instead of the dataset."""
    d = Data.__new__(Data)
    d.requests = {r["case_id"]: r for r in requests}
    d.rows, d.activity, d.rules, d.file_time = rows, list(activity), load_rules(), file_time
    d.linked, d.accounts, d.people, d.email_to_user = linked or {}, {}, {}, {}
    d.audit, d.audit_time = [], {}
    d.build_indexes()
    return d


def row(file_id, captured, kind, table, system, rid, time, **data):
    return {"file_id": file_id, "captured_at": captured, "kind": kind, "table": table, "system": system,
            "resource_id": rid, "time": time, "data": {"resource_id": rid, **data} if rid else data}


CASE = {"case_id": "custom", "email": "ana@example.com", "requested_at": "2026-08-01T00:00:00Z",
        "audit_at": "2026-09-10T00:00:00Z"}
results = []


def check(case_name, expectation, ok, detail=""):
    results.append((case_name, expectation, ok, detail))


# 1. Replica drift: deleted from the primary table, still present in the failover replica copy.
rows = [
    row("ev_A", "2026-09-02T00:00:00Z", "sql_table", "customer_profiles_failover_replica", "", "", "2026-09-02T00:00:00Z"),
    row("ev_A", "2026-09-02T00:00:00Z", "sql", "customer_profiles_failover_replica", "account_service",
        "account_1", "2026-09-02T00:00:00Z", email="ana@example.com", full_name="Ana Lima"),
    row("ev_B", "2026-09-05T00:00:00Z", "sql_table", "customer_profiles", "", "", "2026-09-05T00:00:00Z"),
    row("ev_B", "2026-09-05T00:00:00Z", "sql_scope", "snapshot_scope", "account_service", "", "2026-09-05T00:00:00Z",
        origin_system="account_service"),
]
d = fake_data(rows, [CASE], {"ev_A": "2026-09-02T00:00:00Z", "ev_B": "2026-09-05T00:00:00Z"})
out = get_record_state(d, CASE, "account_1")
check("1. Replica copy survives a primary deletion",
      "a newer PRIMARY snapshot must not count as proof that the REPLICA copy is gone",
      "without this record" not in out and "last sighting stands" in out and "cite for this record's state at audit_at: ev_A" in out,
      out.splitlines()[-2])

# 2. Redacted free text is not exposed PII; real contact details in free text are.
redacted = row("ev_C", "2026-09-03T00:00:00Z", "json", "support_conversations", "support_platform", "support_1",
               "2026-09-03T00:00:00Z", message_text="[REDACTED]", subject_reference="user_1")
exposed = row("ev_C", "2026-09-03T00:00:00Z", "json", "support_conversations", "support_platform", "support_2",
              "2026-09-03T00:00:00Z", message_text="Send the update to ana@example.com please")
check("2. '[REDACTED]' text counts as cleaned",
      "message_text='[REDACTED]' -> no direct identifiers; text with an email -> identifiers visible",
      describe(redacted).startswith("no direct identifiers") and "message_text" in describe(exposed),
      f"{describe(redacted)} / {describe(exposed)}")

# 3. A record seen only before the request is not evidence of what survived the deletion.
old = row("ev_D", "2026-02-13T00:00:00Z", "image", "storage", "object_storage", "object_1", "2026-02-13T00:00:00Z",
          text="delivery_confirmation_1.pdf")
new = row("ev_E", "2026-09-04T00:00:00Z", "json", "support_conversations", "support_platform", "support_3",
          "2026-09-04T00:00:00Z", requester_email="ana@example.com")
d = fake_data([old, new], [CASE], {"ev_D": "2026-02-13T00:00:00Z", "ev_E": "2026-09-04T00:00:00Z"},
              linked={"user_1": [old, new]})
out = find_records(d, CASE, "user_1")
check("3. Pre-request sighting is ignored",
      "object_1 (seen Feb, request Aug) left out; support_3 (seen Sep) kept",
      "object_1" not in out and "support_3" in out, out.replace("\n", " ; "))

# 4. submit_answer guards: wrong treatment for the policy, future evidence, and ordering.
sec = row("ev_F", "2026-09-06T00:00:00Z", "json", "security_audit_events", "security_service", "audit_record_1",
          "2026-09-06T00:00:00Z", subject_reference="user_1")
d = fake_data([sec, new], [CASE], {"ev_F": "2026-09-06T00:00:00Z", "ev_E": "2026-09-04T00:00:00Z",
                                   "ev_LATE": "2026-09-20T00:00:00Z"})
errs, _ = submit_answer(d, CASE, "INCOMPLETE", [
    {"resource_id": "audit_record_1", "operation": "BLOCK_RESTORE", "evidence_id": ""}])
check("4a. Retained security record cannot be 'restore-blocked'",
      "BLOCK_RESTORE on a SECURITY_AUDIT_LOG record is rejected (policy is RETAIN)",
      bool(errs) and "does not fit the policy" in errs[0], errs[0] if errs else "accepted")
errs, _ = submit_answer(d, CASE, "INCOMPLETE", [
    {"resource_id": "support_3", "operation": "REPORT_EXPOSED_PII", "evidence_id": "ev_LATE"}])
check("4b. Evidence after audit_at is rejected",
      "a finding citing a file captured after audit_at is rejected",
      bool(errs) and "after audit_at" in errs[0], errs[0] if errs else "accepted")
errs, steps = submit_answer(d, CASE, "INCOMPLETE", [
    {"resource_id": "audit_record_1", "operation": "VERIFY_RECORD_PRESENT", "evidence_id": ""},
    {"resource_id": "support_3", "operation": "REDACT_RECORD", "evidence_id": "ev_E"},
    {"resource_id": "support_3", "operation": "REPORT_EXPOSED_PII", "evidence_id": "ev_E"},
    {"resource_id": "support_3", "operation": "VERIFY_RECORD_REDACTED", "evidence_id": ""}])
order = [s["operation"] for s in steps or []]
check("4c. Steps are ordered and numbered; remediation cites no evidence",
      "FINDING -> REMEDIATION -> VERIFICATION, step_no 1..n, remediation evidence_id ''",
      not errs and order == ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED"]
      and [s["step_no"] for s in steps] == [1, 2, 3, 4] and steps[1]["evidence_id"] == "",
      " > ".join(order))


if __name__ == "__main__":
    for name, expect, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} | {name} | {expect} | {detail}")
    print(f"{sum(r[2] for r in results)}/{len(results)} passed")
    sys.exit(0 if all(r[2] for r in results) else 1)
