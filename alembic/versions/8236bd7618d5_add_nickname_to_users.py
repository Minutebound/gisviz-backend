"""add nickname to users

Revision ID: 8236bd7618d5
Revises: 30f347b2acba
Create Date: 2026-10-13 15:22:52.680483

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8236bd7618d5'
down_revision: Union[str, None] = '30f347b2acba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()




def upgrade_users() -> None:
    pass


def downgrade_users() -> None:
    pass


def upgrade_posts() -> None:
    pass


def downgrade_posts() -> None:
    pass


def upgrade_analytics() -> None:
    pass


def downgrade_analytics() -> None:
    pass


def upgrade_admin() -> None:
    pass


def downgrade_admin() -> None:
    pass


def upgrade_datasets() -> None:
    pass


def downgrade_datasets() -> None:
    pass


def upgrade_misc() -> None:
    pass


def downgrade_misc() -> None:
    pass

