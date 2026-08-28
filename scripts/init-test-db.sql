-- Runs once, on first initialisation of an empty data volume.
CREATE DATABASE prompt_shields_test;

-- The test suite builds its schema with Base.metadata.create_all, which needs
-- the pgvector type to exist before ai_assets.embedding can be created. The
-- main database gets this from migration 002; the test database has no
-- migrations run against it, so it is created here.
\connect prompt_shields_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
