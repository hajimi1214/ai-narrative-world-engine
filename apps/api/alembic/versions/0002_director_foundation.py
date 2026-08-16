"""director foundation tables"""
from alembic import op
from app.db import Base
from app import models  # noqa: F401

revision = "0002_director_foundation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())

def downgrade() -> None:
    models.DirectorDecisionLog.__table__.drop(op.get_bind())
    models.SceneProposal.__table__.drop(op.get_bind())
    models.RevealConstraint.__table__.drop(op.get_bind())
