"""G7 - spec conformance: do the spec's own examples hold for the code?

Deterministic and LLM-free, like every gate. The spec carries concrete
examples (``clamp(5, 0, 10)`` -> ``5``, ``clamp(1, 5, 0)`` raises
``ValueError``); this gate executes them against the implementation.

It earns its place from a measured failure: in the Phase 2 benchmark a spec
listed ``no_lower`` for the password ``'short'``, which plainly has lowercase
letters. The Test-Author faithfully tested the wrong example and a correct
implementation was rejected. Executing the examples exposes that kind of
disagreement in seconds, with no model, and says *which example* disagrees.

A mismatch does not say who is wrong - the code or the example. That is the
Diagnostician's question, answered against the spec's rule text.
"""

from __future__ import annotations

import json

from ..config import WORK_DIR_NAME
from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

SPEC_FILE = "spec.json"
_CHECKER = "spec_check.py"
_RESULT = "spec_check.json"

#: Runs inside the sandbox. It writes its verdict to a file rather than
#: stdout, because the code under test may print during import and would
#: corrupt a stdout protocol.
_CHECKER_SOURCE = '''\
import importlib
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
work = root / ".codecrew"
spec = json.loads((work / "spec.json").read_text(encoding="utf-8"))
out = {"module_error": None, "examples": []}

try:
    module = importlib.import_module(spec["module"])
except BaseException as exc:
    out["module_error"] = f"{type(exc).__name__}: {exc}"
else:
    namespace = dict(vars(module))
    for function in spec["functions"]:
        name = function["signature"].split("(")[0].replace("def ", "").strip()
        for example in function.get("examples", []):
            record = {
                "function": name,
                "call": example["call"],
                "expected": example.get("result"),
                "raises": example.get("raises"),
                "ok": False,
                "actual": "",
            }
            try:
                value = eval(example["call"], dict(namespace))
            except BaseException as exc:
                names = [c.__name__ for c in type(exc).__mro__]
                record["actual"] = f"raised {type(exc).__name__}: {exc}"
                record["ok"] = bool(example.get("raises")) and \\
                    example["raises"] in names
            else:
                record["actual"] = f"returned {value!r}"
                if example.get("raises") is None:
                    try:
                        record["ok"] = bool(
                            value == eval(example["result"], dict(namespace)))
                    except BaseException as exc:
                        record["actual"] += f" (could not compare: {exc})"
            out["examples"].append(record)

(work / "spec_check.json").write_text(json.dumps(out), encoding="utf-8")
'''


class ConformanceGate(Gate):
    gate_id = "G7"
    name = "Spec conformance"
    blocking = True
    depends_on = ("G1",)

    def applicable(self, ctx: GateContext) -> bool:
        """Only meaningful when there is an approved spec to conform to.

        Single-agent runs have none. That is "not applicable", not "skipped":
        a skip is reported as unverified, and a gate that cannot apply has
        not failed to verify anything.
        """
        return (ctx.workspace / WORK_DIR_NAME / SPEC_FILE).exists()

    def check(self, ctx: GateContext) -> GateResult:
        work = ctx.workspace / WORK_DIR_NAME
        (work / _CHECKER).write_text(_CHECKER_SOURCE, encoding="utf-8")
        result_file = work / _RESULT
        if result_file.exists():
            result_file.unlink()

        run = ctx.sandbox.run(
            ["python", f"{WORK_DIR_NAME}/{_CHECKER}"],
            timeout=ctx.config.thresholds.command_timeout_s,
        )
        evidence = self.save_evidence(ctx, run)
        if not result_file.exists():
            return self._result(
                ctx, Status.ERROR, "spec checker produced no result",
                evidence=evidence, note=run.tail(600),
            )
        data = json.loads(result_file.read_text(encoding="utf-8"))

        if data.get("module_error"):
            return self._result(
                ctx, Status.FAIL, "the specified module cannot be imported",
                findings=[Finding(
                    severity=Severity.CRITICAL, code="spec-module-import",
                    message=data["module_error"],
                )],
                evidence=evidence,
            )

        examples = data.get("examples", [])
        if not examples:
            return self._result(
                ctx, Status.SKIP, "not run", evidence=evidence,
                note="the spec contains no examples to execute",
            )

        bad = [e for e in examples if not e["ok"]]
        score = 100.0 * (len(examples) - len(bad)) / len(examples)
        findings = [
            Finding(
                severity=Severity.HIGH,
                code="spec-example-mismatch",
                message=(
                    f"{e['call']}: the spec expects "
                    + (f"it to raise {e['raises']}" if e.get("raises")
                       else f"{e['expected']}")
                    + f", the implementation {e['actual']}"
                ),
            )
            for e in bad
        ]
        summary = f"{len(examples) - len(bad)}/{len(examples)} spec examples hold"
        if bad:
            return self._result(
                ctx, Status.FAIL, summary, score=score, findings=findings,
                evidence=evidence,
                note="either the code or the example is wrong; the spec's "
                     "rule text decides which",
            )
        return self._result(ctx, Status.PASS, summary, score=score,
                            evidence=evidence)
