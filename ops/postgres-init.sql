-- psql reads mounted secrets without placing their contents in process arguments or logs.
\set ON_ERROR_STOP on
\set migrator_password `cat /run/secrets/db_migrator_password`
\set app_password `cat /run/secrets/db_app_password`
\set memory_password `cat /run/secrets/db_memory_password`
SELECT format('CREATE ROLE financeclaw LOGIN PASSWORD %L', :'migrator_password') \gexec
SELECT format('CREATE ROLE financeclaw_app LOGIN PASSWORD %L', :'app_password') \gexec
SELECT format('CREATE ROLE financeclaw_memory LOGIN PASSWORD %L', :'memory_password') \gexec
ALTER DATABASE financeclaw_java OWNER TO financeclaw;
GRANT CONNECT ON DATABASE financeclaw_java TO financeclaw_app, financeclaw_memory;
CREATE EXTENSION vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
