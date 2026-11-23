"""Check an LLM endpoint before trusting it with a booking.

Sends the agent's real system prompt and a short conversation to the model in
.env, several times, and reports how many replies were usable json.

    python tools\\probe_llm.py                     # 10 calls, as the agent asks
    python tools\\probe_llm.py --runs 20
    python tools\\probe_llm.py json_object text    # compare request styles

Modes:
  json_turn    response_format=json_object plus the per-turn json instruction
               (what the agent does)
  json_object  response_format=json_object alone
  text         no response_format; the json is parsed out of the text

It defaults to facts.test.yaml, so nothing personal is sent to the provider.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from agent.config import ConfigError, load_facts, load_settings  # noqa: E402
from agent.llm import _extract_json, _validate  # noqa: E402
from agent.prompts import TURN_NUDGE, build_system_prompt  # noqa: E402

MODES = ("json_turn", "json_object", "text")

# A turn worth testing: the manager has just moved the goalposts, so a good
# reply has to set requested_change, change_value and needs_operator.
HISTORY = [
    {"role": "user", "content": "Good evening, this is The Ivy, Covent Garden."},
    {
        "role": "assistant",
        "content": "Could you reserve a table for two at 9pm under the name Test Guest?",
    },
    {"role": "user", "content": "I'm afraid 9pm is fully booked. We could do 9.30pm instead?"},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modes", nargs="*", default=None, help=f"one or more of: {', '.join(MODES)}")
    parser.add_argument("--runs", type=int, default=10, help="calls per mode (default 10)")
    parser.add_argument("--facts", default=str(ROOT / "facts.test.yaml"), help="facts file for the prompt")
    args = parser.parse_args()

    modes = args.modes or ["json_turn"]
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        print(f"Unknown mode(s): {', '.join(unknown)}. Choose from {', '.join(MODES)}.")
        return 1

    try:
        settings = load_settings()
        facts = load_facts(Path(args.facts))
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    system_prompt = build_system_prompt(facts, "negotiating", "The Ivy, Covent Garden", "evening")
    messages = [{"role": "system", "content": system_prompt}] + HISTORY
    client = OpenAI(
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60.0, max_retries=0
    )

    print(f"model: {settings.llm_model} at {settings.llm_base_url}")
    print(f"prompt: {len(system_prompt)} chars, contains 'json': {'json' in system_prompt}")

    results = {}
    for mode in modes:
        ok = empty = invalid = errors = 0
        elapsed: list[float] = []
        for run in range(args.runs):
            payload = list(messages)
            extra: dict = {}
            if mode != "text":
                extra["response_format"] = {"type": "json_object"}
            if mode == "json_turn":
                payload.append({"role": "user", "content": TURN_NUDGE})

            started = time.monotonic()
            try:
                response = client.chat.completions.create(
                    model=settings.llm_model,
                    messages=payload,
                    temperature=0.3,
                    max_tokens=2000,
                    **extra,
                )
                raw = response.choices[0].message.content or ""
            except Exception as exc:
                errors += 1
                print(f"  {mode} {run + 1}: error {str(exc)[:120]}")
                continue

            elapsed.append(time.monotonic() - started)
            if not raw.strip():
                empty += 1
                print(f"  {mode} {run + 1}: EMPTY ({len(raw)} whitespace chars)")
                continue
            decision, problem = _validate(_extract_json(raw))
            if decision is None:
                invalid += 1
                print(f"  {mode} {run + 1}: INVALID ({problem}) {raw[:70]!r}")
            else:
                ok += 1
        results[mode] = (ok, empty, invalid, errors, elapsed)

    print("\nmode           ok  empty  invalid  errors  median s")
    for mode, (ok, empty, invalid, errors, elapsed) in results.items():
        median = sorted(elapsed)[len(elapsed) // 2] if elapsed else float("nan")
        print(f"{mode:<13} {ok:>3}  {empty:>5}  {invalid:>7}  {errors:>6}  {median:>8.1f}")

    worst = min(ok for ok, *_ in results.values())
    if worst < args.runs:
        print("\nAnything below a clean sweep is worth a second look before a real booking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
