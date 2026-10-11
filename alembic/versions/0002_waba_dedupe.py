"""waba dedupe ledger

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "waba_processed_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("waba_message_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("wa_phone", sa.String(32), nullable=False,
                  server_default=""),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("waba_message_id"),
    )
    op.create_index("ix_waba_processed_messages_user_id",
                    "waba_processed_messages", ["user_id"])
    op.create_index("ix_waba_processed_messages_received_at",
                    "waba_processed_messages", ["received_at"])


def downgrade() -> None:
    op.drop_table("waba_processed_messages")
