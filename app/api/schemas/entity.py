from pydantic import BaseModel


class EntitySummary(BaseModel):
    entity_id: str
    entity_label: str | None
    risk_score: float
    risk_tier: str
    signals: dict


class EntityListResponse(BaseModel):
    entities: list[EntitySummary]
    total: int
    page: int
    page_size: int


class EntityDetail(BaseModel):
    entity_id: str
    entity_label: str | None
    risk_score: float
    risk_tier: str
    signals: dict
    fields: dict


class EntityTrendPoint(BaseModel):
    timestamp: str
    values: dict


class EntityTrendResponse(BaseModel):
    entity_id: str
    points: list[EntityTrendPoint]
