"""adds photo_face_detection_run to Photo

Revision ID: af7a3a40c850
Revises: 90002d808c79
Create Date: 2021-03-20 15:13:39.017070

"""
from alembic import op
import sqlalchemy as sa
import app.models
from sqlalchemy.sql import expression


# revision identifiers, used by Alembic.
revision = 'af7a3a40c850'
down_revision = '90002d808c79'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('photo', sa.Column('face_detection_run', sa.BOOLEAN(), nullable=False, server_default=expression.false()))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('photo', 'face_detection_run')
    # ### end Alembic commands ###