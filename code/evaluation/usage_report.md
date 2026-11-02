# Usage report — final full-dataset run

This report covers the run that produced the submitted `output.csv`: all **518** requests in
`dataset/deletion_requests.csv`, as one combined run (`runs/final`, finished 2026-10-27).

## Provider and model

| Provider | Access | Model | Settings |
|---|---|---|---|
| Hugging Face Inference Providers (router) | `https://router.huggingface.co/v1/chat/completions` (OpenAI-compatible) | `zai-org/GLM-5.3` | `reasoning_effort=low`, `temperature=0`, tool calling |

Only one model was used for the final run, so the per-model row and the overall row are the same.

## Totals

| model | cases | model calls | input tokens | output tokens | total tokens | avg tokens/request | estimated cost | cost/request |
|---|---|---|---|---|---|---|---|---|
| zai-org/GLM-5.3 | 518 | 2,226 | 4,130,842 | 223,634 | 4,354,476 | 8,406 | $6.77 | $0.0131 |
| **OVERALL** | **518** | **2,226** | **4,130,842** | **223,634** | **4,354,476** | **8,406** | **$6.77** | **$0.0131** |

| Other run facts | Value |
|---|---|
| Average model calls per request | 4.3 |
| Average tool calls per request | 9.7 (limit 25) |
| Requests that hit the tool limit | 0 |
| Requests forced to submit | 0 |
| Requests retried | 0 |
| Failed requests | 0 |
| Average time per request | 7 s (4 requests in parallel) |
| Final status mix | INCOMPLETE 360, COMPLETE 148, NOT_APPLICABLE 10 |

## How cost is estimated

Price per million tokens for `zai-org/GLM-5.3`: **$1.40 input, $4.40 output** (the highest price listed
on the Hugging Face router on 2026-10-26). The router may send a request to a cheaper provider, so
these figures are an upper bound. The actual bill is in the Hugging Face billing page.

`cost = input_tokens / 1e6 × 1.40 + output_tokens / 1e6 × 4.40`

The token counts come from the `usage` field of every model response. They are saved per request in
`runs/final/cases/<case_id>.json` and include tokens spent on any failed attempt.
Reproduce the table with:

```
python code/evaluation/main.py runs/final
```

## Development runs (not part of the final run)

These runs scored the model choice on 42 evaluation requests (32 public samples + 10 real
non-deletion requests). They are listed for transparency only.

| model | cases | model calls | input tokens | output tokens | total tokens | avg tokens/request | cost | cost/request |
|---|---|---|---|---|---|---|---|---|
| zai-org/GLM-5.3 (final settings, `glm53_low_v7`) | 42 | 147 | 264,589 | 13,327 | 277,916 | 6,617 | $0.43 | $0.0102 |
| deepseek-ai/DeepSeek-V4.1-Flash (`deepseek_low`) | 42 | 141 | 280,338 | 315,734 | 596,072 | 14,192 | $0.46 | $0.0110 |

DeepSeek is cheaper per token but used about 24× more output tokens (its reasoning is long). It
ended up costing slightly more, was about 4× slower (27 s vs 6 s per request), and was less accurate
(see `evaluation_report.md`).
