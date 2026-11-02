# Ghost Data Still Exists — deletion-audit agent

For each request in `dataset/deletion_requests.csv`, an LLM agent decides whether the person asked
for deletion. If they did, it checks whether the deletion really happened at `audit_at` and writes
`COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with ordered steps to `output.csv` at the repo root.

The agent (`zai-org/GLM-5.3` through the Hugging Face router) investigates each case by calling
tools: `lookup_person`, `find_records`, `get_record_state`, `get_policy` and `submit_answer`. Those
tools answer from an index built once from every evidence file (SQL, JSON, logs, text, PDF,
screenshots). `submit_answer` checks the answer against the policy and the time rules before
accepting it. Design details: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Setup

Python 3.12 (3.10+ should work). From the repository root:

```
python -m venv .venv
.venv\Scripts\activate          # Windows    (macOS/Linux: source .venv/bin/activate)
pip install -r code/requirements.txt
```

The dependencies are PyMuPDF (reads PDFs) and RapidOCR (reads screenshots). The model is called
with the Python standard library, so no SDK is needed.

Create a `.env` file at the repository root (it is git-ignored) with a Hugging Face token that has
Inference Providers access:

```
HF_TOKEN=hf_...
```

The `dataset/` folder must be at the repository root.

## Run

```
python code/main.py                     # all 518 requests -> output.csv at the repo root
```

- Loading and indexing the evidence (including OCR) takes about 3 minutes, then about 7 s per
  request with 4 in parallel. The full run takes about 15 minutes and costs about $7.
- Each answer is saved in `runs/<tag>/cases/`. If the run stops, the same command resumes and only
  sends the unfinished requests.
- Failed requests are retried automatically (up to 3 rounds). `output.csv` is written only when all
  518 requests have a valid answer.

Other options:

```
python code/main.py --cases eval                    # 32 samples + 10 non-deletion requests
python code/main.py --cases case_30,case_533 --tag try1
python code/main.py --model deepseek-ai/DeepSeek-V4.1-Flash --thinking low
```

Settings (environment or `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `HF_TOKEN` | required | Hugging Face token |
| `AGENT_MODEL` | `zai-org/GLM-5.3` | model id on the router |
| `AGENT_THINKING` | `low` | `low`, `high` or `none` (reasoning effort) |
| `AGENT_WORKERS` | `4` | requests in parallel |
| `AGENT_MAX_TOOL_CALLS` | `25` | tool calls per request before the agent must submit |
| `AGENT_MAX_RETRIES` | `4` | retries per model call on rate limits or server errors |
| `AGENT_MAX_ROUNDS` | `3` | retry rounds for failed requests |

## Evaluate

```
python code/evaluation/main.py runs/<tag>          # accuracy on samples + usage and cost
python code/evaluation/main.py runs/a runs/b       # compare runs, per-model and overall table
python code/evaluation/main.py output.csv          # format check of the final file
python code/evaluation/custom_cases.py             # hand-built cases, no model calls
```

Reports: [`evaluation/evaluation_report.md`](evaluation/evaluation_report.md) and
[`evaluation/usage_report.md`](evaluation/usage_report.md).

## Files

| File | Role |
|---|---|
| `main.py` | runs the agent over the requests and writes `output.csv` |
| `agent.py` | the model loop: call the model, run the tool it picks, repeat until `submit_answer` accepts |
| `tools.py` | the agent's tools and the answer checks |
| `prompts/system.md` | the agent's instructions |
| `evidence.py` | reads every evidence file into one row format |
| `identity.py` | current and past emails, phones and names for each user |
| `match.py` | links evidence rows and accounts to people |
| `rules.py` | maps each record to its data category and the policy action |
| `evaluation/` | checker, hand-built cases, reports |

Only participant-facing files under `dataset/` are used. The token is read from the environment
and never logged.
