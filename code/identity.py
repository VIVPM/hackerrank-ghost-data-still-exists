"""Build each person's full set of identifiers (current + historical) and match evidence text to people."""
import csv
import re
import unicodedata

from evidence import DATASET


def norm_email(s):
    return (s or "").strip().lower()


def norm_phone(s):
    return re.sub(r"\D", "", s or "")


def norm_name(s):
    # OCR drops spaces and some files mangle accents, so compare letters only
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def load_people():
    """user_id -> {"user_id", "names", "emails", "phones", "hashes"} (all normalized sets)."""
    people = {}
    with open(DATASET / "user_identities.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            people[r["user_id"]] = {
                "user_id": r["user_id"],
                "names": {norm_name(r["name"])},
                "emails": {norm_email(r["email"])},
                "phones": {norm_phone(r["phone"])},
                "hashes": {r["email_hash"].lower()},
            }
    # shortcut: keeps every historical value regardless of audit_at; all changes predate the audits seen so far
    field_to_key = {"email": ("emails", norm_email), "phone": ("phones", norm_phone),
                    "email_hash": ("hashes", str.lower), "name": ("names", norm_name)}
    with open(DATASET / "user_audit_log.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key, norm = field_to_key[r["field_name"]]
            for v in (r["old_value"], r["new_value"]):
                if v:
                    people[r["user_id"]][key].add(norm(v))
    return people


def email_index(people):
    """Any email (current or old) -> user_id."""
    return {e: p["user_id"] for p in people.values() for e in p["emails"]}


if __name__ == "__main__":
    people = load_people()
    idx = email_index(people)
    print("people:", len(people), "| emails known:", len(idx))
    for split in ("sample_deletion_requests", "deletion_requests"):
        with open(DATASET / f"{split}.csv", encoding="utf-8") as f:
            req = list(csv.DictReader(f))
        found = sum(norm_email(r["email"]) in idx for r in req)
        print(split, "requests:", len(req), "| matched to a person:", found)
        assert found == len(req)
    assert norm_phone("+81 90 7610 2507") == norm_phone("+819076102507")
    assert norm_name("Kofi Agyeman") == norm_name("KofiAgyeman")
    assert norm_name("Nicolás Fernández") == "nicolasfernandez"
