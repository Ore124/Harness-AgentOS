"""Create the durable workflow kernel schema."""
from __future__ import annotations

from alembic import op

from orchestrator.workflow_repository import metadata


revision = "20260911_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
