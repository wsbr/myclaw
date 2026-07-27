"""Validators for execution trace chain: Task → Session → Runner → Span."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.span_decoder import decode_spans


@dataclass
class TraceValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def assert_ok(self) -> None:
        if not self.ok:
            raise AssertionError("Trace validation failed:\n" + "\n".join(f"  - {e}" for e in self.errors))


class TraceValidator:
    """Validate plan mode execution trace and results."""

    def validate_plan_execution(
        self,
        *,
        task: dict,
        plan: dict,
        runners: list[dict],
        spans_by_runner: dict[str, list[dict]],
        expected_step_count: int,
    ) -> TraceValidationResult:
        result = TraceValidationResult(ok=True)

        self._validate_task(task, expected_step_count, result)
        self._validate_plan(plan, expected_step_count, result)
        self._validate_runners(runners, expected_step_count, spans_by_runner, result)

        result.ok = len(result.errors) == 0
        return result

    def _validate_task(self, task: dict, expected_step_count: int, result: TraceValidationResult) -> None:
        if task.get("status") != "completed":
            result.errors.append(f"Task status expected 'completed', got '{task.get('status')}'")

        output = task.get("output_data") or task.get("output") or {}
        steps = output.get("steps", [])
        if len(steps) != expected_step_count:
            result.errors.append(f"Task output steps count: expected {expected_step_count}, got {len(steps)}")

        for step in steps:
            if step.get("status") != "completed":
                result.errors.append(f"Step {step.get('id')} status expected 'completed', got '{step.get('status')}'")
            if not step.get("output"):
                result.errors.append(f"Step {step.get('id')} missing output")

        if not output.get("summary"):
            result.errors.append("Task output missing summary")

    def _validate_plan(self, plan: dict, expected_step_count: int, result: TraceValidationResult) -> None:
        if plan.get("status") != "completed":
            result.errors.append(f"Plan status expected 'completed', got '{plan.get('status')}'")

        steps = plan.get("steps", [])
        if len(steps) != expected_step_count:
            result.errors.append(f"Plan steps count: expected {expected_step_count}, got {len(steps)}")

    def _validate_runners(
        self,
        runners: list[dict],
        expected_step_count: int,
        spans_by_runner: dict[str, list[dict]],
        result: TraceValidationResult,
    ) -> None:
        # Plan generate runner + plan parent runner + child runners per step
        plan_parent = [
            r for r in runners
            if (r.get("input_data") or r.get("input") or {}).get("type") == "plan"
        ]
        if len(plan_parent) != 1:
            result.errors.append(f"Expected 1 plan parent runner, found {len(plan_parent)}")

        child_runners = [r for r in runners if r.get("parent_runner_id")]
        if len(child_runners) != expected_step_count:
            result.errors.append(
                f"Expected {expected_step_count} child runners, found {len(child_runners)}"
            )

        generate_runners = [
            r for r in runners
            if (r.get("input_data") or r.get("input") or {}).get("mode") == "plan_generate"
        ]
        if len(generate_runners) != 1:
            result.errors.append(f"Expected 1 plan_generate runner, found {len(generate_runners)}")

        if plan_parent:
            parent_id = plan_parent[0]["id"]
            parent_spans = decode_spans(spans_by_runner.get(parent_id, []))
            plan_step_spans = [s for s in parent_spans if s.get("span_type") == "plan_step"]
            if len(plan_step_spans) != expected_step_count:
                result.errors.append(
                    f"Expected {expected_step_count} plan_step spans, found {len(plan_step_spans)}"
                )

            for span in plan_step_spans:
                if span.get("status") != "completed":
                    result.errors.append(f"plan_step span '{span.get('name')}' not completed")
                resp = span.get("response_decoded") or {}
                if not resp.get("output"):
                    result.errors.append(f"plan_step span '{span.get('name')}' missing output in response")

        for child in child_runners:
            if child.get("status") != "completed":
                result.errors.append(f"Child runner {child.get('id')} status not completed")

            child_spans = decode_spans(spans_by_runner.get(child["id"], []))
            llm_spans = [s for s in child_spans if s.get("span_type") == "llm_call"]
            if not llm_spans:
                result.errors.append(f"Child runner {child.get('id')} has no llm_call spans")
            for span in llm_spans:
                if span.get("status") != "completed":
                    result.errors.append(f"llm_call span in runner {child.get('id')} not completed")

        completed_runners = [r for r in runners if r.get("status") == "completed"]
        if len(completed_runners) < 1 + expected_step_count:
            result.warnings.append(
                f"Only {len(completed_runners)} completed runners "
                f"(expected at least {1 + expected_step_count})"
            )
