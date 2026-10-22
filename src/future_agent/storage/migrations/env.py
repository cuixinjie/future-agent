"""Run migrations on the connection provided by the composition adapter."""

from alembic import context

from future_agent.storage.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
