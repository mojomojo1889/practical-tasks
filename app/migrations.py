from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def column_exists(engine: Engine, table: str, column: str) -> bool:
    inspector = inspect(engine)
    return any(item["name"] == column for item in inspector.get_columns(table))


def table_exists(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def add_column_if_missing(engine: Engine, table: str, column: str, definition: str) -> None:
    if not column_exists(engine, table, column):
        with engine.begin() as connection:
            connection.execute(
                text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
            )


def run_migrations(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS academic_groups (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    join_code_hash VARCHAR(64),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS group_memberships (
                    id INTEGER PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    UNIQUE(group_id, user_id),
                    FOREIGN KEY(group_id) REFERENCES academic_groups(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS group_teachers (
                    id INTEGER PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    teacher_id INTEGER NOT NULL,
                    permission VARCHAR(20) NOT NULL DEFAULT 'owner',
                    created_at DATETIME NOT NULL,
                    UNIQUE(group_id, teacher_id),
                    FOREIGN KEY(group_id) REFERENCES academic_groups(id),
                    FOREIGN KEY(teacher_id) REFERENCES users(id)
                )
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS role_requests (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    requested_role VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    reviewed_by INTEGER,
                    reviewed_at DATETIME,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(reviewed_by) REFERENCES users(id)
                )
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    actor_id INTEGER,
                    action VARCHAR(80) NOT NULL,
                    target_type VARCHAR(80),
                    target_id INTEGER,
                    details TEXT NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(actor_id) REFERENCES users(id)
                )
                """
            )
        )

    add_column_if_missing(
        engine,
        "users",
        "teacher_approved",
        "BOOLEAN NOT NULL DEFAULT 0",
    )

    add_column_if_missing(
        engine,
        "users",
        "must_change_password",
        "BOOLEAN NOT NULL DEFAULT 0",
    )

    add_column_if_missing(
        engine,
        "tasks",
        "academic_group_id",
        "INTEGER",
    )

    add_column_if_missing(
        engine,
        "tasks",
        "category_key",
        "VARCHAR(80)",
    )

    add_column_if_missing(
        engine,
        "tasks",
        "custom_category",
        "VARCHAR(120)",
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE tasks
                SET category_key = category
                WHERE category_key IS NULL
                """
            )
        )

        connection.execute(
            text(
                """
                UPDATE tasks
                SET category_key = 'other'
                WHERE category_key IS NULL OR TRIM(category_key) = ''
                """
            )
        )

        connection.execute(
            text(
                """
                UPDATE tasks
                SET custom_category = category
                WHERE category_key = 'other'
                  AND custom_category IS NULL
                  AND category IS NOT NULL
                  AND TRIM(category) NOT IN ('other', '')
                """
            )
        )
