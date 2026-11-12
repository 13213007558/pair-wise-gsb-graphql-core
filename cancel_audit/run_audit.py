"""Run the abort-signal coverage audit and emit a replayable event ledger.

Usage (from the repository root):

    .venv/bin/python -m cancel_audit.run_audit

Outputs:
* a summary table on stdout;
* cancel_audit/ledger.json - the full, ordered event ledger for every case.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from .audit_cases import run_all

KIND_ORDER = ["coroutine", "shared_future", "sync", "subscription"]
EVENT_COLUMNS = [
    "started",
    "cancel_notice",
    "signal_observed",
    "finally",
    "aclose",
    "delivered",
    "completed",
]


def render_table(results) -> str:
    header = (
        f"{'case':<38} {'outcome':<14} "
        + " ".join(f"{col:>13}" for col in EVENT_COLUMNS)
        + f" {'suspended':>9}  orphans"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        counts = result.counts
        suspended = ""
        suspended_events = [
            event
            for event in result.events
            if event["kind"] == "post_gc_source_still_suspended"
        ]
        if suspended_events:
            suspended = "yes" if suspended_events[-1].get("suspended") else "no"
        row = (
            f"{result.case_id:<38} {result.outcome:<14} "
            + " ".join(f"{counts.get(col, 0):>13}" for col in EVENT_COLUMNS)
            + f" {suspended:>9}  {len(result.orphan_tasks)}"
        )
        lines.append(row)
    return "\n".join(lines)


def render_findings(results) -> list[str]:
    findings: list[str] = []
    for result in results:
        if result.orphan_tasks:
            findings.append(
                f"ORPHAN TASKS in {result.case_id}: {result.orphan_tasks}"
            )
        state = result.state
        if state and state.get("shared_future_cancelled"):
            adapter = state.get("adapter")
            if adapter == "returns_future_directly":
                findings.append(
                    f"SHARED FUTURE CANCELLED in {result.case_id}: returning a"
                    " caller-owned Future lets the library cancel that exact object."
                )
            elif adapter == "awaits_future":
                findings.append(
                    f"SHARED FUTURE CANCELLED in {result.case_id}: a plain await"
                    " propagates Task cancellation into the business-owned Future."
                    " Use asyncio.shield to keep ownership on the business side."
                )
        pre_gc = next(
            (
                event
                for event in result.events
                if event["kind"] == "pre_gc_source_suspended"
            ),
            None,
        )
        post_gc = next(
            (
                event
                for event in result.events
                if event["kind"] == "post_gc_source_still_suspended"
            ),
            None,
        )
        if pre_gc and pre_gc.get("suspended"):
            if post_gc and post_gc.get("suspended"):
                findings.append(
                    f"SOURCE LEAKED in {result.case_id}: an idle abort with no"
                    " aclose() left the source generator suspended even after GC;"
                    " its finally never ran. Consumer must aclose() the stream."
                )
            else:
                findings.append(
                    f"DEFERRED CLEANUP in {result.case_id}: an idle abort does not"
                    " close the source (it is suspended right after the consumer"
                    " drops it); reclamation only happens later via async-gen GC."
                    " Consumers must call aclose() for deterministic cleanup."
                )
    return findings


async def main() -> None:
    results = await run_all()
    output_path = pathlib.Path(__file__).parent / "ledger.json"
    payload = {
        "schema_version": 1,
        "cases": [
            {
                "case_id": r.case_id,
                "title": r.title,
                "kind": r.kind,
                "timing": r.timing,
                "outcome": r.outcome,
                "outcome_detail": r.outcome_detail,
                "counts": r.counts,
                "state": r.state,
                "orphan_tasks": r.orphan_tasks,
                "events": r.events,
            }
            for r in results
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    print(render_table(results))
    print()
    findings = render_findings(results)
    if findings:
        print("Findings:")
        for finding in findings:
            print(" -", finding)
    else:
        print("Findings: no library-owned task leftovers in any case.")
    print(f"\nReplayable ledger written to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
