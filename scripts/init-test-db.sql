CREATE DATABASE prompt_shields_test;

-- pgvector must be enabled per-database. The main `prompt_shields` database
-- gets it via Alembic revision 002; the test database is created outside the
-- migration chain, so enable it here.
\connect prompt_shields_test
CREATE EXTENSION IF NOT EXISTS vector;
