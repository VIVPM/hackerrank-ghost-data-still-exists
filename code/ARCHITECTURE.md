# Architecture — Ghost Data Still Exists deletion-audit agent

## 1. Shape: one agent per case

For every request, one LLM agent investigates on its own. It runs a loop:

```
          ┌──────────────────────────────────────────────┐
          │  case row (email, times, request_text)       │
          │  + system prompt (rules of the audit)        │
          └───────────────────────┬──────────────────────┘
                                  ▼
                    ┌───────────────────────────┐
             ┌────▶│   LLM decides next action  │
             │      └─────────────┬─────────────┘
             │        tool call   │   or  submit_answer
             │                    ▼
             │      ┌───────────────────────────┐
             └──────│  tool runs, returns facts  │
                    └───────────────────────────┘
```

The code does **not** fix the order of steps. The agent chooses which tool to call,
reads the result, and decides when it has enough to answer. The loop ends when
`submit_answer` accepts an answer, or when the step limit is reached.

## 2. Tools (the agent's only way to see data)

Tools return **short, clean facts** (one line per record), never raw files, to save
tokens and avoid misreading. Every fact carries the `evidence_id` / `activity_id` /
`audit_id` that proves it. Tools never return anything captured after the case's
`audit_at`.

| Tool | Input | Returns | Built on |
|---|---|---|---|
| `lookup_person` | email | user_id, all current + old emails, phones, hashes (with `audit_id` of each change) | `identity.py` |
| `find_records` | user_id, audit_at | every resource linked to the person (system, resource_id, how it was linked) + the paired primary account | `match.py` |
| `get_record_state` | resource_id, audit_at | timeline up to audit_at: each observation (time, evidence_id, PRESENT / ABSENT / identifiers still visible / redacted) and each activity claim (activity_id, result) | `evidence.py` |
| `get_policy` | resource_id | data category, required action from `deletion_rules.csv`, and the finding / remediation / verification operations it implies | `rules.py` |
| `submit_answer` | status, steps | validates the answer; on error returns the list of problems so the agent can fix them; on success sorts, numbers and saves it | new |

`submit_answer` checks:
- status is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` (`NOT_APPLICABLE` ⇒ no steps)
- only allowed phase / operation / state combinations
- `COMPLETE` ⇒ only VERIFICATION steps, each with evidence
- FINDING steps cite evidence; REMEDIATION and INCOMPLETE VERIFICATION use `""`
- every cited id exists and is not after `audit_at`
- then it sorts by phase → operation order (problem statement table) → system_id → resource_id and numbers steps from 1

So the format rules can never be broken, while the **judgement** (deletion or not,
which records matter, what state they are in) stays with the agent.

## 3. What the agent must decide (system prompt)

1. Does `request_text` ask for deletion? If not → `NOT_APPLICABLE`, `[]`.
2. Who is the person (current + historical identifiers)?
3. Which records belong to them, and what state is each in **at audit_at**?
4. What does policy require for each record?
5. Is anything still wrong (record present, PII exposed, flow active, restore open)?
   - yes → `INCOMPLETE` with findings, remediation, verification
   - no → `COMPLETE` with evidence-backed verification
6. Reported status and activity results are claims, not proof.

## 4. Files

```
code/
├── main.py            run the agent on every case → output.csv (+ usage log)
├── agent.py           the loop: model call → tool call → result → repeat
├── tools.py           tool schemas + functions (wrap the modules below)
├── prompts/system.md  the agent's instructions
├── evidence.py        read all 94 evidence files into rows          (built)
├── identity.py        people + historical identifiers               (built)
├── match.py           link rows/accounts to people                  (built)
├── rules.py           category → required action → operations       (built)
├── requirements.txt
└── evaluation/
    ├── main.py        checker: scores samples, validates all rows, sums tokens/cost
    ├── evaluation_report.md
    └── usage_report.md
```

## 5. Model access

- Hugging Face Inference Providers, OpenAI-compatible chat endpoint with tool calling.
- Settings from environment variables only: `HF_TOKEN`, `AGENT_MODEL`, `AGENT_THINKING`.
- Temperature 0 for repeatable answers.
- Candidates to compare on 42 cases (32 samples + 10 real non-deletion requests):
  `deepseek-ai/DeepSeek-V4.1-Flash` and `zai-org/GLM-5.3`, both with low thinking.

## 6. Cost and safety controls

- Evidence is loaded once per run and shared by all cases (no LLM involved).
- Step limit per case (e.g. 25 tool calls) to stop loops and runaway cost.
- Each finished case is saved; a re-run skips cases already answered.
- Every model call records input / output tokens for `usage_report.md`.
- If the API fails for a case, the case is retried, then marked and reported, never silently guessed.

## 7. Known limits (from the data investigation)

- Some sample-answer records appear in no participant file (e.g. `account_004`).
- Anonymized analytics records with no identifiers cannot be tied to a person reliably.
- Three account pairings in the samples are true ties (same status, time and result).
