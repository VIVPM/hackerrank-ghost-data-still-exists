"""Decide each record's data category and what deletion_rules.csv requires for it."""
import csv

from evidence import DATASET

# default category per system when the evidence gives no record_type
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "integration_service": "DATA_FLOW",  # a process, not a record: it gets disabled
}

# required action -> (finding, remediation, verification) operations
ACTION_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    # backups are retained, but their restore path must be blocked
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
}


def load_rules():
    with open(DATASET / "deletion_rules.csv", encoding="utf-8") as f:
        return {r["data_category"]: r["required_action"] for r in csv.DictReader(f)}


def category_of(system, rows):
    """rows: every evidence row we have for one resource."""
    for r in rows:
        if r["data"].get("record_type") and r["data"]["record_type"] != "DATA_FLOW":
            return r["data"]["record_type"]
    blob = " ".join(f"{k}={v}" for r in rows for k, v in r["data"].items()).lower()
    if system == "support_platform":
        if any(r["kind"] == "pdf" for r in rows) or "attachment" in blob:
            return "SUPPORT_ATTACHMENT"
        if "legacy_contact_index" in blob:
            return "SECONDARY_IDENTIFIER"  # a stray copy of contact details, not a conversation
    if system == "analytics_platform" and "email_opt_in" in blob:
        return "SECONDARY_IDENTIFIER"  # a contact/opt-in entry, not a behavioural profile
    return SYSTEM_CATEGORY.get(system, "SECONDARY_IDENTIFIER")


def required_ops(category, rules):
    """(finding, remediation, verification) operations for a category."""
    if category == "DATA_FLOW":
        return ACTION_OPS["DISABLE"]
    if category == "BACKUP_ARCHIVE":
        return ACTION_OPS["BLOCK"]
    return ACTION_OPS[rules[category]]


if __name__ == "__main__":
    import json
    from collections import Counter, defaultdict

    from evidence import load_text_evidence

    rules = load_rules()
    by_rid = defaultdict(list)
    for r in load_text_evidence():
        if r["resource_id"]:
            by_rid[r["resource_id"]].append(r)
    # every remediation/verification op in the sample answers should be what our rule gives
    ok, bad = 0, Counter()
    with open(DATASET / "sample_output.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for s in json.loads(row["steps_json"]):
                if s["phase"] == "FINDING":
                    continue
                ops = required_ops(category_of(s["system_id"], by_rid.get(s["resource_id"], [])), rules)
                if s["operation"] in ops:
                    ok += 1
                else:
                    bad[(s["system_id"], s["resource_id"], s["operation"], ops[1])] += 1
    print("sample fix/check steps matching our rule:", ok, "/", ok + sum(bad.values()))
    for k, v in bad.items():
        print("  mismatch:", k, v)
