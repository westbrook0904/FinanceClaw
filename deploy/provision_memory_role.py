"""Provision the memory database identity immediately after a fresh application migration.

This command never upgrades permissions on an existing identity or initializes a populated
database. Repeated starts verify the existing grants. Password rotation is a separate DBA
operation, and no credentials are written to logs or command-line arguments.
"""

import os

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

MEMORY_TABLES = ("memory_owners", "memory_sources", "memory_extractions", "memory_records")
READ_TABLES = (
    "conversations",
    "conversation_messages",
    "conversation_turns",
    "interactions",
    "notification_targets",
    "channel_conversation_bindings",
)
APPEND_TABLES = ("audit_records", "notification_events")
MUTABLE_TABLES = (*MEMORY_TABLES, "outbox_events")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _expected_privileges(table: str) -> frozenset[str]:
    """Keep grants explicit so new application tables are denied by default."""
    if table in MUTABLE_TABLES:
        return frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
    if table in APPEND_TABLES:
        return frozenset({"SELECT", "INSERT"})
    if table in READ_TABLES:
        return frozenset({"SELECT"})
    return frozenset()


def _tables(connection, schema: str) -> tuple[str, ...]:
    """Read only application tables in the explicitly selected schema."""
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
            (schema,),
        ).fetchall()
    )


def verify_role(connection, *, role_name: str = "financeclaw_memory", schema: str = "public"):
    """Reject excess permissions, role memberships and missing grants without changing them."""
    role = connection.execute(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls, rolinherit FROM pg_roles WHERE rolname = %s",
        (role_name,),
    ).fetchone()
    if role is None or not role[0] or any(role[1:]):
        raise RuntimeError("memory database identity has unsafe role attributes")
    if connection.execute(
        "SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname=%s)",
        (role_name,),
    ).fetchone():
        raise RuntimeError("memory database identity must not inherit or assume other roles")
    for privilege in ("CREATE", "TEMP"):
        if connection.execute(
            "SELECT has_database_privilege(%s, current_database(), %s)",
            (role_name, privilege),
        ).fetchone()[0]:
            raise RuntimeError("memory database identity must not create database objects")
    if connection.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%%' "
        "AND nspname <> 'information_schema' AND has_schema_privilege(%s, oid, 'CREATE')",
        (role_name,),
    ).fetchone():
        raise RuntimeError("memory database identity must not create schema objects")
    if not connection.execute(
        "SELECT has_schema_privilege(%s, %s, 'USAGE')", (role_name, schema)
    ).fetchone()[0]:
        raise RuntimeError("memory database identity cannot read application schema")
    tables = _tables(connection, schema)
    if not set((*READ_TABLES, *APPEND_TABLES, *MUTABLE_TABLES)).issubset(tables):
        raise RuntimeError("Stage 11 application migration is required before provisioning")
    for table in tables:
        expected = _expected_privileges(table)
        qualified = sql.Identifier(schema, table).as_string(connection)
        for privilege in PRIVILEGES:
            actual = connection.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role_name, qualified, privilege)
            ).fetchone()[0]
            if actual != (privilege in expected):
                raise RuntimeError(f"memory database grant mismatch: {table} {privilege}")


def provision(
    connection,
    *,
    password: str,
    role_name: str = "financeclaw_memory",
    schema: str = "public",
) -> bool:
    """Create minimum grants on empty migrated tables, or verify an existing installation.

    The caller supplies an administrative connection to the intended application database.
    All role creation and grants are atomic. The returned boolean reports initial creation.
    """
    if len(password) < 24 or "\x00" in password:
        raise ValueError("MEMORY_POSTGRES_PASSWORD must contain at least 24 characters")
    with connection.transaction():
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)).fetchone():
            verify_role(connection, role_name=role_name, schema=schema)
            return False
        tables = _tables(connection, schema)
        if not set((*READ_TABLES, *APPEND_TABLES, *MUTABLE_TABLES)).issubset(tables):
            raise RuntimeError("Stage 11 application migration is required before provisioning")
        for table in tables:
            if (
                table != "alembic_version"
                and connection.execute(
                    sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(schema, table))
                ).fetchone()
            ):
                raise RuntimeError("initial memory role provisioning requires an empty database")
        database = connection.execute("SELECT current_database()").fetchone()[0]
        identity = sql.Identifier(role_name)
        try:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(identity, sql.Literal(password))
            )
        except psycopg.Error as error:
            # Utility statements require a SQL literal; server error details can repeat it.
            raise RuntimeError(
                f"memory database identity creation failed: {type(error).__name__}"
            ) from None
        connection.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), identity)
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), identity)
        )
        for table in tables:
            grants = _expected_privileges(table)
            if grants:
                connection.execute(
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        sql.SQL(", ").join(sql.SQL(item) for item in sorted(grants)),
                        sql.Identifier(schema, table),
                        identity,
                    )
                )
        verify_role(connection, role_name=role_name, schema=schema)
        return True


def main() -> None:
    """Read admin connection and separate memory secret from deployment environment only."""
    url = make_url(os.environ["FINANCECLAW_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise ValueError("memory role provisioning requires PostgreSQL")
    with psycopg.connect(
        dbname=url.database,
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        **dict(url.query),
    ) as connection:
        created = provision(connection, password=os.environ["MEMORY_POSTGRES_PASSWORD"])
    print("Memory database identity provisioned" if created else "Memory database grants verified")


if __name__ == "__main__":
    main()
