-- Runs only in a new, explicitly selected Stage 10 volume.
CREATE DATABASE financeclaw_native;
\connect financeclaw_native
CREATE EXTENSION IF NOT EXISTS vector;
