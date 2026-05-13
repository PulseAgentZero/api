import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.agent import (
    ChatRequest,
    ChatResponse,
    ConversationListItem,
    ConversationResponse,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.agent_conversation_repository import (
    AgentConversationRepository,
)
from app.infrastructure.database.session import get_db
from app.services.agent_service import run as run_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["Agent"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


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
) -> ChatResponse:
    conv_id = _parse_uuid(conversation_id, "conversation_id")
    repo = AgentConversationRepository(db)
    conv = await repo.get_by_id(conv_id)
    if not conv or conv.org_id != current_user.org_id or conv.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )

    user_msg = {"role": "user", "content": body.content}
    await repo.append_messages(conv_id, user_msg)
    await db.flush()

    messages = (conv.messages or {}).get("messages", [])
    reply_text = await run_agent(db, current_user, messages)

    assistant_msg = {"role": "assistant", "content": reply_text}
    await repo.append_messages(conv_id, assistant_msg)
    await db.commit()

    return ChatResponse(reply=reply_text, conversation_id=conv_id)


@router.post("/chat", response_model=ChatResponse)
async def chat_without_path_conversation(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
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

    user_msg = {"role": "user", "content": body.content}
    await repo.append_messages(conv.id, user_msg)
    await db.flush()

    messages = (conv.messages or {}).get("messages", [])
    reply_text = await run_agent(db, current_user, messages)

    assistant_msg = {"role": "assistant", "content": reply_text}
    await repo.append_messages(conv.id, assistant_msg)
    await db.commit()
    return ChatResponse(reply=reply_text, conversation_id=conv.id)
