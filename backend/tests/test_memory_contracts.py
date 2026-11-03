"""Freeze executable contracts against the six design attachments, including references."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from avatarweave_agent.memory import contracts


def test_all_design_models_have_the_same_json_schema():
    root = Path(__file__).resolve().parents[2] / "docs/voice-memory-design/contracts"
    source = "\n\n".join(
        block
        for path in sorted(root.glob("*.md"))
        for block in re.findall(r"```python\n(.*?)```", path.read_text(), re.DOTALL)
    )
    namespace = {}
    # Only repository-owned design code, never request/provider content.
    exec(compile(source, "<memory-design-contracts>", "exec"), namespace)  # noqa: S102
    base = namespace["Contract"]
    names = [
        name
        for name, model in namespace.items()
        if isinstance(model, type) and issubclass(model, base)
    ]
    assert len(names) == 166
    for name in names:
        model = namespace[name]
        model.model_rebuild(_types_namespace=namespace)
        assert getattr(contracts, name).model_json_schema() == model.model_json_schema(), name


@pytest.mark.parametrize(
    "payload",
    [
        '{"workspace_id":"a","user_id":1,"companion_id":"c"}',
        '{"workspace_id":"a","user_id":"u","companion_id":"c","role":"admin"}',
    ],
)
def test_scope_types_cannot_coerce_or_expand(payload):
    with pytest.raises(ValidationError):
        contracts.Scope.model_validate_json(payload)


def test_unknown_version_and_unbounded_runtime_are_rejected():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    value = {
        "operation_id": "op",
        "trace_id": "trace",
        "now": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=1)).isoformat(),
        "algorithm_version": "v1",
        "schema_version": 999,
    }
    with pytest.raises(ValidationError):
        contracts.RequestMeta.model_validate_json(__import__("json").dumps(value))
