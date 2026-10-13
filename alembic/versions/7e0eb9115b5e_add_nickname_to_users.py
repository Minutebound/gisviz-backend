"""add nickname to users

Revision ID: 7e0eb9115b5e
Revises: 8236bd7618d5
Create Date: 2026-10-13 15:23:14.695371

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7e0eb9115b5e'
down_revision: Union[str, None] = '8236bd7618d5'
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

