"""Replayable event ledger for the abort-signal coverage audit.

Every unit of work under audit appends structured events to a thread-safe
ledger. The same ledger is serialized to JSON (see run_audit.py) so that a run
can be replayed/diffed without touching the event loop again.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventLedger:
    case_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, kind: str, work: str, **detail: Any) -> None:
        with self._lock:
            event = {"seq": len(self.events), "kind": kind, "work": work}
            event.update(detail)
            self.events.append(event)

    def kinds_for(self, work: str | None = None) -> Counter[str]:
        counter: Counter[str] = Counter()
        for event in self.events:
            if work is None or event["work"] == work:
                counter[event["kind"]] += 1
        return counter

    def kinds(self) -> Counter[str]:
        return self.kinds_for()

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "events": list(self.events)}
