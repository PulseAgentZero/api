"""Pipeline state passed between autonomous agents."""

from typing import TypedDict, Optional


class PipelineState(TypedDict, total=False):
    # Organisation context (populated by orchestrator before first agent runs)
    org_id: str
    org_name: str
    entity_label: str
    goal_label: str
    business_context: str
    industry: str
    connection_id: str
    db_type: str

    # Schema mapping (from Pulse DB)
    entity_table: str
    entity_id_col: str
    entity_name_col: Optional[str]
    signal_columns: dict            # {signal_label: column_name}
    timestamp_col: Optional[str]
    risk_config: dict
    raw_schema: dict                # introspected tables + columns

    # Schema Intelligence Agent output
    schema_analysis: dict           # table relationships, column semantics
    validated_columns: list[str]    # columns confirmed in live DB
    related_tables: list[dict]      # discovered related tables + join keys
    schema_issues: list[dict]       # mismatches or warnings

    # Profiling Agent output
    entity_profiles: list[dict]     # per-entity behavioural profiles
    profile_stats: dict             # aggregate stats across all entities

    # Model Training Agent output
    target_column: Optional[str]        # discovered or mapped target variable
    ml_available: bool                  # whether ML scoring is available
    model_metrics: dict                 # accuracy, f1, auc_roc, etc.
    feature_importances: list[dict]     # [{feature, importance}] sorted desc
    ml_scored_entities: list[dict]      # entities scored by ML model

    # Risk Scoring Agent output
    scored_entities: list[dict]     # entities with risk_score, risk_tier, risk_narrative
    risk_summary: dict              # tier breakdown, key findings

    # Recommendation Agent output
    recommendations: list[dict]     # generated recommendation records
    recommendation_stats: dict      # counts by urgency

    # Control flow
    current_step: str
    error: Optional[str]
    reasoning_log: list[dict]
    pipeline_metrics: dict              # per-step and aggregate metrics
    pipeline_run_id: Optional[str]      # FK to pipeline_runs row (post-finalize)
    generation_caps: Optional[dict]     # caps/sampling notes (e.g. narrative limit hit)
    started_at: str
    completed_at: Optional[str]
