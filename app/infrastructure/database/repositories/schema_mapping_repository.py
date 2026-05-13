from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.schema_mapping import SchemaMapping


class SchemaMappingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, mapping_id: UUID) -> SchemaMapping | None:
        return await self.db.get(SchemaMapping, mapping_id)

    async def list_by_org(self, org_id: UUID) -> list[SchemaMapping]:
        result = await self.db.execute(
            select(SchemaMapping)
            .where(SchemaMapping.org_id == org_id)
            .order_by(SchemaMapping.created_at)
        )
        return list(result.scalars().all())

    async def list_by_connection(self, connection_id: UUID) -> list[SchemaMapping]:
        result = await self.db.execute(
            select(SchemaMapping)
            .where(SchemaMapping.connection_id == connection_id)
            .order_by(SchemaMapping.created_at)
        )
        return list(result.scalars().all())

    async def create(
        self,
        org_id: UUID,
        connection_id: UUID,
        entity_table: str,
        entity_id_col: str,
        entity_name_col: str | None = None,
        signal_columns: dict | None = None,
        timestamp_col: str | None = None,
        risk_config: dict | None = None,
        raw_schema: dict | None = None,
        target_column: str | None = None,
    ) -> SchemaMapping:
        mapping = SchemaMapping(
            org_id=org_id,
            connection_id=connection_id,
            entity_table=entity_table,
            entity_id_col=entity_id_col,
            entity_name_col=entity_name_col,
            signal_columns=signal_columns,
            timestamp_col=timestamp_col,
            risk_config=risk_config,
            raw_schema=raw_schema,
            target_column=target_column,
        )
        self.db.add(mapping)
        await self.db.flush()
        return mapping

    async def update(self, mapping_id: UUID, **fields) -> SchemaMapping | None:
        mapping = await self.get_by_id(mapping_id)
        if mapping is None:
            return None
        for key, value in fields.items():
            if hasattr(mapping, key):
                setattr(mapping, key, value)
        await self.db.flush()
        return mapping

    async def delete(self, mapping_id: UUID) -> bool:
        mapping = await self.get_by_id(mapping_id)
        if mapping is None:
            return False
        await self.db.delete(mapping)
        await self.db.flush()
        return True

    async def deactivate_for_connection(self, connection_id: UUID) -> None:
        await self.db.execute(
            update(SchemaMapping)
            .where(SchemaMapping.connection_id == connection_id)
            .values(is_active=False)
        )
        await self.db.flush()
