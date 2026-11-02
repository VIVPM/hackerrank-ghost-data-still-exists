"""The agent's tools. Each returns short, evidence-cited facts and never shows data after audit_at."""
import csv
import json
import re
from collections import defaultdict

from evidence import DATASET, load_activity, load_manifest, load_text_evidence, system_of
from identity import load_people, norm_email
from match import assign_accounts, link_rows
from rules import category_of, load_rules, required_ops

# fields that identify a person directly (what redaction / anonymization must remove)
PII_FIELDS = {"email", "phone", "full_name", "display_name", "name", "email_hash", "requester_email",
              "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
              "lookup_key", "match_key", "subject_key", "matched_value", "query", "message_text"}
# stable references a tombstone or retained record may keep
REF_FIELDS = {"customer_reference", "user_id", "subject_reference", "subject_ref", "correlation_key"}
STATUS_FIELDS = ("result", "account_status", "http_status", "rows", "status_code", "enabled",
                 "restore_allowed", "last_delivery_status", "total_hits", "total", "download_available",
                 "retention_until", "retained_until", "event_code", "event_name", "record_type",
                 "integrity_check", "marker_found", "access", "reason")
SNAPSHOT_KINDS = {"sql", "sql_scope", "json_scope"}
FREE_TEXT = {"message_text", "query", "lookup_key", "match_key", "subject_key", "matched_value"}
EMAIL_OR_PHONE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{7,}\d")

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OP_STATE = {  # operation -> (phase, state), in the order the problem statement lists them
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OP_ORDER = {op: i for i, op in enumerate(OP_STATE)}


class Data:
    """Everything loaded once per run and shared by all cases (no LLM involved)."""

    def __init__(self, requests):
        self.requests = {r["case_id"]: r for r in requests}
        self.rows = load_text_evidence()
        self.people = load_people()
        self.activity = load_activity()
        self.rules = load_rules()
        self.file_time = {m["file_id"]: m["captured_at"] for m in load_manifest()}
        self.linked = link_rows(self.rows, self.people, self.activity)
        self.accounts = assign_accounts(requests, self.activity, self.linked, self.people)
        self.email_to_user = {e: p["user_id"] for p in self.people.values() for e in p["emails"]}
        with open(DATASET / "user_audit_log.csv", encoding="utf-8") as f:
            self.audit = list(csv.DictReader(f))
        self.audit_time = {a["audit_id"]: a["event_time"] for a in self.audit}
        self.build_indexes()

    def build_indexes(self):
        """Lookups derived from rows/activity (also used by the hand-built evaluation cases)."""
        self.act_time = {a["activity_id"]: a["event_time"] for a in self.activity}
        self.by_rid = defaultdict(list)
        self.cover = defaultdict(set)  # system -> files that captured it as a snapshot
        self.table_cover = defaultdict(set)  # table / collection -> files that contain it
        for r in self.rows:
            if r["resource_id"]:
                self.by_rid[r["resource_id"]].append(r)
            if r["kind"] in SNAPSHOT_KINDS and r["system"]:
                self.cover[r["system"]].add(r["file_id"])
            if r["kind"] in ("sql_table", "json_scope") and r["table"]:
                self.table_cover[r["table"]].add(r["file_id"])
        self.act_by_rid = defaultdict(list)
        for a in self.activity:
            self.act_by_rid[a["resource_id"]].append(a)


# ---------- tool functions (case-bound) ----------

def lookup_person(data, case, email):
    uid = data.email_to_user.get(norm_email(email))
    if not uid:
        return f"No person found for {email}."
    p = data.people[uid]
    lines = [f"user_id: {uid}",
             f"emails (current + old): {', '.join(sorted(p['emails']))}",
             f"phones: {', '.join(sorted(p['phones']))}",
             f"email hashes: {len(p['hashes'])} known"]
    for a in data.audit:
        if a["user_id"] == uid and a["field_name"] != "email_hash" and a["event_time"] <= case["audit_at"]:
            lines.append(f"{a['audit_id']} {a['event_time'][:10]}: {a['field_name']} changed {a['old_value']} -> {a['new_value']}")
    return "\n".join(lines)


def find_records(data, case, user_id):
    cutoff = case["audit_at"]
    found = {}  # resource_id -> how we know
    # a fact counts if it was observed by audit_at, even when the file holding it was exported later
    # citable files first so the label names one when possible
    last = defaultdict(str)
    for r in sorted(data.linked.get(user_id, []), key=lambda r: r["captured_at"] > cutoff):
        if r["resource_id"] and r["time"] <= cutoff:
            found.setdefault(r["resource_id"], f"seen with this person's identifiers in {r['file_id']}")
            last[r["resource_id"]] = max(last[r["resource_id"]], r["time"])
    # a record only ever seen before the request says nothing about what survived the deletion
    for rid, t in last.items():
        if t < case["requested_at"]:
            del found[rid]
    acc = data.accounts.get(case["case_id"])
    if acc:
        found.setdefault(acc, "primary account paired with this request through deletion activity")
    # processes / copies the activity stream ties to these records (e.g. a connector that repopulated one)
    for rid in list(found):
        for a in data.activity:
            if a["event_time"] <= cutoff and rid in a["details"] and a["resource_id"] != rid \
                    and a["resource_id"] not in found and system_of(a["resource_id"]):
                found[a["resource_id"]] = f"{a['activity_id']} links it to {rid}"
    if not found:
        return "No records linked to this person before audit_at."
    return "\n".join(f"{system_of(rid) or '?'} | {rid} | {how}" for rid, how in sorted(found.items()))


def describe(row):
    d = row["data"]
    if row["kind"] in ("image", "pdf"):
        return f"text: {d['text'][:160]}"
    # free text only exposes someone if it really holds an email or phone ("[REDACTED]" does not)
    pii = sorted(k for k in d if k in PII_FIELDS and d[k] not in (None, "", "null")
                 and (k not in FREE_TEXT or EMAIL_OR_PHONE.search(str(d[k]))))
    refs = [f"{k}={d[k]}" for k in REF_FIELDS if d.get(k) not in (None, "", "null")]
    status = [f"{k}={d[k]}" for k in STATUS_FIELDS if d.get(k) not in (None, "")]
    parts = [f"identifiers visible: {', '.join(pii)}" if pii else "no direct identifiers"]
    if refs:
        parts.append("refs: " + ", ".join(refs))
    if status:
        parts.append(", ".join(status))
    return " | ".join(parts)


def get_record_state(data, case, resource_id):
    cutoff = case["audit_at"]
    system = system_of(resource_id)
    lines, seen = [], set()
    rows = sorted((r for r in data.by_rid.get(resource_id, []) if r["time"] <= cutoff),
                  key=lambda r: (r["time"], r["captured_at"]))
    for r in rows:
        cite = "" if r["captured_at"] <= cutoff else " [file exported after audit_at: use as fact, do not cite]"
        if r["time"] < case["requested_at"]:
            cite += " [before the request: shows it existed, not that it survived deletion]"
        line = f"{r['file_id']} (observed {r['time'][:16]}, {r['kind']} {r['table']}){cite}: {describe(r)}"
        if line not in seen:  # log lines of one trace repeat the same facts
            seen.add(line)
            lines.append(line)
    out = [f"{resource_id} ({system}) observations up to audit_at {cutoff}:"] + (lines or ["(none)"])
    citable = [r for r in rows if r["captured_at"] <= cutoff]
    last_seen = max((max(r["time"], r["captured_at"]) for r in rows), default="")
    if citable:
        last = max(citable, key=lambda r: r["captured_at"])
        out.append(f"latest citable file mentioning it: {last['file_id']} ({last['captured_at'][:16]})")
    # absence only counts in a newer capture of the same table/copy the record was last seen in
    # (a primary-table snapshot says nothing about a failover replica copy)
    tables = {r["table"] for r in rows if r["kind"] in ("sql", "json") and r["table"]}
    files = set().union(*(data.table_cover[t] for t in tables)) if tables else data.cover.get(system, set())
    snaps = sorted((data.file_time[f], f) for f in files if data.file_time[f] <= cutoff)
    if snaps and snaps[-1][0] > last_seen and not any(r["file_id"] == snaps[-1][1] for r in rows):
        t, f = snaps[-1]
        where = f"table {', '.join(sorted(tables))}" if tables else system
        out.append(f"newer snapshot of {where} without this record: {f} ({t[:16]})")
    elif tables and rows and rows[-1]["kind"] in ("sql", "json"):
        out.append(f"no newer snapshot of table {', '.join(sorted(tables))} - the last sighting stands")
    if citable:
        cite = max(citable, key=lambda r: r["captured_at"])["file_id"]
    elif snaps:
        cite = snaps[-1][1]
    else:
        cite = ""
    if cite:
        out.append(f"evidence to cite for this record's state at audit_at: {cite}")
    acts = [a for a in data.act_by_rid.get(resource_id, []) if a["event_time"] <= cutoff]
    acts += [a for a in data.activity if a["event_time"] <= cutoff and resource_id in a["details"]
             and a["resource_id"] != resource_id]
    for a in sorted(acts, key=lambda a: a["event_time"]):
        out.append(f"{a['activity_id']} {a['event_time'][:16]} {a['event_type']} {a['source_system']}->"
                   f"{a['target_system']} result={a['reported_result']} (claim): {a['details']}")
    return "\n".join(out)


def get_policy(data, case, resource_id):
    system = system_of(resource_id)
    cat = category_of(system, data.by_rid.get(resource_id, []))
    find, fix, verify = required_ops(cat, data.rules)
    action = data.rules.get(cat, "DISABLE" if cat == "DATA_FLOW" else "?")
    return (f"{resource_id}: category {cat}, required action {action}. "
            f"If not yet compliant -> finding {find or '-'}, remediation {fix or '-'}, verification {verify}. "
            f"If already compliant -> verification {verify} citing the evidence that proves it.")


def submit_answer(data, case, status, steps):
    """Validate; on success return (None, normalized steps), else (errors, None)."""
    errors = []
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        errors.append(f"status must be COMPLETE, INCOMPLETE or NOT_APPLICABLE, got {status!r}")
    if not isinstance(steps, list):
        return ["steps must be a list"], None
    if status == "NOT_APPLICABLE" and steps:
        errors.append("NOT_APPLICABLE must have no steps")
    if status in ("COMPLETE", "INCOMPLETE") and not steps:
        errors.append(f"{status} needs at least one step")
    out = []
    for i, s in enumerate(steps, 1):
        op = s.get("operation")
        if op not in OP_STATE:
            errors.append(f"step {i}: unknown operation {op!r}")
            continue
        phase, state = OP_STATE[op]
        ev = s.get("evidence_id") or ""
        rid = s.get("resource_id") or ""
        sysid = s.get("system_id") or system_of(rid)
        if not rid:
            errors.append(f"step {i}: resource_id missing")
        else:
            allowed = set(required_ops(category_of(sysid, data.by_rid.get(rid, [])), data.rules)) - {None}
            if op not in allowed:
                errors.append(f"step {i}: {op} does not fit the policy for {rid} (allowed: {', '.join(sorted(allowed))})")
        if status == "COMPLETE" and phase != "VERIFICATION":
            errors.append(f"step {i}: COMPLETE answers may only contain VERIFICATION steps")
        needs_ev = phase == "FINDING" or status == "COMPLETE"
        if needs_ev and not ev:
            errors.append(f"step {i} ({op} {rid}): evidence_id required")
        if not needs_ev and ev:
            ev = ""  # proposed future work never cites evidence
        if ev:
            t = data.file_time.get(ev) or data.act_time.get(ev) or data.audit_time.get(ev)
            if not t:
                errors.append(f"step {i}: evidence_id {ev!r} does not exist")
            elif t > case["audit_at"]:
                errors.append(f"step {i}: evidence_id {ev} is after audit_at")
        out.append({"phase": phase, "system_id": sysid, "resource_id": rid,
                    "operation": op, "state": state, "evidence_id": ev})
    if errors:
        return errors, None
    uniq = {json.dumps(s, sort_keys=True): s for s in out}.values()
    ordered = sorted(uniq, key=lambda s: (PHASES.index(s["phase"]), OP_ORDER[s["operation"]],
                                          s["system_id"], s["resource_id"]))
    return None, [{"step_no": n, **s} for n, s in enumerate(ordered, 1)]


# ---------- schemas for the model ----------

def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


STR = {"type": "string"}
TOOL_SCHEMAS = [
    _fn("lookup_person", "Resolve an email (current or old) to the person: user_id, all emails, phones, "
        "and identity-change audit entries (audit_id).", {"email": STR}, ["email"]),
    _fn("find_records", "List every record linked to this person before audit_at, with how it was linked.",
        {"user_id": STR}, ["user_id"]),
    _fn("get_record_state", "Timeline of one record up to audit_at: each evidence file's observation, the "
        "latest file mentioning it, the latest snapshot of its system, and activity claims.",
        {"resource_id": STR}, ["resource_id"]),
    _fn("get_policy", "Data category and required treatment for one record under deletion_rules.csv, and "
        "which operations that implies.", {"resource_id": STR}, ["resource_id"]),
    _fn("submit_answer", "Submit the final answer. It is validated; if invalid you get the errors back and "
        "must fix and resubmit. Step order and numbering are handled for you.",
        {"deletion_status": {"type": "string", "enum": ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]},
         "steps": {"type": "array", "items": {"type": "object", "properties": {
             "system_id": STR, "resource_id": STR, "operation": {"type": "string", "enum": list(OP_STATE)},
             "evidence_id": STR}, "required": ["resource_id", "operation", "evidence_id"]}}},
        ["deletion_status", "steps"]),
]
TOOLS = {"lookup_person": lookup_person, "find_records": find_records,
         "get_record_state": get_record_state, "get_policy": get_policy}
