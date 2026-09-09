-- Local PostgreSQL hosts two databases with deliberately separate ownership boundaries.
-- The BFF uses financeclaw_app; LangGraph Agent Server owns financeclaw_agent.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE DATABASE financeclaw_agent OWNER financeclaw;
\connect financeclaw_agent
CREATE EXTENSION IF NOT EXISTS vector;
