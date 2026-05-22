"""Conversational agent HTTP routes — shared by main API and standalone ASGI process."""

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.agent import (
    ChatRequest,
    ChatResponse,
    ConversationListItem,
    ConversationResponse,
)
from app.config.settings import settings
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.agent_conversation_repository import (
    AgentConversationRepository,
)
from app.infrastructure.database.session import get_db
from app.services.agent_service import (
    ChatResult,
    run as run_agent,
    run_stream as run_agent_stream,
    update_user_memory_from_message,
)
from app.services.conversational_memory import summarize_conversation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


async def _maybe_summarize_on_idle(
    db: AsyncSession,
    current_user: User,
    conv,
) -> None:
    """If the prior assistant reply is older than the idle threshold, summarize the
    existing thread before the new turn arrives. Best-effort; never raises."""
    minutes = settings.CONV_MEMORY_IDLE_SUMMARY_MINUTES
    if minutes <= 0 or conv.last_message_at is None:
        return
    prior = (conv.messages or {}).get("messages", [])
    if not prior:
        return
    age = datetime.now(timezone.utc) - conv.last_message_at
    if age < timedelta(minutes=minutes):
        return
    try:
        await summarize_conversation(current_user, prior)
    except Exception as exc:
        logger.debug("[chat] idle-summary failed (non-fatal): %s", exc)


async def _stream_chat_sse(
    db: AsyncSession,
    repo: AgentConversationRepository,
    conv_id: UUID,
    current_user: User,
    messages: list[dict],
):
    """Yield SSE events while streaming the agent reply, then persist + commit at the end."""
    chunks: list[str] = []
    try:
        async for piece in run_agent_stream(db, current_user, messages):
            if not piece:
                continue
            chunks.append(piece)
            yield f"data: {json.dumps({'type': 'text', 'text': piece})}\n\n"
    except Exception as exc:  # surface to client; do not leave the stream hanging
        logger.warning("[chat_stream] generator failed: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    full_reply = "".join(chunks).strip()
    try:
        if full_reply:
            await repo.append_messages(conv_id, {"role": "assistant", "content": full_reply})
            await db.commit()
    except Exception as exc:
        logger.warning("[chat_stream] persist failed: %s", exc)

    yield f"data: {json.dumps({'type': 'done', 'reply': full_reply, 'conversation_id': str(conv_id)})}\n\n"


def _conv_to_response(conv) -> ConversationResponse:
    return ConversationResponse(
        id=conv.id,
        org_id=conv.org_id,
        user_id=conv.user_id,
        messages=(conv.messages or {}).get("messages", []),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.get("/conversations", response_model=list[ConversationListItem])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationListItem]:
    convs = await AgentConversationRepository(db).list_by_user(
        current_user.org_id, current_user.id
    )
    return [
        ConversationListItem(
            id=c.id,
            message_count=len((c.messages or {}).get("messages", [])),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in convs
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    conv = await AgentConversationRepository(db).get_by_id(
        _parse_uuid(conversation_id, "conversation_id")
    )
    if not conv or conv.org_id != current_user.org_id or conv.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return _conv_to_response(conv)


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    conv = await AgentConversationRepository(db).create(
        org_id=current_user.org_id,
        user_id=current_user.id,
    )
    await db.commit()
    return _conv_to_response(conv)


@router.post("/conversations/{conversation_id}/chat", response_model=ChatResponse)
async def chat(
    conversation_id: str,
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv_id = _parse_uuid(conversation_id, "conversation_id")
    repo = AgentConversationRepository(db)
    conv = await repo.get_by_id(conv_id)
    if not conv or conv.org_id != current_user.org_id or conv.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )

    # If this conversation was idle past the threshold, summarize the prior segment
    # into long-term memory before adding the new turn.
    await _maybe_summarize_on_idle(db, current_user, conv)

    user_msg = {"role": "user", "content": body.content}
    await repo.append_messages(conv_id, user_msg)
    await update_user_memory_from_message(db, current_user, body.content)
    await db.flush()

    messages = (conv.messages or {}).get("messages", [])

    if body.stream:
        return StreamingResponse(
            _stream_chat_sse(db, repo, conv_id, current_user, messages),
            media_type="text/event-stream",
        )

    result: ChatResult = await run_agent(db, current_user, messages, conversation_id=conv_id)

    assistant_msg: dict = {"role": "assistant", "content": result.reply}
    if result.tool_context:
        assistant_msg["tool_context"] = result.tool_context
    if result.tools_called:
        assistant_msg["tools_called"] = result.tools_called
    if result.artifacts:
        assistant_msg["artifacts"] = result.artifacts
    await repo.append_messages(conv_id, assistant_msg)
    await db.commit()
    logger.info(
        "[Chat] turn complete conversation_id=%s user_id=%s",
        conv_id, current_user.id,
    )

    return ChatResponse(
        reply=result.reply,
        conversation_id=conv_id,
        tools_called=result.tools_called or None,
        artifacts=result.artifacts or None,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat_without_path_conversation(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentConversationRepository(db)
    if body.conversation_id:
        conv = await repo.get_by_id(body.conversation_id)
        if not conv or conv.org_id != current_user.org_id or conv.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
    else:
        conv = await repo.create(org_id=current_user.org_id, user_id=current_user.id)
        await db.flush()

    await _maybe_summarize_on_idle(db, current_user, conv)

    user_msg = {"role": "user", "content": body.content}
    await repo.append_messages(conv.id, user_msg)
    await update_user_memory_from_message(db, current_user, body.content)
    await db.flush()

    messages = (conv.messages or {}).get("messages", [])

    if body.stream:
        return StreamingResponse(
            _stream_chat_sse(db, repo, conv.id, current_user, messages),
            media_type="text/event-stream",
        )

    result: ChatResult = await run_agent(db, current_user, messages, conversation_id=conv.id)

    assistant_msg: dict = {"role": "assistant", "content": result.reply}
    if result.tool_context:
        assistant_msg["tool_context"] = result.tool_context
    if result.tools_called:
        assistant_msg["tools_called"] = result.tools_called
    if result.artifacts:
        assistant_msg["artifacts"] = result.artifacts
    await repo.append_messages(conv.id, assistant_msg)
    await db.commit()
    return ChatResponse(
        reply=result.reply,
        conversation_id=conv.id,
        tools_called=result.tools_called or None,
        artifacts=result.artifacts or None,
    )
