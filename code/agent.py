"""The agent loop: the model picks a tool, we run it, the result goes back, until it submits an answer."""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools import TOOL_SCHEMAS, TOOLS, submit_answer

API_URL = "https://router.huggingface.co/v1/chat/completions"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "25"))
MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "4"))


FORCE_SUBMIT_TURNS = 3  # extra turns after the budget where only submit_answer is allowed


class CaseError(RuntimeError):
    """A case that could not finish; carries the tokens already spent so usage stays honest."""

    def __init__(self, msg, usage):
        super().__init__(msg)
        self.usage = usage


def call_model(messages, model, thinking, tool_choice="auto"):
    body = {"model": model, "messages": messages, "tools": TOOL_SCHEMAS, "tool_choice": tool_choice,
            "temperature": 0}
    if thinking:
        body["reasoning_effort"] = thinking
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {os.environ['HF_TOKEN']}", "Content-Type": "application/json"})
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # rate limits and server hiccups are worth retrying; anything else is our bug
            if e.code not in (429, 500, 502, 503, 504) or attempt == MAX_RETRIES:
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"network error: {e}") from e
        time.sleep(2 ** attempt * 3)


def run_case(data, case, model, thinking):
    """Returns {"case_id", "status", "steps", "usage": [...], "tool_calls", "error"}."""
    user = "Audit this deletion request:\n" + json.dumps(
        {k: case[k] for k in ("case_id", "email", "requested_at", "reported_status", "reported_at",
                              "audit_at", "request_text")}, ensure_ascii=False, indent=1)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    usage, calls, forced = [], 0, 0
    submit_only = {"type": "function", "function": {"name": "submit_answer"}}
    while forced < FORCE_SUBMIT_TURNS:
        choice = "auto"
        if calls >= MAX_TOOL_CALLS:
            # budget used up: the agent must answer with what it has learned so far
            forced += 1
            choice = submit_only
            messages.append({"role": "user", "content": "Tool budget used up. Call submit_answer now with your "
                                                        "best answer from the facts you already have."})
        try:
            resp = call_model(messages, model, thinking, choice)
        except RuntimeError as e:
            raise CaseError(str(e), usage) from e
        u = resp.get("usage") or {}
        usage.append({"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)})
        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
                        if tool_calls else {"role": "assistant", "content": msg.get("content") or ""})
        if not tool_calls:
            messages.append({"role": "user", "content": "Use the tools; finish by calling submit_answer."})
            calls += 1
            continue
        for tc in tool_calls:
            calls += 1
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            if args is None:
                result = "Arguments were not valid JSON. Try again."
            elif name == "submit_answer":
                try:
                    errors, steps = submit_answer(data, case, args.get("deletion_status"), args.get("steps", []))
                except Exception as e:  # malformed steps (e.g. not objects) must not crash the case
                    errors, steps = [f"could not read steps: {e}"], None
                if not errors:
                    return {"case_id": case["case_id"], "status": args["deletion_status"], "steps": steps,
                            "usage": usage, "tool_calls": calls, "error": "", "forced_submit": forced > 0}
                result = "Answer rejected:\n" + "\n".join(errors)
            elif name in TOOLS:
                try:
                    result = TOOLS[name](data, case, **args)
                except Exception as e:  # a tool bug becomes a message the agent can work around
                    result = f"Tool error: {type(e).__name__}: {e}"
            else:
                result = f"Unknown tool {name}."
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    raise CaseError(f"no valid answer within {MAX_TOOL_CALLS} tool calls + {FORCE_SUBMIT_TURNS} forced turns",
                    usage)
