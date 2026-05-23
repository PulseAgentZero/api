"""Entivia Studio — Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Chart type ────────────────────────────────────────────────────────────────

ChartType = Literal[
    "bar", "line", "area", "pie", "scatter", "table", "number",
    "funnel", "heatmap", "gauge", "waterfall", "trend",
    "stat", "bar_gauge", "histogram",
]


# ── Parameter definition ──────────────────────────────────────────────────────

class QueryParamDefinition(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: Literal["text", "number", "date", "datetime"] = "text"
    default_value: str | None = None
    description: str | None = Field(None, max_length=200)
    label: str | None = Field(None, max_length=100)


# ── Column format rule ────────────────────────────────────────────────────────

class ColumnFormatRule(BaseModel):
    type: Literal["currency", "percent", "date", "badge", "number"]
    symbol: str | None = None
    decimals: int | None = Field(None, ge=0, le=10)
    format: str | None = None
    colors: dict[str, str] | None = None


# ── Embedded config / layout types ───────────────────────────────────────────

LegendPosition = Literal["top", "bottom", "left", "right"]


class ChartDisplayOptions(BaseModel):
    """Presentation options (Grafana/Superset-style). Stored in visualization config JSON."""
    show_legend: bool | None = True
    legend_position: LegendPosition | None = "bottom"
    show_grid: bool | None = True
    stacked: bool | None = False
    horizontal: bool | None = False
    x_label_rotate: int | None = Field(None, ge=-90, le=90)
    x_label_max_chars: int | None = Field(None, ge=8, le=64)
    max_points: int | None = Field(None, ge=5, le=500)


class ChartAxesLabels(BaseModel):
    x_label: str | None = Field(None, max_length=200)
    y_label: str | None = Field(None, max_length=200)


class VisualizationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x_axis: str | None = None
    y_axis: str | list[str] | None = None
    color: str | None = None
    title: str | None = None
    value_column: str | None = None
    label_column: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = None
    sparkline_column: str | None = None
    colors: list[str] | None = Field(
        None,
        description="Series color palette (hex). Applied in order to Y series / pie slices.",
    )
    series_colors: dict[str, str] | None = Field(
        None,
        description="Per-series or per-category hex overrides keyed by column or label name.",
    )
    display: ChartDisplayOptions | None = None
    axes: ChartAxesLabels | None = None


class DashboardLayoutItem(BaseModel):
    item_id: UUID
    x: int = Field(0, ge=0)
    y: int = Field(0, ge=0)
    w: int = Field(6, ge=1, le=12)
    h: int = Field(4, ge=1)


# ── Query request schemas ────────────────────────────────────────────────────

class StudioQueryExecuteRequest(BaseModel):
    sql_text: str = Field(..., min_length=1, max_length=50_000)
    connection_id: UUID | None = None
    param_values: dict[str, Any] = Field(default_factory=dict)
    page: int = Field(1, ge=1)
    page_size: int = Field(100, ge=1, le=1000)


class StudioQueryRunRequest(BaseModel):
    """Runtime param values + pagination for running a saved query."""
    param_values: dict[str, Any] = Field(default_factory=dict)
    page: int = Field(1, ge=1)
    page_size: int = Field(100, ge=1, le=1000)


class StudioQueryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    sql_text: str = Field(..., min_length=1, max_length=50_000)
    connection_id: UUID | None = None
    params: list[QueryParamDefinition] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=20)
    refresh_cron: str | None = None
    refresh_enabled: bool = False

    @field_validator("refresh_cron")
    @classmethod
    def validate_cron(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(v)
            except Exception:
                raise ValueError(f"Invalid cron expression: {v!r}")
        return v


class StudioQueryUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    sql_text: str | None = Field(None, min_length=1, max_length=50_000)
    connection_id: UUID | None = None
    params: list[QueryParamDefinition] | None = None
    tags: list[str] | None = None
    refresh_cron: str | None = None
    refresh_enabled: bool | None = None

    @field_validator("refresh_cron")
    @classmethod
    def validate_cron(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(v)
            except Exception:
                raise ValueError(f"Invalid cron expression: {v!r}")
        return v


class StudioGenerateSQLRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    connection_id: UUID | None = None
    time_window: str | None = Field(None, max_length=500)
    segments: str | None = Field(None, max_length=500)
    filters_to_parameterize: str | None = Field(None, max_length=1000)
    extra_context: str | None = Field(None, max_length=2000)


class StudioGenerateSQLIntakeRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    connection_id: UUID | None = None


class StudioGenerateSQLIntakeResponse(BaseModel):
    goal: str
    connections: list[dict[str, Any]]
    default_connection_id: str | None
    schema_preview: str
    questions: list[dict[str, Any]]


# ── Visualization request schemas ─────────────────────────────────────────────

class StudioVisualizationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    chart_type: ChartType
    config: VisualizationConfig = Field(default_factory=VisualizationConfig)
    column_formats: dict[str, ColumnFormatRule] = Field(default_factory=dict)


class StudioVisualizationUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    chart_type: ChartType | None = None
    config: VisualizationConfig | None = None
    column_formats: dict[str, ColumnFormatRule] | None = None


# ── Dashboard request schemas ─────────────────────────────────────────────────

class DashboardTimeRange(BaseModel):
    """Grafana-style time range stored on the dashboard."""

    preset: Literal[
        "last_15m",
        "last_1h",
        "last_6h",
        "last_24h",
        "last_7d",
        "last_30d",
        "custom",
    ] = "last_24h"
    from_: str | None = Field(None, alias="from")
    to: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class StudioDashboardCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    is_public: bool = False
    layout: list[DashboardLayoutItem] = Field(default_factory=list)
    dashboard_params: list[QueryParamDefinition] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=20)
    refresh_interval_seconds: int | None = Field(None, ge=5, le=86400)
    time_range: DashboardTimeRange | None = None


class StudioDashboardUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_public: bool | None = None
    layout: list[DashboardLayoutItem] | None = None
    dashboard_params: list[QueryParamDefinition] | None = None
    tags: list[str] | None = None
    refresh_interval_seconds: int | None = Field(None, ge=0, le=86400)
    time_range: DashboardTimeRange | None = None

    @field_validator("refresh_interval_seconds")
    @classmethod
    def zero_means_off(cls, v: int | None) -> int | None:
        if v == 0:
            return None
        return v


class StudioDashboardAddItemRequest(BaseModel):
    panel_type: Literal["visualization", "text"] = "visualization"
    visualization_id: UUID | None = None
    content: str | None = Field(None, max_length=50_000)
    position: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_panel(self) -> "StudioDashboardAddItemRequest":
        if self.panel_type == "visualization" and self.visualization_id is None:
            raise ValueError("visualization_id is required for panel_type='visualization'")
        if self.panel_type == "text" and not self.content:
            raise ValueError("content is required for panel_type='text'")
        return self


class StudioDashboardForkRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)


class StudioDashboardExecuteRequest(BaseModel):
    """Dashboard-level filter values — propagated to all charts."""
    param_values: dict[str, Any] = Field(default_factory=dict)
    time_range: DashboardTimeRange | None = None


class StudioEmbedTokenRequest(BaseModel):
    expires_in_hours: int = Field(24, ge=1, le=720)


# ── Response schemas ──────────────────────────────────────────────────────────

class StudioQueryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    connection_id: UUID | None
    created_by: UUID | None
    name: str
    description: str | None
    sql_text: str
    params: list[dict[str, Any]]
    tags: list[Any]
    refresh_cron: str | None
    refresh_enabled: bool
    last_run_at: datetime | None
    last_run_row_count: int | None
    created_at: datetime
    updated_at: datetime
    starred: bool = False


class StudioQueryResultResponse(BaseModel):
    rows: list[dict[str, Any]]
    columns: list[str]
    total: int
    page: int
    page_size: int
    cached: bool


class StudioVisualizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    query_id: UUID
    created_by: UUID | None
    name: str
    chart_type: str
    config: dict[str, Any]
    column_formats: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StudioDashboardItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    dashboard_id: UUID
    visualization_id: UUID | None
    position: int
    panel_type: str
    content: str | None
    created_at: datetime


class StudioDashboardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    created_by: UUID | None
    name: str
    description: str | None
    slug: str | None
    is_public: bool
    layout: list[Any]
    dashboard_params: list[Any]
    tags: list[Any]
    refresh_interval_seconds: int | None = None
    time_range: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    items: list[StudioDashboardItemResponse] = []
    starred: bool = False

    @model_validator(mode="before")
    @classmethod
    def _columns_only_from_orm(cls, data: Any) -> Any:
        """Avoid loading StudioDashboard.items (lazy='raise')."""
        from sqlalchemy.inspection import inspect as sa_inspect

        from app.infrastructure.database.models.studio_dashboard import StudioDashboard

        if isinstance(data, StudioDashboard):
            return {
                attr.key: getattr(data, attr.key)
                for attr in sa_inspect(data).mapper.column_attrs
            }
        return data


class StudioQueryRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    query_id: UUID | None
    triggered_by: UUID | None
    status: str
    param_values: dict[str, Any]
    row_count: int | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    result: StudioQueryResultResponse | None = None


class StudioGenerateSQLResponse(BaseModel):
    sql: str
    explanation: str
    params: list[dict[str, Any]]


class StudioRecommendVizResponse(BaseModel):
    chart_type: str
    config: dict[str, Any]
    reasoning: str


class StudioQueryExplainResponse(BaseModel):
    explanation: str


class StudioEmbedTokenResponse(BaseModel):
    token: str
    embed_url: str
    expires_at: datetime


# ── Dashboard execute response ────────────────────────────────────────────────

class DashboardExecuteItemResult(BaseModel):
    visualization_id: UUID
    result: StudioQueryResultResponse | None = None
    error: str | None = None


class StudioDashboardExecuteResponse(BaseModel):
    results: list[DashboardExecuteItemResult]


# ── Public dashboard response ─────────────────────────────────────────────────

class PublicVisualizationResponse(BaseModel):
    """One chart on a public Studio dashboard, including live query results."""

    id: UUID
    name: str = Field(..., description="Visualization title shown in the dashboard UI.")
    chart_type: str = Field(
        ...,
        description="Chart renderer key (e.g. bar, line, table, number).",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Axis, color, and display options for the chart type.",
    )
    column_formats: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-column formatting rules (currency, percent, badges, etc.).",
    )
    query_result: StudioQueryResultResponse | None = Field(
        None,
        description="Executed query rows for this chart. Null if the query failed at render time.",
    )


class PublicDashboardResponse(BaseModel):
    """Rendered public Studio dashboard (not wrapped in the API-key `data` envelope)."""

    id: UUID
    name: str
    description: str | None = Field(None, description="Optional dashboard subtitle or context.")
    slug: str = Field(..., description="Shareable URL slug when the dashboard is public.")
    layout: list[Any] = Field(
        ...,
        description="Grid layout items (`x`, `y`, `w`, `h`) for each panel.",
    )
    dashboard_params: list[Any] = Field(
        default_factory=list,
        description="Filter definitions visitors can pass as query parameters.",
    )
    refresh_interval_seconds: int | None = None
    time_range: dict[str, Any] = Field(default_factory=dict)
    visualizations: list[PublicVisualizationResponse] = Field(
        ...,
        description="Charts on the dashboard, each with up to 500 rows of query data.",
    )
