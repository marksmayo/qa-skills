from alembic import context

# No lock_timeout or statement_timeout is set for migrations.
context.configure(url=context.config.get_main_option("sqlalchemy.url"), transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
