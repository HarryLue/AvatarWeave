"""Bounded chat-compatible model gateway; task/schema pairing is server-owned."""

import asyncio
import json
from time import monotonic

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (
    AdmissionInput,
    AdmissionOutput,
    CompressionOutput,
    DialogueDraft,
    ModelTaskInput,
    ModelTaskOutput,
    PersonaOutput,
    QueryOutput,
    ReflectionOutput,
)
from .execution import MemoryFault
from .lifecycle import PREDICATES, enclosing_clause

TASKS = {
    "extract": ("admission.v1", AdmissionOutput),
    "query": ("query.v1", QueryOutput),
    "compress": ("compression.v1", CompressionOutput),
    "reflect": ("reflection.v1", ReflectionOutput),
    "tone": ("persona.v1", PersonaOutput),
    "dialogue": ("dialogue.text.v1", DialogueDraft),
}


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class ProviderMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    content: str | None


class ProviderChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    message: ProviderMessage
    finish_reason: str | None = None


class ProviderCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    choices: list[ProviderChoice] = Field(min_length=1, max_length=4)
    usage: ProviderUsage


class ModelGateway:
    def __init__(self, *, base_url, api_key, model, max_inflight=2, client=None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), follow_redirects=False
        )
        self.inflight = 0
        self.max_inflight = max_inflight

    @classmethod
    def from_pipeline(cls):
        from ..pipeline import load_pipeline

        config = load_pipeline()
        if config.llm is None or config.llm.provider != "openai":
            return None
        options = config.llm.options
        if not options.get("api_key"):
            return None
        return cls(
            base_url=options.get("base_url", "https://api.openai.com/v1"),
            api_key=options["api_key"],
            model=options["model"],
        )

    async def close(self):
        await self.client.aclose()

    async def task(self, request: ModelTaskInput, *, source_events=None) -> ModelTaskOutput:
        expected_schema, model = TASKS[request.task]
        if request.output_schema != expected_schema:
            raise MemoryFault("invalid_model_output", "Task/output schema pairing is invalid")
        if request.meta.execution.lane == "dialogue" and request.task != "dialogue":
            raise MemoryFault(
                "unsupported_capability", "Normal dialogue forbids extra model generation calls"
            )
        if self.inflight >= self.max_inflight:
            raise MemoryFault("overloaded", "Model resource is busy", retryable=True)
        if sum(len(m.content.encode()) for m in request.messages) > 128000:
            raise MemoryFault("budget_exceeded", "Model input exceeds the bounded payload")
        self.inflight += 1
        start = monotonic()
        try:
            async with asyncio.timeout(request.meta.execution.budget_ms / 1000):
                payload = {
                    "model": self.model,
                    "stream": False,
                    "messages": [m.model_dump() for m in request.messages]
                    + [
                        {
                            "role": "system",
                            "content": "Return exactly one JSON object matching this schema. Do not use code fences.\n"
                            + json.dumps(model.model_json_schema(), ensure_ascii=False),
                        }
                    ],
                    "max_tokens": request.max_output_tokens,
                    "response_format": {"type": "json_object"},
                }
                # Incremental response-size bound also covers chunked providers.
                async with self.client.stream(
                    "POST",
                    self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._api_key},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 256000:
                            raise MemoryFault(
                                "budget_exceeded", "Model response exceeds the payload budget"
                            )
                        body.extend(chunk)
                completion = ProviderCompletion.model_validate_json(body)
                if completion.choices[0].finish_reason not in {"stop", "end_turn"}:
                    raise MemoryFault("invalid_model_output", "Model output is incomplete")
                content = completion.choices[0].message.content
                if content is None:
                    raise MemoryFault(
                        "invalid_model_output", "Model response has no structured content"
                    )
                if request.task == "extract" and source_events is not None:
                    content = align_evidence(content, source_events)
                value = model.model_validate_json(content)
                return ModelTaskOutput(
                    task=request.task,
                    profile_id=request.profile_id,
                    payload=value,
                    input_tokens=completion.usage.prompt_tokens,
                    output_tokens=completion.usage.completion_tokens,
                    provider_request_ref=completion.id,
                    latency_ms=(monotonic() - start) * 1000,
                )
        except json.JSONDecodeError:
            raise MemoryFault("invalid_model_output", "Model returned invalid JSON") from None
        except ValidationError as error:
            # Types/counts only: validation inputs and messages can contain private text.
            schema = model.model_json_schema()
            names = set(schema.get("properties", {}))
            for definition in schema.get("$defs", {}).values():
                names.update(definition.get("properties", {}))
            kinds = ",".join(
                ".".join(
                    str(part) if isinstance(part, int) or part in names else "field"
                    for part in item["loc"]
                )
                + ":"
                + item["type"]
                for item in error.errors(include_input=False)[:4]
            )
            raise MemoryFault(
                "invalid_model_output",
                "Model output does not match its task schema: " + kinds[:200],
            ) from None
        except httpx.HTTPError:
            raise MemoryFault(
                "provider_unavailable", "Model response is unavailable or invalid", retryable=True
            ) from None
        except TimeoutError:
            raise MemoryFault(
                "deadline_exceeded", "Model deadline exceeded", retryable=True
            ) from None
        finally:
            self.inflight -= 1

    async def extract(self, request: AdmissionInput):
        from .contracts import ModelMessage

        allowed = ", ".join(PREDICATES)
        prompt = (
            "从用户最终原话提取可独立更新的真实生活事实，不把寒暄、假设、玩笑、引述别人当确定事实。"
            "每个候选必须引用本批来源的真实 Unicode 字符区间；原话和标点逐字匹配。"
            "quote 选取简短完整的逐字原文；服务端仅在片段唯一时对齐 Unicode 位置，不接受改写片段。"
            "引用须保留主语、否定、条件与时间，不要只摘出字段值。"
            "subject_id 对用户本人使用 " + request.scope.user_id + "；对第三人仅填原话中的姓名或称呼字符串，"
            "由服务端匹配已确认的稳定实体；不得编造第三人 ID，不把第三人的经历归给用户。"
            "只使用谓词：" + allowed + "。namespace=life；assertion_kind=user_report。"
            "canonical_value 必须是原话中的子串，display_text 简短忠实；明确否定保留 negative。"
            "不推断人格或诊断。日期仅以 original_expression 保留用户原始表达，start/end 置 null，precision=unknown。"
            "不产生已提交、已播放或已索引的声明。decisions 可以为空，准入由服务端重新判断。"
            f"最多 {request.max_candidates} 条候选。input_watermark=0。"
        )
        serialized = json.dumps(
            [e.model_dump(mode="json") for e in request.events], ensure_ascii=False
        )
        chunks = [serialized[index : index + 14000] for index in range(0, len(serialized), 14000)]
        result = await self.task(
            ModelTaskInput(
                meta=request.meta,
                scope=request.scope,
                fence=request.fence,
                task="extract",
                profile_id="memory-extract.v1",
                prompt_version=request.extraction_prompt_version,
                output_schema="admission.v1",
                messages=[ModelMessage(role="system", content=prompt)]
                + [ModelMessage(role="user", content=part) for part in chunks],
                max_output_tokens=6000,
            ),
            source_events=request.events,
        )
        return result.payload


def align_evidence(content, events):
    """Decode provider transport and locate unique exact quotes in trusted source text.

    This is deterministic alignment, not a model retry or approximate matching.
    Missing/ambiguous text, unknown refs and forged origins still fail admission.
    All business output is validated as AdmissionOutput immediately afterwards.
    """
    raw = json.loads(content)
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        return content
    sources = {(event.event_id, event.revision): event.content for event in events}
    for candidate in raw["candidates"][:64]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("sources"), list):
            continue
        for pointer in candidate["sources"][:32]:
            if (
                not isinstance(pointer, dict)
                or not isinstance(pointer.get("quote"), str)
                or not isinstance(pointer.get("event_id"), str)
                or type(pointer.get("source_revision")) is not int
            ):
                continue
            text = sources.get((pointer.get("event_id"), pointer.get("source_revision")))
            quote = pointer["quote"]
            if text is None or not quote:
                continue
            start = text.find(quote)
            if start >= 0 and text.find(quote, start + 1) < 0:
                left, right = enclosing_clause(text, start, start + len(quote))
                if right - left <= 2048:
                    pointer["start_char"], pointer["end_char"], pointer["quote"] = (
                        left,
                        right,
                        text[left:right],
                    )
                else:
                    pointer["start_char"], pointer["end_char"] = start, start + len(quote)
    return json.dumps(raw, ensure_ascii=False)
