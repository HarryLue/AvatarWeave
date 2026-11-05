"""Score independent observations or validate a joint report; never invent unmeasured gates."""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from avatarweave_agent.memory.contracts import (
    CapacityManifest,
    EvaluationInput,
    JointAcceptanceReport,
    LoadProfile,
)
from avatarweave_agent.memory.evaluation import (
    ObservedCase,
    evaluate,
    validate_joint_report,
)
from avatarweave_agent.memory.utils import private_write


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    cases = modes.add_parser("cases")
    cases.add_argument("--request", type=Path, required=True)
    cases.add_argument(
        "--observations",
        type=Path,
        required=True,
        help="JSON with dataset_sha256 and cases: [{case_id, observation: ObservedCase}] from independent adjudication",
    )
    cases.add_argument("--output", type=Path, required=True)
    joint = modes.add_parser("joint")
    joint.add_argument("--report", type=Path, required=True)
    joint.add_argument("--capacity", type=Path, required=True)
    joint.add_argument("--load", type=Path, required=True)
    joint.add_argument("--raw-request-count", type=int, required=True)
    joint.add_argument("--dataset-question-count", type=int, required=True)
    args = parser.parse_args()
    if args.mode == "joint":
        report = JointAcceptanceReport.model_validate_json(args.report.read_bytes())
        failures = validate_joint_report(
            report,
            CapacityManifest.model_validate_json(args.capacity.read_bytes()),
            LoadProfile.model_validate_json(args.load.read_bytes()),
            raw_sample_count=args.raw_request_count,
            dataset_question_count=args.dataset_question_count,
        )
        print(
            json.dumps(
                {
                    "verdict": report.verdict if not failures else "incomplete",
                    "failures": failures,
                }
            )
        )
        if failures or report.verdict != "passed":
            raise SystemExit(1)
        return
    request = EvaluationInput.model_validate_json(args.request.read_bytes())
    raw = json.loads(args.observations.read_bytes())
    digest = hashlib.sha256(
        json.dumps(
            [case.model_dump(mode="json") for case in request.cases],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    if raw.get("dataset_sha256") != digest:
        parser.error("Observations do not match the frozen evaluation case digest")
    rows = raw.get("cases", [])
    observations = {
        row["case_id"]: ObservedCase.model_validate_json(json.dumps(row["observation"]))
        for row in rows
    }
    if len(rows) != len(observations) or not observations.keys() <= {
        case.case_id for case in request.cases
    }:
        parser.error("Duplicate or unknown observation case ID")

    class IndependentRunner:
        async def run(self, case):
            return observations[case.case_id]

    result = await evaluate(request, IndependentRunner())
    private_write(args.output, result.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {
                "cases": len(result.data.results),
                "passed": sum(row.passed for row in result.data.results),
                "human_review_pending": result.data.human_review_pending,
                "critical_failures": result.data.critical_failures,
                "joint_acceptance_claimed": False,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
