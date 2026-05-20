"""Profiling Agent — builds behavioural profiles by querying across tables.

Runs second in the pipeline. Uses the schema analysis from Agent 1
to fire targeted cross-table queries and build per-entity profiles.

Provider: Groq (llama-3.3-70b-versatile)
Rationale: Cross-table aggregates and metric derivation are structured
enough for 70B. Fast inference keeps pipeline latency low.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider
from app.agents.prompts.profiling import PROFILING_PROMPT
from app.agents.state import PipelineState
from app.agents.tools.query_tools import build_query_tools
from app.config.settings import settings
from app.infrastructure.external_services.rag import embed_and_store_profiles
from app.services.procedural_memory import format_procedural_block

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_LIMIT = 200


class ProfilingAgent(BaseAgent):
    """Builds rich behavioural profiles for entities via live DB queries.

    Uses Groq (llama-3.3-70b-versatile) for structured cross-table analysis.
    """

    def __init__(self) -> None:
        super().__init__(
            name="ProfilingAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL, 
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Execute entity profiling."""

        org_id = UUID(state["org_id"])
        profile_limit = DEFAULT_PROFILE_LIMIT

        # Register all query tools
        self.registry = type(self.registry)()
        for tool in build_query_tools(db, org_id):
            self.registry.register(tool)

        related_tables = state.get("related_tables", [])
        column_semantics = (state.get("schema_analysis") or {}).get("column_semantics", {})
        entity_table = state.get("entity_table", "")
        entity_table_columns: list[str] = []
        raw_schema = state.get("raw_schema") or {}
        for tbl in raw_schema.get("tables", []):
            if tbl.get("name") == entity_table or tbl.get("table") == entity_table:
                entity_table_columns = tbl.get("columns", [])
                break

        prompt = PROFILING_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            business_context=state.get("business_context", ""),
            entity_label=state.get("entity_label", "entities"),
            goal_label=state.get("goal_label", "improve operations"),
            entity_table=entity_table,
            entity_id_col=state.get("entity_id_col", ""),
            entity_name_col=state.get("entity_name_col", ""),
            entity_table_columns=json.dumps(entity_table_columns),
            signal_columns=json.dumps(state.get("signal_columns", {})),
            related_tables=json.dumps(related_tables, default=str),
            column_semantics=json.dumps(column_semantics),
            profile_limit=profile_limit,
            procedural_block=format_procedural_block(
                state.get("procedural_learnings")
            ),
        )

        user_prompt = (
            f"Build behavioural profiles for the {state.get('entity_label', 'entities')} "
            f"in the '{state.get('entity_table', '')}' table. "
            f"Query related tables to enrich each profile with usage, billing, support, "
            f"and service data. Focus on metrics relevant to: {state.get('goal_label', '')}. "
            f"Profile up to {profile_limit} entities."
        )

        try:
            raw = await self.reason_and_act_json(
                system_prompt=prompt,
                user_prompt=user_prompt,
                required_keys=["entity_profiles"],
                max_iterations=10,
                max_tokens=8192,
            )
            result = json.loads(raw)
        except Exception as e:
            logger.error("[ProfilingAgent] Failed: %s", e)
            state["entity_profiles"] = []
            state["profile_stats"] = {"error": str(e)}
            state["error"] = f"Profiling failed: {e}"
            state["reasoning_log"].extend(self._reasoning_entries)
            return state

        state["entity_profiles"] = result.get("entity_profiles", [])
        profile_stats = result.get("profile_stats", {})
        profile_stats["total_profiled"] = len(state["entity_profiles"])
        state["profile_stats"] = profile_stats
        state["reasoning_log"].extend(self._reasoning_entries)

        # Persist profile embeddings to Qdrant for future-cycle RAG retrieval.
        # Helper handles graceful degradation if Voyage/Qdrant is unavailable.
        await embed_and_store_profiles(str(org_id), state["entity_profiles"])

        logger.info(
            "[ProfilingAgent] Complete: %d entities profiled",
            len(state["entity_profiles"]),
        )
        return state
