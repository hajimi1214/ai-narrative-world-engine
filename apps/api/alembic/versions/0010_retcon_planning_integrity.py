"""retcon planning integrity"""
from alembic import op

revision = "0010_retcon_planning_integrity"
down_revision = "0009_retcon_replay_planning_foundation"
branch_labels = None
depends_on = None

def upgrade():
    op.create_unique_constraint(
        "uq_retcon_impact_plan_request_version",
        "retcon_impact_plans",
        ["retcon_request_id", "version"],
    )

def downgrade():
    op.drop_constraint("uq_retcon_impact_plan_request_version", "retcon_impact_plans", type_="unique")
