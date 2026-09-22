"""Set shared serving-role passwords without putting secrets in SQL migrations."""

import os

from psycopg2 import sql

ROLE_ENV = {
    "kea_loader": "KEA_LOADER_PASSWORD",
    "kea_app": "KEA_APP_PASSWORD",
}


def configure(conn) -> None:
    missing = [name for name in ROLE_ENV.values() if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    with conn, conn.cursor() as cur:
        for role, variable in ROLE_ENV.items():
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Role {role} does not exist; run make migrate-serving first"
                )
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(role)),
                (os.environ[variable],),
            )
            print(f"configured {role}")


def main() -> None:
    import psycopg2

    dsn = os.environ.get("KEA_MIGRATION_DSN")
    if not dsn:
        raise SystemExit("Set KEA_MIGRATION_DSN to the explicit target database")
    conn = psycopg2.connect(dsn)
    try:
        configure(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
