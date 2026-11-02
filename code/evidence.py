"""Read the text evidence (SQL, JSON, logs, TXT) into one flat list of rows.

Every row looks like:
    {"file_id", "captured_at", "kind", "table", "system", "resource_id", "time", "data"}
`data` holds the record's own fields (email, phone, status, ...).
"""
import csv
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset"

# resource_id prefix -> system name used in the answers
PREFIX_TO_SYSTEM = {
    "account": "account_service",
    "analytics": "analytics_platform",
    "backup": "backup_service",
    "export": "export_service",
    "tombstone": "identity_service",
    "data_flow": "integration_service",
    "search": "search_service",
    "audit_record": "security_service",
    "support": "support_platform",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
}
SYSTEMS = set(PREFIX_TO_SYSTEM.values())
TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "updated_at", "indexed_at")


def system_of(resource_id):
    prefix = re.sub(r"_\d+$", "", resource_id or "")
    return PREFIX_TO_SYSTEM.get(prefix, "")


def make_row(meta, kind, table, data, system="", time=""):
    rid = data.get("resource_id") or ""
    return {
        "file_id": meta["file_id"],
        "captured_at": meta["captured_at"],
        "kind": kind,
        "table": table,
        "system": system or data.get("origin_system") or system_of(rid),
        "resource_id": rid,
        "time": time or next((data[k] for k in TIME_KEYS if data.get(k)), "") or meta["captured_at"],
        "data": data,
    }


def flatten(d):
    """Lift fields of nested dicts (e.g. profile, document) up one level; lists stay as-is."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in flatten(v).items():
                out.setdefault(k2, v2)
        else:
            out[k] = v
    return out


# ---------- SQL ----------

def read_sql(path, meta):
    db = sqlite3.connect(":memory:")
    db.executescript(path.read_text(encoding="utf-8"))
    rows = []
    tables = [t for (t,) in db.execute("select name from sqlite_master where type='table'")]
    for t in tables:
        # one marker per table, even an empty one: a later file with this table but without a record shows it is gone
        rows.append(make_row(meta, "sql_table", t, {}))
        cur = db.execute(f'select * from "{t}"')
        cols = [c[0] for c in cur.description]
        for values in cur:
            data = dict(zip(cols, values))
            if data.get("record_json"):
                # replica tables keep the real record as JSON text
                data.update(flatten(json.loads(data.pop("record_json"))))
            if t == "snapshot_scope":
                # says which systems this snapshot covered (a missing row there means "absent")
                rows.append(make_row(meta, "sql_scope", t, data, system=data["origin_system"]))
            else:
                rows.append(make_row(meta, "sql", t, data))
    return rows


# ---------- JSON ----------

def walk_json(node, ctx, meta, rows):
    if isinstance(node, list):
        for item in node:
            walk_json(item, ctx, meta, rows)
        return
    if not isinstance(node, dict):
        return
    ctx = dict(ctx)
    for k in ("data_store", "system", "collection"):
        if isinstance(node.get(k), str):
            ctx["system" if k != "collection" else "collection"] = node[k]
    for k in TIME_KEYS:
        if isinstance(node.get(k), str):
            ctx["time"] = node[k]
            break
    if isinstance(node.get("records"), list) and ctx.get("system") in SYSTEMS:
        # a captured collection, even an empty one, shows which system this file covered
        rows.append(make_row(meta, "json_scope", ctx.get("collection", ""), {"origin_system": ctx["system"]},
                             system=ctx["system"], time=ctx.get("time", "")))
    if "resource_id" in node:
        rows.append(make_row(meta, "json", ctx.get("collection", ""), flatten(node),
                             system=ctx.get("system", "") if ctx.get("system") in SYSTEMS else "",
                             time=ctx.get("time", "")))
    for k, v in node.items():
        if isinstance(v, (dict, list)):
            # service_exports uses system names as keys
            child = dict(ctx, system=k) if k in SYSTEMS else ctx
            walk_json(v, child, meta, rows)


def read_json(path, meta):
    rows = []
    walk_json(json.loads(path.read_text(encoding="utf-8")), {}, meta, rows)
    return rows


# ---------- TXT handover ----------

def read_txt(path, meta):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == 6:
            ts, system, rid, rtype, result, subject = parts
            data = {"resource_id": rid, "record_type": rtype, "result": result, "subject_reference": subject}
            rows.append(make_row(meta, "txt", "handover", data, system=system, time=ts))
    return rows


# ---------- logs ----------

KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_kv(text):
    out = {}
    for k, v in KV.findall(text):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = None if v == "null" else v
    return out


def parse_log_line(line):
    m = re.match(r"(\d{4}-\d\d-\d\dT[\d:]+Z)\s+(.*)", line)
    if not m:
        return None
    ts, body = m.groups()
    if body.startswith("{"):
        kv = {k: (None if v == "null" else v) for k, v in json.loads(body).items()}
    else:
        kv = parse_kv(body)
        if "message" in kv:  # cloud_runtime: real fields hide inside message="..."
            kv.update(parse_kv(kv.pop("message")))
        # layouts that put service/trace/event in fixed positions
        for pat in (r"\[\w+\] \[(?P<service>\w+)\] \[(?P<trace_id>[\w-]+)\] (?P<event>\w+):",
                    r"\| \w+ \| (?P<service>\w+) \| (?P<trace_id>[\w-]+) \| (?P<event>\w+) \|",
                    r"<\d+>1 \S+ (?P<service>\w+) (?P<trace_id>[\w-]+) - -"):
            pm = re.search(pat, body)
            if pm:
                for k, v in pm.groupdict().items():
                    kv.setdefault(k, v)
    kv.setdefault("service", kv.get("function") or kv.get("logger"))
    kv.setdefault("trace_id", kv.get("requestId"))
    return ts, kv


def read_log(path, meta):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_log_line(line)
        if not parsed:
            continue
        ts, kv = parsed
        system = kv.get("system") or (kv.get("service") if kv.get("service") in SYSTEMS else "") or ""
        rows.append(make_row(meta, "log", path.name, kv, system=system, time=ts))
    # the email is usually on the "started" line and the resource_id on the "completed" line;
    # copy fields across lines that share a trace_id
    by_trace = {}
    for r in rows:
        if r["data"].get("trace_id"):
            by_trace.setdefault(r["data"]["trace_id"], []).append(r)
    for group in by_trace.values():
        merged = {}
        for r in group:
            for k, v in r["data"].items():
                if v is not None:
                    merged.setdefault(k, v)
        for r in group:
            for k, v in merged.items():
                if r["data"].get(k) is None:
                    r["data"][k] = v
            if not r["resource_id"]:
                r["resource_id"] = r["data"].get("resource_id") or ""
                r["system"] = r["system"] or system_of(r["resource_id"])
    return rows


# ---------- PDFs and screenshots (free text) ----------

# no leading \b: OCR sometimes glues a row number on ("3export_1259")
RID = re.compile(r"(?<![a-z_])(?:" + "|".join(sorted(PREFIX_TO_SYSTEM, key=len, reverse=True)) + r")_\d+\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def text_row(meta, kind, table, text):
    """One row from a line of free text, with the identifiers we can see in it."""
    low = text.lower()
    rids = RID.findall(low)
    data = {
        "text": text,
        "resource_id": rids[0] if rids else "",
        "resource_ids": rids,
        "emails": EMAIL.findall(low),
        "user_ids": re.findall(r"\buser_\d+\b", low),
        "orders": re.findall(r"\bord-\d+\b", low),
    }
    return make_row(meta, kind, table, data)


def read_pdf(path, meta):
    import fitz  # PyMuPDF

    rows, rest = [], []
    for page in fitz.open(path):
        for line in page.get_text().splitlines():
            # table rows in these PDFs are written as "a | b | c"
            (rows.append(text_row(meta, "pdf", path.name, line)) if " | " in line else rest.append(line))
    rows.append(text_row(meta, "pdf", path.name, " ".join(rest)))
    return rows


_ocr = None


def read_image(path, meta):
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    import cv2  # installed with rapidocr

    img = cv2.imread(str(path))
    result, _ = _ocr(img)
    if not result:
        return []
    # shortcut: in these screens the right detail panel starts ~375px from the right edge; upgrade if a layout differs
    split = img.shape[1] - 400
    rows = []
    for side in ("main", "panel"):
        words = [((b[0][1] + b[2][1]) / 2, b[0][0], t) for b, t, _ in result
                 if (b[0][0] < split) == (side == "main")]
        lines = []
        for y, x, t in sorted(words):
            if lines and abs(lines[-1][0] - y) < 10:  # same height -> same row
                lines[-1][1].append((x, t))
            else:
                lines.append([y, [(x, t)]])
        for _, ws in lines:
            rows.append(text_row(meta, "image", f"{path.name}:{side}", " | ".join(t for _, t in sorted(ws))))
    return rows


# ---------- all together ----------

READERS = {".sql": read_sql, ".json": read_json, ".txt": read_txt, ".log": read_log,
           ".pdf": read_pdf, ".png": read_image}


def load_activity():
    with open(DATASET / "system_activity.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_manifest():
    with open(DATASET / "evidence_files.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_text_evidence():
    rows = []
    for meta in load_manifest():
        path = DATASET / meta["file_path"]
        reader = READERS.get(path.suffix)
        if reader:
            rows.extend(reader(path, meta))
    return rows


if __name__ == "__main__":
    from collections import Counter

    rows = load_text_evidence()
    manifest = load_manifest()
    text_files = {m["file_id"] for m in manifest if Path(m["file_path"]).suffix in READERS}
    files_with_rows = {r["file_id"] for r in rows}
    print("rows:", len(rows), "| text files:", len(text_files), "| files with rows:", len(files_with_rows))
    print("missing files:", sorted(text_files - files_with_rows))
    print("rows by kind:", dict(Counter(r["kind"] for r in rows)))
    print("rows by system:", dict(Counter(r["system"] for r in rows)))
    no_rid = [r for r in rows if not r["resource_id"] and r["kind"] != "sql_scope"]
    print("rows without resource_id:", len(no_rid))
    assert all(r["file_id"] and r["time"] for r in rows)
