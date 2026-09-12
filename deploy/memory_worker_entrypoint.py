"""Build the dedicated memory DSN without exposing a credential in process arguments."""

import os
import sys

from sqlalchemy.engine import URL


def memory_database_url(environment) -> str:
    """Encode reserved password characters and fix the least-privilege database principal."""
    password = environment.get("MEMORY_POSTGRES_PASSWORD", "")
    if len(password) < 24:
        raise ValueError("MEMORY_POSTGRES_PASSWORD must contain at least 24 characters")
    return URL.create(
        "postgresql+psycopg",
        username="financeclaw_memory",
        password=password,
        host=environment.get("MEMORY_POSTGRES_HOST", "postgres"),
        port=int(environment.get("MEMORY_POSTGRES_PORT", "5432")),
        database=environment.get("MEMORY_POSTGRES_DATABASE", "financeclaw_app"),
    ).render_as_string(hide_password=False)


def main() -> None:
    """Run the worker or its operations CLI with the same dedicated database identity."""
    arguments = sys.argv[1:]
    module = "financeclaw.memory_worker"
    if arguments:
        if arguments[0] != "operations":
            raise ValueError("only the memory operations subcommand is supported")
        module += ".operations"
        arguments = arguments[1:]
    environment = dict(os.environ)
    environment["FINANCECLAW_DATABASE_URL"] = memory_database_url(environment)
    environment["FINANCECLAW_PROCESS_ROLE"] = "memory_worker"
    environment["FINANCECLAW_DATABASE_AUTO_CREATE_SCHEMA"] = "false"
    os.execvpe(sys.executable, [sys.executable, "-m", module, *arguments], environment)


if __name__ == "__main__":
    main()
