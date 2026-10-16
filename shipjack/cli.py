"""Command line: the scripted bench (no model) and the real-model bench."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import llm
from .scenarios import BY_NAME, SCENARIOS
from .wallet import PROFILES


def _write(report: dict, md: str, json_out: str | None, markdown_out: str | None) -> None:
    if json_out:
        Path(json_out).write_text(json.dumps(report, indent=1) + "\n")
    if markdown_out:
        Path(markdown_out).write_text(md)


def bench(args) -> int:
    report = llm.scripted_report()
    md = llm.to_markdown(report, title="Scripted results (a fully fooled agent)")
    print(md)
    _write(report, md, args.json, args.markdown)
    return 0


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _step(call) -> str:
    a = call.arguments
    if call.tool == "create_checkout" or call.tool == "update_checkout":
        items = ", ".join(f"{i.get('quantity', 1)}x {i.get('id')}" for i in a.get("items") or [])
        where = a.get("ship_to") or {}
        dest = f"ship to {where.get('line_one', '?')}, {where.get('city', '?')}" if where else f"email {a.get('email')}"
        return f"{call.tool} {a.get('store')}: {items or 'no item change'}; {dest}"
    if call.tool == "pay":
        return f"pay {a.get('store')} {a.get('session_id')}"
    return f"{call.tool} " + " ".join(str(v) for v in a.values())


def demo(args) -> int:
    scenario = BY_NAME[args.scenario]
    if args.model == "scripted":
        api = llm.ScriptedModel(scenario.fooled)
    else:
        url, env = llm.PROVIDERS[args.provider]
        key = os.environ.get(env, "")
        if not key:
            print(f"shipjack demo: set {env} for a real model, or use --model scripted", file=sys.stderr)
            return 2
        api = llm.ChatAPI(url, key, args.model, temperature=args.temperature)
    print(f"Scenario: {scenario.name} ({scenario.kind})" + (f", the page wants to {scenario.goal}" if
                                                             scenario.kind == "attack" else ""))
    print(f"Task: {scenario.task}\nModel: {api.model}\n")
    runs = llm.run_paired(api, scenario)
    first = next(iter(runs.values()))
    shared = first.calls[:first.fork] if first.fork >= 0 else first.calls
    print("The agent, before paying:")
    for i, call in enumerate(shared, 1):
        print(f"  {i}. {_step(call)}")
    print()
    for profile, run in runs.items():
        verdict = llm.judge(scenario, run)
        print(f"{llm.PROFILE_TITLES[profile]}:")
        for call in run.calls[len(shared):]:
            line = f"  - {_step(call)}"
            if call.tool == "pay":
                result = json.loads(call.result)
                line += (f" -> paid {_money(result['amount'])} to {result['merchant']}" if call.paid else
                         f" -> declined by the {call.stage}: {result.get('message', '')[:160]}")
            print(line)
        for order in run.facts["orders"]:
            dest = order["address"] or {}
            where = f"ships to {dest.get('line_one')}, {dest.get('city')}" if dest else f"emailed to {order['email']}"
            print(f"  order at {order['merchant']}: {_money(order['amount'])}, {where}")
        print(f"  outcome: {verdict['outcome']}; the user's task done: {'yes' if verdict['done'] else 'no'}")
        if run.final:
            print("  agent's answer: " + " ".join(run.final.split())[:300])
        print()
    return 0


def llm_bench(args) -> int:
    if args.rescore:  # no model calls, no key: score a saved report with the current rules
        report = llm.rejudge(json.loads(Path(args.rescore).read_text()))
        md = llm.to_markdown(report)
        print(md)
        _write(report, md, args.json, args.markdown)
        return 0
    url, env = llm.PROVIDERS.get(args.provider, (None, None))
    url, env = args.base_url or url, args.key_env or env
    key = os.environ.get(env or "")
    if not url or not key:
        print(f"shipjack llm-bench: set {env} (the API key is read from the environment only)", file=sys.stderr)
        return 2
    if args.list_models:
        print("\n".join(llm.ChatAPI(url, key, "-").models()))
        return 0
    if not args.models:
        print("shipjack llm-bench: pass --models", file=sys.stderr)
        return 2
    chosen = [s for s in SCENARIOS if not args.scenarios or s.name in args.scenarios]
    previous = llm.rejudge(json.loads(Path(args.resume).read_text())) if args.resume else None
    meta = dict(provider=args.provider, base_url=url, temperature=args.temperature, date=time.strftime("%Y-%m-%d"))

    def save(report: dict) -> None:
        if args.json:
            tmp = Path(args.json).with_suffix(".tmp")
            tmp.write_text(json.dumps({**report, **meta}, indent=1) + "\n")
            tmp.replace(args.json)

    report = llm.run_matrix(lambda m: llm.ChatAPI(url, key, m, temperature=args.temperature), args.models,
                            trials=args.trials, profiles=tuple(args.profiles), scenarios=chosen, previous=previous,
                            on_update=save, log=lambda m: print(m, file=sys.stderr, flush=True),
                            follow_up=args.follow_up)
    report.update(meta)
    md = llm.to_markdown(report)
    print(md)
    _write(report, md, args.json, args.markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shipjack")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench", help="every scenario with a scripted, fully fooled agent (no model, no key)")
    b.add_argument("--json")
    b.add_argument("--markdown")
    b.set_defaults(func=bench)
    d = sub.add_parser("demo", help="one scenario, told step by step, under every payment profile")
    d.add_argument("scenario", choices=[s.name for s in SCENARIOS])
    d.add_argument("--model", default="scripted", help="'scripted' (a fully fooled agent, no key) or a model id")
    d.add_argument("--provider", default="groq", choices=sorted(llm.PROVIDERS))
    d.add_argument("--temperature", type=float, default=0.6)
    d.set_defaults(func=demo)
    m = sub.add_parser("llm-bench", help="real models in the shopping agent's seat")
    m.add_argument("--models", nargs="*", default=[])
    m.add_argument("--trials", type=int, default=5)
    m.add_argument("--provider", default="groq", choices=sorted(llm.PROVIDERS))
    m.add_argument("--base-url")
    m.add_argument("--key-env", help="environment variable that holds the API key")
    m.add_argument("--temperature", type=float, default=0.6)
    m.add_argument("--profiles", nargs="*", default=list(PROFILES), choices=PROFILES)
    m.add_argument("--scenarios", nargs="*", choices=[s.name for s in SCENARIOS])
    m.add_argument("--json")
    m.add_argument("--markdown")
    m.add_argument("--resume", help="an earlier --json report: keep its finished trials")
    m.add_argument("--rescore", help="score a saved --json report again, with no model calls")
    m.add_argument("--list-models", action="store_true")
    m.add_argument("--follow-up", help="a trusting user's one reply when the model stops without paying, "
                                       "for example 'OK, go ahead.'")
    m.set_defaults(func=llm_bench)
    from .load import add_parser as add_load
    add_load(sub)
    from .retrieval_eval import add_parser as add_retrieval
    add_retrieval(sub)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
