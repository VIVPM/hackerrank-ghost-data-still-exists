# Evaluation report

## 1. What was evaluated

- **Scored set (42 requests):** the 32 public samples (`sample_deletion_requests.csv`, answers in
  `sample_output.csv`) and the 10 real requests whose wording is not a standard deletion phrasing.
  These 10 have no published answer, but each is clearly an access, correction, export or marketing
  request, so it must come out `NOT_APPLICABLE`. Selection: `python code/main.py --cases eval`.
- **Final run (518 requests):** no published answers exist, so it gets a format check only
  (section 5).
- **Hand-built cases:** 6 small invented scenarios that test the agent's tools directly (section 4).

Scores come from the final agent settings (`zai-org/GLM-5.3`, low reasoning, 25-tool limit), run
`runs/glm53_low_v7`. Reproduce them with `python code/evaluation/main.py runs/glm53_low_v7`.

### How each metric is computed (`code/evaluation/main.py`)

| Metric | Definition |
|---|---|
| Deletion-status accuracy | share of cases whose `deletion_status` equals the answer |
| Step precision / recall / F1 | a step counts as correct only if all six fields match an answer step (`phase, system_id, resource_id, operation, state, evidence_id`) |
| Field accuracy (aligned) | each answer step is paired with our step for the same record (same `resource_id` and phase), then each field is compared. A missing step scores 0 on every field. |
| Field accuracy (strict) | step *i* compared with step *i* by position, so one missing step also counts the later positions as wrong |
| Sequence accuracy | share of cases whose whole step list is identical to the answer, in order |

## 2. Results on the 32 samples

| Metric | Result |
|---|---|
| **Deletion-status accuracy** | **100.0%** (32/32) |
| NOT_APPLICABLE on the 10 non-deletion requests | **10/10** |
| **Step precision / recall / F1** | **97.3% / 93.3% / 95.2%** |
| **Field accuracy (aligned)** | **93.3%** |
| Field accuracy (strict, by position) | 90.7% |
| **Sequence accuracy** | **62.5%** (20/32) |

Per-field accuracy (aligned):

| phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|
| 93.3% | 93.3% | 93.3% | 93.3% | 93.3% | 93.3% |

The per-field scores are all equal because every record the agent finds has all six fields right.
The only losses are records it does not find (section 6).

Status confusion (answer → ours):

| | ours COMPLETE | ours INCOMPLETE |
|---|---|---|
| answer COMPLETE | 10 | 0 |
| answer INCOMPLETE | 0 | 22 |

### Model comparison (same 42 requests, same tools and prompt)

| Model | Status acc | Step F1 | Field acc (aligned) | Sequence acc | Cost | Time/request |
|---|---|---|---|---|---|---|
| **GLM-5.3, low reasoning (chosen)** | **100%** | **95.2%** | **93.3%** | **62.5%** | $0.43 | 6 s |
| DeepSeek-V4.1-Flash, low reasoning | 90.3%* | 84.8% | 90.1% | 54.8% | $0.46 | 27 s |

\* DeepSeek failed to answer case_11 within the limit, so it is scored on 31 samples. Its
`evidence_id` accuracy was the weakest field (84.4%).

## 3. Failures found and improvements made

Each row is a real mistake seen on the sample runs, and what was changed. The first GLM-5.3 run
(`glm53_low`) scored 93.5% status, 89.3% step F1, 54.8% sequence. The final run (`glm53_low_v7`)
scored 100% / 95.2% / 62.5%.

| # | Failure seen | Cause | Fix |
|---|---|---|---|
| 1 | case_05 marked COMPLETE although the failover replica still held the account | a newer snapshot of the *primary* table was read as proof the *replica* copy was gone | absence is proven only by a newer snapshot of the **same table**. Each SQL table and JSON collection now leaves a marker even when empty (`evidence.py`, `tools.get_record_state`) |
| 2 | case_27 support message flagged as exposed PII | the text `[REDACTED]` was treated as personal data | free text counts as exposed only if it contains an email or phone number (`tools.EMAIL_OR_PHONE`) |
| 3 | extra findings such as `object_19`, `account_6523` | records seen only before the request was made were reported as leftovers | records whose last sighting is before `requested_at` are left out. All 41 answer findings in the samples cite evidence from after the request. |
| 4 | case_12: "block restore" proposed for a security audit record | the model picked an operation the policy does not allow | `submit_answer` rejects any operation outside the record's policy (security logs must be **retained**) and tells the agent why |
| 5 | an audit record cited from a file exported after `audit_at` | the file's export time was mixed up with the time the record was observed | two separate times: a row's observed time decides what was **known** at `audit_at`, the file's capture time decides what may be **cited** |
| 6 | the cited evidence changed between runs for the same record | several valid files, no rule for picking one | `get_record_state` prints one "evidence to cite" line per record |
| 7 | case_27 turned into NOT_APPLICABLE | "please confirm my deletion is finished" was read as not being a deletion request | prompt rule: asking to check, confirm or finish a deletion **is** a deletion request |
| 8 | unrelated rows tagged to a person | a user alias like `user_17` matched inside other IDs | aliases are matched as whole words only, and an alias that is just a bare user_id is ignored |
| 9 | OCR read `3export_1259` from a screenshot | row number glued to the ID | the resource-ID pattern must not follow a letter or digit. The missing `marketing_` prefix was added. |
| 10 | case_11 ran out of tool calls (15) | a large case with many records | limit raised to 25. When it runs out, the agent is forced to submit its best answer instead of failing. |
| 11 | retried cases lost the tokens of earlier attempts | usage was overwritten | usage from every attempt is accumulated, so the usage report counts every token paid for |

## 4. Hand-built evaluation cases (`code/evaluation/custom_cases.py`)

These are invented scenarios, each a few hand-written evidence rows. They run the agent's real tools
and `submit_answer` checks without calling the model, so they are free and give the same result
every time. Each one reproduces a failure from section 3.
Run: `python code/evaluation/custom_cases.py`

| # | Scenario | Expected | Result |
|---|---|---|---|
| 1 | Account deleted from the primary table, still in the failover replica | the newer primary snapshot is **not** proof the replica copy is gone, so the replica sighting stands | PASS |
| 2 | Support text `[REDACTED]` vs support text containing an email | first: no direct identifiers; second: `message_text` exposes identifiers | PASS |
| 3 | A file seen only in February, request made in August | the February-only record is left out, the September record is kept | PASS |
| 4a | `BLOCK_RESTORE` on a security audit record | rejected: policy is RETAIN, only `VERIFY_RECORD_PRESENT` allowed | PASS |
| 4b | A finding citing a file captured after `audit_at` | rejected | PASS |
| 4c | Steps submitted out of order, with evidence on a remediation step | sorted FINDING → REMEDIATION → VERIFICATION, numbered 1..n, remediation `evidence_id` emptied | PASS |

**6/6 passed.**

## 5. Final 518-request run: format check

`python code/evaluation/main.py output.csv`

| Check | Result |
|---|---|
| Rows | 518, one per `case_id`, sorted |
| Valid statuses / JSON / step numbering / phase order | 0 problems |
| Operation, phase and state combinations | 0 problems |
| COMPLETE cases contain only evidence-backed verification steps | 0 problems |
| Remediation steps cite no evidence | 0 problems |
| Failed or empty answers | 0 |
| Status mix | INCOMPLETE 360, COMPLETE 148, NOT_APPLICABLE 10 |

## 6. Remaining errors (known limits)

12 of the 32 samples are not an exact match. Every difference is one or two verification steps:

| Kind | Cases | Why it is hard |
|---|---|---|
| Anonymized analytics record not found | case_03, 04, 21, 25, 26, 27, 28 (7 steps) | the record was anonymized correctly, so it carries no name, email, phone or user_id that links it to the person. Nothing in the evidence connects it back. |
| Wrong account chosen when two accounts are equally likely | case_23/29, case_28/29, case_04 | two requests compete for accounts with the same deletion activity. The pairing rule (owner evidence, then status, then nearest time) gets 28 of 32 sample accounts right. |
| Retained record that appears in no evidence file | case_18 (`audit_record_6645`), case_20 (`audit_record_5569`), case_04 (`account_004`) | no file or activity row links the record to the person |
| One extra step | case_11 (`support_8642`) | a support record linked to the person whose answer step is absent. No evidence explains why. |

None of these change the status of a case. Status accuracy stays at 100%.
