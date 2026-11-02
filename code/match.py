"""Link evidence rows to people.

A row links to a person when it shows one of their identifiers (email, phone, email hash,
user_id, or a unique name). A row with no identifiers (e.g. a redacted record) still links
through its resource_id if another row already tied that resource to the person.
"""
import re
from collections import Counter, defaultdict

from evidence import system_of
from identity import load_people, norm_email, norm_name, norm_phone

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
USER = re.compile(r"\buser_\d+\b")
HASH = re.compile(r"\b[0-9a-f]{64}\b")


def build_lookup(people):
    unique_names = {n for n, c in Counter(n for p in people.values() for n in p["names"]).items() if c == 1}
    lookup = {}
    for uid, p in people.items():
        lookup[uid] = uid
        for e in p["emails"]:
            lookup[e] = uid
        for h in p["hashes"]:
            lookup[h] = uid
        for ph in p["phones"]:
            lookup["tel:" + ph] = uid
        for n in p["names"] & unique_names:
            lookup["name:" + n] = uid
    return lookup


def row_keys(row):
    """All identifier keys visible in a row, normalized like build_lookup."""
    keys = set()
    for k, v in row["data"].items():
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if not isinstance(v, str) or not v:
            continue
        low = v.lower()
        keys.update(norm_email(e) for e in EMAIL.findall(low))
        keys.update(USER.findall(low))
        keys.update(HASH.findall(low))
        if "phone" in k:
            keys.add("tel:" + norm_phone(v))
        if k in ("full_name", "display_name", "requester_name", "name"):
            keys.add("name:" + norm_name(v))
        if k == "text":
            # free text from PDFs/screenshots: phones and names have no field label here
            for m in re.findall(r"\+[\d\s().-]{8,}\d", v):
                keys.add("tel:" + norm_phone(m))
            keys.add("textname:" + norm_name(v))
    return keys


def catalog_links(activity):
    """LINK_RESOURCE_IDENTITY rows name the owner outright: resource_id -> user_id."""
    out, aliases = {}, {}
    for a in activity:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = USER.search(a["details"])
            if m:
                out[a["resource_id"]] = m.group(0)
                # "(delivery_confirmation_19.pdf)" / "(NST-2026-0718-1842)": the name the record shows on screen
                alias = re.search(r"\(([^)]+)\)", a["details"])
                # a bare user_id is not a display name: it appears on every record of that person
                if alias and alias.group(1) != a["resource_id"] and not USER.fullmatch(alias.group(1)):
                    aliases[alias.group(1).lower()] = (a["resource_id"], m.group(0))
    return out, aliases


def link_rows(rows, people, activity=()):
    """Returns {user_id: [rows]}."""
    lookup = build_lookup(people)
    names = [(k[5:], uid) for k, uid in lookup.items() if k.startswith("name:")]
    catalog, aliases = catalog_links(activity)
    direct = defaultdict(set)  # user_id -> row indexes
    for i, row in enumerate(rows):
        text = row["data"].get("text", "").lower() if row["kind"] in ("image", "pdf") else ""
        for alias, (rid, uid) in aliases.items():
            if not row["resource_id"] and re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", text):
                row["resource_id"], row["system"] = rid, system_of(rid)
        if row["resource_id"] in catalog:
            # the catalog owner beats whatever identifier a (possibly stale) screen shows
            direct[catalog[row["resource_id"]]].add(i)
            continue
        keys = row_keys(row)
        for k in keys:
            if k.startswith("textname:"):
                blob = k[9:]
                for n, uid in names:
                    if len(n) >= 8 and n in blob:
                        direct[uid].add(i)
            elif k in lookup:
                direct[lookup[k]].add(i)

    # screenshots/PDFs about exactly one person: every row on that page belongs to them
    by_file = defaultdict(set)
    for uid, idxs in direct.items():
        for i in idxs:
            if rows[i]["kind"] in ("image", "pdf"):
                by_file[rows[i]["file_id"]].add(uid)
    for fid, uids in by_file.items():
        if len(uids) == 1:
            uid = next(iter(uids))
            direct[uid].update(i for i, r in enumerate(rows) if r["file_id"] == fid)

    # rows with no identifiers left (redacted / deleted) still link through their resource_id
    by_rid = defaultdict(set)
    for i, r in enumerate(rows):
        if r["resource_id"]:
            by_rid[r["resource_id"]].add(i)
    linked = {}
    for uid, idxs in direct.items():
        rids = {rows[i]["resource_id"] for i in idxs if rows[i]["resource_id"]}
        all_idx = set(idxs)
        for rid in rids:
            all_idx |= by_rid[rid]
        linked[uid] = [rows[i] for i in sorted(all_idx)]
    return linked


REPORTED_TO_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


def assign_accounts(requests, activity, linked, people):
    """Pair each request with its primary account_* record. {case_id: resource_id}

    Deleted accounts often survive only as an activity row with no identifiers, so pair by:
    1. an account already tied to the person through evidence wins;
    2. the deletion result agrees with the reported status (COMPLETE~SUCCESS, ...);
    3. closest to reported_at, between requested_at and 30 min after reported_at.
    Each account goes to one request only (all requests compete together).
    """
    from datetime import datetime

    def t(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    owners = defaultdict(set)
    for uid, rs in linked.items():
        for r in rs:
            if r["resource_id"].startswith("account_"):
                owners[r["resource_id"]].add(uid)
    idx = {e: p["user_id"] for p in people.values() for e in p["emails"]}
    acts = [a for a in activity if a["resource_id"].startswith("account_")
            and a["source_system"] in ("deletion_worker", "privacy_intake")]
    pairs = []
    for r in requests:
        uid = idx[norm_email(r["email"])]
        for a in acts:
            o = owners.get(a["resource_id"], set())
            if o and uid not in o:
                continue
            minutes = (t(r["reported_at"]) - t(a["event_time"])).total_seconds() / 60
            if r["requested_at"] <= a["event_time"] and minutes >= -30:
                same = a["event_type"] == "CLASSIFY_REQUEST" or REPORTED_TO_RESULT[r["reported_status"]] == a["reported_result"]
                pairs.append((uid not in o, not same, abs(minutes), r["case_id"], a["resource_id"]))
    # shortcut: ties (same status, same report time, same result) are decided by time only; 3 of 32 samples hit this
    got, used = {}, set()
    for *_, case_id, rid in sorted(pairs):
        if case_id not in got and rid not in used:
            got[case_id] = rid
            used.add(rid)
    return got


if __name__ == "__main__":
    import csv
    import json

    from evidence import DATASET, load_activity, load_text_evidence

    rows = load_text_evidence()
    people = load_people()
    activity = load_activity()
    linked = link_rows(rows, people, activity)
    print("people with evidence:", len(linked), "of", len(people))

    # resources tied to more than one person would mean a bad match
    owners = defaultdict(set)
    for uid, rs in linked.items():
        for r in rs:
            if r["resource_id"]:
                owners[r["resource_id"]].add(uid)
    shared = {rid: u for rid, u in owners.items() if len(u) > 1}
    print("resource_ids linked to >1 person:", len(shared), list(shared.items())[:5])

    # recall on the samples: every resource in the correct answer should be among the person's linked resources
    idx = {e: p["user_id"] for p in people.values() for e in p["emails"]}
    with open(DATASET / "sample_deletion_requests.csv", encoding="utf-8") as f:
        req = {r["case_id"]: r for r in csv.DictReader(f)}
    with open(DATASET / "deletion_requests.csv", encoding="utf-8") as f:
        real = list(csv.DictReader(f))
    accounts = assign_accounts(list(req.values()) + real, activity, linked, people)
    print("real requests paired with an account:", sum(r["case_id"] in accounts for r in real), "/", len(real))
    total = hit = 0
    with open(DATASET / "sample_output.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            uid = idx[norm_email(req[r["case_id"]]["email"])]
            mine = {x["resource_id"] for x in linked.get(uid, [])} | {accounts.get(r["case_id"])}
            for s in json.loads(r["steps_json"]):
                total += 1
                if s["resource_id"] in mine:
                    hit += 1
                else:
                    print("  missed:", r["case_id"], uid, s["system_id"], s["resource_id"], s["evidence_id"])
    print(f"sample steps whose resource we linked: {hit}/{total}")
