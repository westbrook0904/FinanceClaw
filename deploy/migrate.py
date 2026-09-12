"""Run the empty-database migration and provision the separate memory identity."""

import subprocess
import sys

from provision_memory_role import main as provision_memory_role


def main() -> None:
    """Stop immediately on migration failure; never provision against an older schema."""
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    provision_memory_role()


if __name__ == "__main__":
    main()
