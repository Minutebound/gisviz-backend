<%!
import re
%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()


<%
    db_names = config.get_main_option("databases")
%>
% for db_name in re.split(r',\s*', db_names):

def upgrade_${db_name}() -> None:
    ${upgrades[db_name] if db_name in upgrades else "pass"}


def downgrade_${db_name}() -> None:
    ${downgrades[db_name] if db_name in downgrades else "pass"}

% endfor