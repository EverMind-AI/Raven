"""One bounded model exchange that may query and must end in exactly one structured submission."""

import asyncio
import json

from ..curator.generation.context.render import assistant_message


class ExchangeError(RuntimeError):
    def __init__(self, message, trace=()):
        super().__init__(message)
        self.trace = tuple(trace)


def messages(system: str, materials: dict) -> list[dict]:
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": json.dumps(materials, ensure_ascii=False, indent=2)},
    ]


async def exchange(
    provider, messages, tools, *, submit, query=None, model=None, max_calls=4, timeout=120, trace=None, label=""
):
    """Drive the model until one call in `submit` parses; `query` tools run locally and feed results back.

    The last allowed call offers only the submission tools, so a query-happy model still ends with a submission."""
    query = query or {}
    trace = trace if trace is not None else []
    submissions = [entry for entry in tools if entry["function"]["name"] in submit]
    for attempt in range(1, max_calls + 1):
        offered = tools
        if attempt == max_calls and query:
            offered = submissions
            messages.append(
                {"role": "user", "content": "This is the last call: submit now; no further queries are possible."}
            )
        trace.append({"label": label, "event": "model.call", "call": attempt})
        try:
            async with asyncio.timeout(timeout):
                response = await provider.chat_with_retry(
                    messages=messages, tools=offered, model=model, tool_choice="auto"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ExchangeError(f"{label}: model call failed: {exc}", trace) from exc
        if response.finish_reason == "error":
            raise ExchangeError(f"{label}: provider error: {response.content}", trace)
        messages.append(assistant_message(response))
        if not response.tool_calls:
            names = ", ".join(f"`{name}`" for name in (*query, *submit))
            messages.append(
                {"role": "user", "content": f"Plain text is not delivered. Reply by calling one of the tools: {names}."}
            )
            trace.append({"label": label, "event": "output.missing"})
            continue
        submitted = [call for call in response.tool_calls if call.name in submit]
        if submitted:
            if len(response.tool_calls) != 1:
                error = "Submit exactly once, separately from queries."
                for call in response.tool_calls:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"error": error})})
                trace.append({"label": label, "event": "output.rejected", "error": error})
                continue
            call = submitted[0]
            try:
                value = submit[call.name](call.arguments)
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)[:6000]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"error": error})})
                trace.append({"label": label, "event": "output.rejected", "error": error})
                continue
            trace.append({"label": label, "event": call.name})
            return call.name, value
        for call in response.tool_calls:
            try:
                result = (
                    query[call.name](call.arguments) if call.name in query else {"error": f"unknown tool: {call.name}"}
                )
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                result = {"error": str(exc)[:4000]}
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
            )
            trace.append({"label": label, "event": "query", "tool": call.name, "arguments": call.arguments})
    raise ExchangeError(f"{label}: submission budget exhausted", trace)
