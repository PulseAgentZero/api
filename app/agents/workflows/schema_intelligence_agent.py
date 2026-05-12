"""Schema Intelligence Agent — validates and analyses the org's database schema.

Runs first in the pipeline. Builds the knowledge representation that
every downstream agent depends on.

Provider: Groq (llama-3.3-70b-versatile)
Rationale: Structured task — validate columns, discover tables, classify
column semantics. Fast inference on Groq. Does not need Claude's reasoning depth.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider
from app.agents.prompts.schema_intelligence import SCHEMA_INTELLIGENCE_PROMPT
from app.agents.state import PipelineState
from app.agents.tools.query_tools import build_query_tools
from app.config.settings import settings

logger = logging.getLogger(__name__)


class SchemaIntelligenceAgent(BaseAgent):
    """Analyses the org's DB schema and validates the onboarding mapping.

    Uses Groq (llama-3.3-70b-versatile) for fast, structured analysis.
    """

    def __init__(self) -> None:
        super().__init__(
            name="SchemaIntelligenceAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Execute schema intelligence analysis."""

        org_id = UUID(state["org_id"])

        # Fresh tool registry for this run
        self.registry = type(self.registry)()
        for tool in build_query_tools(db, org_id):
            if tool.name in ("list_tables", "validate_column_exists", "get_row_count", "query_related_table"):
                self.registry.register(tool)

        prompt = SCHEMA_INTELLIGENCE_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            business_context=state.get("business_context", ""),
            entity_label=state.get("entity_label", "entities"),
            goal_label=state.get("goal_label", "improve operations"),
            entity_table=state.get("entity_table", ""),
            entity_id_col=state.get("entity_id_col", ""),
            entity_name_col=state.get("entity_name_col", ""),
            signal_columns=json.dumps(state.get("signal_columns", {})),
            timestamp_col=state.get("timestamp_col", ""),
        )

        user_prompt = (
            "Analyse the database schema for this organisation. "
            "Validate all mapped columns, discover related tables, "
            "and build the schema knowledge representation."
        )

        try:
            raw = await self.reason_and_act_json(
                system_prompt=prompt,
                user_prompt=user_prompt,
                required_keys=["schema_valid", "validated_columns"],
                max_iterations=8,
            )
            result = json.loads(raw)
        except Exception as e:
            logger.error("[SchemaIntelligenceAgent] Failed: %s", e)
            state["schema_issues"] = [{"issue": str(e)}]
            state["validated_columns"] = []
            state["related_tables"] = []
            state["schema_analysis"] = {}
            state["error"] = f"Schema intelligence failed: {e}"
            state["reasoning_log"].extend(self._reasoning_entries)
            return state

        state["schema_analysis"] = result
        state["validated_columns"] = result.get("validated_columns", [])
        state["related_tables"] = result.get("related_tables", [])
        state["schema_issues"] = result.get("schema_issues", [])
        state["reasoning_log"].extend(self._reasoning_entries)

        logger.info(
            "[SchemaIntelligenceAgent] Complete: %d validated cols, %d related tables, %d issues",
            len(state["validated_columns"]),
            len(state["related_tables"]),
            len(state["schema_issues"]),
        )
        return state
