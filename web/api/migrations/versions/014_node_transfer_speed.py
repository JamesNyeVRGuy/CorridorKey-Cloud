"""Add transfer speed columns to node_reputations.

Revision ID: 014
Revises: 013
Create Date: 2026-04-15

Tracks per-node average download/upload speed (MB/s) as an exponential
moving average, updated on each completed job. Used in reputation scoring
to deprioritize nodes with slow network connections.
"""

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ck.node_reputations
            ADD COLUMN IF NOT EXISTS avg_download_mbps DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS avg_upload_mbps DOUBLE PRECISION NOT NULL DEFAULT 0
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE ck.node_reputations
            DROP COLUMN IF EXISTS avg_download_mbps,
            DROP COLUMN IF EXISTS avg_upload_mbps
    """)
