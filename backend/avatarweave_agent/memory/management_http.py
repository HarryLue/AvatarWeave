"""Account-bound memory management HTTP projections (contract 04)."""

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, Field, ValidationError
from sqlalchemy import select

from .contracts import (
    Contract,
    CorrectMemoryBody,
    CorrectTranscriptBody,
    DeleteMemoryBody,
    ErrorInfo,
    Identifier,
    PageRequest,
    PositiveInt,
    PrivacyInput,
    PrivacyPreference,
    PublicApiResult,
    PublicDeletionView,
    PublicEventProcessing,
    PublicMemoryPage,
    PublicMemoryView,
    PublicOpenLoopPage,
    PublicOpenLoopView,
    PublicTimelineItem,
    PublicTimelinePage,
    RepositoryInput,
    UpdateOpenLoopBody,
    VersionRef,
)
from .entities import EntityBody, PublicEntity, PublicEntityPage
from .execution import MemoryFault
from .http import body_as, body_openapi, fence_for, invalid_body, meta, response, runtime, scope_for
from .organizer import PinMemoryBody
from .reflections import PublicReflection, PublicReflectionPage, ReflectionReviewBody
from .storage import Account
from .tables import Deletion

router = APIRouter(prefix="/api/v2")


class PublicTranscriptView(Contract):
    event_id: Identifier
    source_revision: PositiveInt
    text: Annotated[str, Field(max_length=16000)]
    occurred_at: AwareDatetime


@router.get(
    "/sessions/{session_id}/transcripts", response_model=PublicApiResult[list[PublicTranscriptView]]
)
async def transcripts(session_id: str, request: Request):
    from .contracts import SourceEvent
    from .storage import SourceRevision
    from .utils import utc

    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    scope = scope_for(rt, account)
    async with rt.database.sessions() as db:
        await rt.service._account(db, scope)
        await rt.service._session(db, scope, session_id)
        rows = list(
            await db.scalars(
                select(SourceRevision)
                .where(
                    SourceRevision.account_id == account.id,
                    SourceRevision.session_id == session_id,
                    SourceRevision.revoked.is_(False),
                    SourceRevision.source_json.is_not(None),
                    SourceRevision.expires_at > rt.service.clock(),
                )
                .order_by(SourceRevision.sequence.desc())
                .limit(32)
            )
        )
        return response(
            [
                PublicTranscriptView(
                    event_id=row.event_id,
                    source_revision=row.revision,
                    text=SourceEvent.model_validate_json(row.source_json).content,
                    occurred_at=utc(row.occurred_at),
                )
                for row in rows
            ]
        )


@router.patch(
    "/transcripts",
    response_model=PublicApiResult[PublicEventProcessing],
    openapi_extra=body_openapi(CorrectTranscriptBody),
)
async def correct_transcript(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, CorrectTranscriptBody)
    if body is None:
        return invalid_body()
    return response(await rt.service.correct_transcript(scope_for(rt, account), body), status=202)


@router.patch(
    "/memories/{memory_id}/pin",
    response_model=PublicApiResult[PublicMemoryView],
    openapi_extra=body_openapi(PinMemoryBody),
)
async def pin_memory(memory_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, PinMemoryBody)
    if body is None:
        return invalid_body()
    return response(await rt.service.pin_memory(scope_for(rt, account), memory_id, body))


@router.get("/open-loops", response_model=PublicApiResult[PublicOpenLoopPage])
async def open_loops(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(await rt.service.list_open_loops(scope_for(rt, account), page_query(request)))


@router.patch(
    "/open-loops/{loop_id}",
    response_model=PublicApiResult[PublicOpenLoopView],
    openapi_extra=body_openapi(UpdateOpenLoopBody),
)
async def update_open_loop(loop_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, UpdateOpenLoopBody)
    if body is None:
        return invalid_body()
    return response(await rt.service.update_open_loop(scope_for(rt, account), loop_id, body))


def page_query(request):
    text = request.query_params.get("limit", "30")
    if not text.isascii() or not text.isdecimal():
        raise MemoryFault("invalid_model_output", "Page limit must be an integer")
    try:
        return PageRequest(limit=int(text), cursor=request.query_params.get("cursor"))
    except ValidationError:
        raise MemoryFault("invalid_model_output", "Invalid pagination") from None


@router.get("/entities", response_model=PublicApiResult[PublicEntityPage])
async def entities(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(await rt.service.public_entities(scope_for(rt, account), page_query(request)))


@router.post(
    "/entities",
    response_model=PublicApiResult[PublicEntity],
    openapi_extra=body_openapi(EntityBody),
)
@router.patch(
    "/entities/{entity_id}",
    response_model=PublicApiResult[PublicEntity],
    openapi_extra=body_openapi(EntityBody),
)
async def save_entity(request: Request, entity_id: str | None = None):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, EntityBody)
    if body is None:
        return invalid_body()
    return response(await rt.service.save_entity(scope_for(rt, account), body, entity_id))


@router.delete(
    "/entities/{entity_id}",
    response_model=PublicApiResult[PublicDeletionView],
    openapi_extra=body_openapi(DeleteMemoryBody),
)
async def delete_entity(entity_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, DeleteMemoryBody)
    if body is None:
        return invalid_body()
    result = await rt.service.delete_entity(scope_for(rt, account), entity_id, body)
    return response(await deletion_view(rt, account.id, result.deletion_job_id))


@router.get("/reflections", response_model=PublicApiResult[PublicReflectionPage])
async def reflections(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(await rt.service.list_reflections(scope_for(rt, account), page_query(request)))


@router.patch(
    "/reflections/{reflection_id}",
    response_model=PublicApiResult[PublicReflection],
    openapi_extra=body_openapi(ReflectionReviewBody),
)
async def review_reflection(reflection_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, ReflectionReviewBody)
    if body is None:
        return invalid_body()
    return response(await rt.service.review_reflection(scope_for(rt, account), reflection_id, body))


@router.get("/memories", response_model=PublicApiResult[PublicMemoryPage])
async def memories(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    page = page_query(request)
    scope = scope_for(rt, account)
    result = await rt.service.repository(
        RepositoryInput(
            meta=meta(rt.service),
            scope=scope,
            fence=fence_for(account),
            operation="scan_period",
            refs=[],
            cursor=page.cursor,
            page_size=page.limit,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    views = [
        await rt.service.public_memory(scope, record.ref.object_id, version=record.ref.version)
        for record in result.data.records
    ]
    return response(
        PublicMemoryPage(
            items=views, next_cursor=result.data.next_cursor, revision=result.data.read_watermark
        )
    )


@router.get("/memories/{memory_id}", response_model=PublicApiResult[PublicMemoryView])
async def memory(memory_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(await rt.service.public_memory(scope_for(rt, account), memory_id))


@router.get("/memories/{memory_id}/evidence", response_model=PublicApiResult[PublicMemoryView])
async def evidence(memory_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(
        await rt.service.public_memory(scope_for(rt, account), memory_id, include_quote=True)
    )


@router.patch(
    "/memories/{memory_id}",
    response_model=PublicApiResult[PublicMemoryView],
    openapi_extra=body_openapi(CorrectMemoryBody),
)
async def correct(memory_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, CorrectMemoryBody)
    if body is None:
        return invalid_body()
    view, pending = await rt.service.correct_memory(scope_for(rt, account), memory_id, body)
    return response(
        view,
        status=202 if pending else 200,
        error=ErrorInfo(
            code="memory_pending",
            message="Correction recorded; the previous fact is awaiting verification",
            retryable=False,
            retry_after_ms=500,
        )
        if pending
        else None,
    )


@router.delete(
    "/memories/{memory_id}",
    response_model=PublicApiResult[PublicDeletionView],
    openapi_extra=body_openapi(DeleteMemoryBody),
)
async def delete_memory(memory_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, DeleteMemoryBody)
    if body is None:
        return invalid_body()
    scope = scope_for(rt, account)
    event, _ = await rt.service.control_event(
        scope,
        key=body.idempotency_key,
        operation="delete_memory",
        body=body,
        text="delete_memory:" + memory_id,
        target=memory_id,
    )
    # Build the current fence AFTER the durable control event; retries never
    # treat the old preference/source watermarks as fresh authorization.
    async with rt.database.sessions() as db:
        account = await db.get(Account, account.id)
    preferences = PrivacyPreference.model_validate_json(account.preferences_json)
    result = await rt.service.privacy(
        PrivacyInput(
            meta=meta(rt.service),
            scope=scope,
            fence=fence_for(account),
            operation="delete_memory",
            preferences=None,
            target_refs=[VersionRef(object_id=memory_id, version=body.expected_version)],
            target_source_ids=[],
            target_time=None,
            expected_preference_version=preferences.preference_version,
            confirmed_control_event_id=event.event_id,
            deletion_job_id=None,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    return await deletion_view(rt, account.id, result.data.deletion_job_id)


async def deletion_view(rt, account_id, job_id):
    async with rt.database.sessions() as db:
        row = await db.scalar(
            select(Deletion).where(Deletion.id == job_id, Deletion.account_id == account_id)
        )
        if row is None:
            raise MemoryFault("not_found", "Deletion job not found")
        from .utils import utc

        return response(
            PublicDeletionView(
                deletion_job_id=row.id,
                state=row.state,
                logical_revoked_at=utc(row.created_at),
                physical_cleanup_complete=row.state == "completed",
                backup_expiry_at=None,
                retryable=row.state == "failed",
            ),
            status=202 if row.state == "revoked_cleaning" else 200,
        )


@router.get("/deletion-jobs/{job_id}", response_model=PublicApiResult[PublicDeletionView])
async def deletion_job(job_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return await deletion_view(rt, account.id, job_id)


@router.get("/timeline", response_model=PublicApiResult[PublicTimelinePage])
async def timeline(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    page = page_query(request)
    scope = scope_for(rt, account)
    result = await rt.service.repository(
        RepositoryInput(
            meta=meta(rt.service),
            scope=scope,
            fence=fence_for(account),
            operation="scan_period",
            refs=[],
            cursor=page.cursor,
            page_size=page.limit,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    return response(
        PublicTimelinePage(
            items=[
                PublicTimelineItem(
                    memory_id=r.ref.object_id,
                    version=r.ref.version,
                    title=r.display_text[:2048],
                    period=r.statement.valid_time,
                    related_names=[],
                    status="uncertain"
                    if r.status == "uncertain"
                    else "confirmed"
                    if r.assertion_kind == "user_confirmed"
                    else "reported",
                    protected_from_proactive_recall=r.sensitivity != "normal",
                )
                for r in result.data.records
            ],
            next_cursor=result.data.next_cursor,
        )
    )
