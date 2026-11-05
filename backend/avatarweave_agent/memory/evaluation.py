"""M21 independent fixture execution and conservative joint-report validation."""

import asyncio
from time import monotonic
from typing import Annotated, Literal, Protocol

from pydantic import Field

from .contracts import (
    CaseResult,
    Contract,
    EvaluationOutput,
    MetricCount,
    ModuleResult,
    VersionRef,
)


class ObservedCase(Contract):
    retrieved_refs: Annotated[list[VersionRef], Field(max_length=128)]
    unsupported_claim_count: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    answer_result: Literal[
        "answered",
        "clarified",
        "abstained",
        "timed_out",
        "rejected",
        "system_failed",
        "not_applicable",
    ]
    adjudication_ref: str | None


class CaseRunner(Protocol):
    async def run(self, case) -> ObservedCase: ...


async def evaluate(request, runner: CaseRunner):
    results, critical, review = [], 0, 0
    if len({case.case_id for case in request.cases}) != len(request.cases):
        raise ValueError("Evaluation case IDs must be unique")
    deadline = monotonic() + request.meta.execution.budget_ms / 1000
    for case in request.cases:
        start = monotonic()
        failure = None
        try:
            remaining = deadline - start
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(min(5, remaining)):
                observation = await runner.run(case)
            if not isinstance(observation, ObservedCase):
                raise TypeError("Runner must return a validated observation")
        except TimeoutError:
            failure = "deadline"
            observation = ObservedCase(
                retrieved_refs=[],
                unsupported_claim_count=0,
                tokens_used=0,
                answer_result="timed_out",
                adjudication_ref=None,
            )
        except Exception:  # noqa: BLE001 -- every offered case retains a failure result
            failure = "runner"
            observation = ObservedCase(
                retrieved_refs=[],
                unsupported_claim_count=0,
                tokens_used=0,
                answer_result="system_failed",
                adjudication_ref=None,
            )
        actual = {(r.object_id, r.version) for r in observation.retrieved_refs}
        expected = {(r.object_id, r.version) for r in case.expected_refs}
        forbidden = actual & {(r.object_id, r.version) for r in case.forbidden_refs}
        evidence_ok = expected <= actual and not forbidden and failure is None
        needs_review = observation.adjudication_ref is None and failure is None
        passed = (
            evidence_ok
            and observation.unsupported_claim_count == 0
            and not needs_review
            and observation.answer_result not in {"timed_out", "system_failed", "rejected"}
        )
        review += int(needs_review)
        critical += int(
            bool(forbidden)
            and case.scenario in {"identity", "privacy", "migration", "interruption"}
        )
        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=passed,
                retrieved_refs=observation.retrieved_refs,
                forbidden_reference_count=len(forbidden),
                unsupported_claim_count=observation.unsupported_claim_count,
                latency_ms=(monotonic() - start) * 1000,
                tokens_used=observation.tokens_used,
                failure_stage=failure
                or (
                    "independent_adjudication"
                    if needs_review
                    else None
                    if passed
                    else "evidence_or_answer"
                ),
                adjudication_ref=observation.adjudication_ref,
                metric_counts=[
                    MetricCount(
                        metric="required_evidence_coverage",
                        numerator=int(evidence_ok),
                        denominator=1,
                    )
                ],
                answer_result=observation.answer_result,
            )
        )
    return ModuleResult[EvaluationOutput](
        status="ok",
        data=EvaluationOutput(
            run_id=request.run_id,
            results=results,
            human_review_pending=review,
            critical_failures=critical,
            report_ref="evaluation:" + request.run_id,
        ),
    )


REQUIRED_METRICS = {
    "write_precision",
    "important_write_recall",
    "required_evidence_coverage",
    "temporal_update_accuracy",
    "grounded_answer_accuracy",
    "unsupported_assertion_rate",
    "correct_abstention",
    "over_abstention",
    "mandatory_compression_coverage",
    "cold_recovery_accuracy",
    "ordinary_candidate_recall",
    "accepted_compression_precision",
}


def validate_joint_report(report, capacity, load, *, raw_sample_count, dataset_question_count):
    """Never accept model/self-reported passed without all frozen prerequisites."""
    failures = []
    thresholds = {
        "write_precision": 0.98,
        "important_write_recall": 0.95,
        "ordinary_candidate_recall": 0.90,
        "required_evidence_coverage": 0.95,
        "temporal_update_accuracy": 0.95,
        "grounded_answer_accuracy": 0.95,
        "correct_abstention": 0.95,
        "mandatory_compression_coverage": 1.0,
        "accepted_compression_precision": 0.99,
        "cold_recovery_accuracy": 0.95,
    }
    maximums = {"unsupported_assertion_rate": 0.01, "over_abstention": 0.05}
    for observation in report.quality:
        n = observation.counts.denominator
        rate = observation.counts.numerator / n if n else None
        threshold = thresholds.get(observation.counts.metric)
        if rate is not None and threshold is not None and rate < threshold:
            failures.append("Quality threshold failed: " + observation.counts.metric)
        maximum = maximums.get(observation.counts.metric)
        if rate is not None and maximum is not None and rate > maximum:
            failures.append("Quality error rate exceeded: " + observation.counts.metric)
        if n < 100:
            failures.append("Quality subgroup sample is insufficient")
    if report.dataset_version != capacity.dataset_version:
        failures.append("Frozen dataset version mismatch")
    if (
        load.independent_runs < 3
        or load.warmup_minutes < 10
        or load.steady_minutes < 30
        or load.burst_minutes < 5
        or load.recovery_minutes < 10
    ):
        failures.append("Load duration or repeat count is insufficient")
    if load.active_sessions > capacity.max_active_sessions:
        failures.append("Load exceeds the capacity under review")
    if (
        report.capacity_manifest_id != capacity.manifest_id
        or report.load_profile_id != load.profile_id
    ):
        failures.append("Frozen capacity/load profile mismatch")
    if dataset_question_count < 1000 or raw_sample_count != report.outcomes.offered:
        failures.append("Held-out size or all-request accounting is insufficient")
    present = {o.counts.metric for o in report.quality if o.counts.denominator > 0}
    if not REQUIRED_METRICS <= present:
        failures.append("Missing measured quality metric")
    if len(report.run_ids) < load.independent_runs or not load.background_maintenance_enabled:
        failures.append("Repeated runs with background maintenance required")
    if report.critical_violation_count or report.detached_work_outstanding:
        failures.append("Critical violation or unreleased work blocks release")
    for observation in report.quality:
        if observation.counts.denominator == 0 or observation.interval_low is None:
            failures.append("Unevaluated quality observation")
    if any(g.state != "passed" for g in report.gates):
        failures.append("Not every gate is measured and passed")
    if report.verdict == "passed" and failures:
        raise ValueError("; ".join(failures))
    return failures
