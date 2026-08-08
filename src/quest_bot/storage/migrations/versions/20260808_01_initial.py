"""Create the quest schema.

Revision ID: 20260808_01
Revises:
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSITIONS = "'not_started', 'intro', 'stage', 'finished', 'timed_out'"
CONTENT_TYPES = "'text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note'"


def stable_check(condition: str, *, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(name))


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at_ms", sa.Integer(), nullable=False),
        stable_check("length(trim(username)) > 0", name="ck_users_username_nonempty"),
        stable_check("role IN ('admin', 'captain')", name="ck_users_role"),
        stable_check("active IN (0, 1)", name="ck_users_active"),
        stable_check("updated_at_ms >= 0", name="ck_users_updated_at_ms"),
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sqlite_strict=True,
    )
    op.execute("CREATE INDEX users_username_nocase_idx ON users(username COLLATE NOCASE)")

    op.create_table(
        "quest_settings",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("time_limit_minutes", sa.Integer(), server_default=sa.text("80"), nullable=False),
        stable_check("singleton_id = 1", name="ck_quest_settings_singleton"),
        stable_check("time_limit_minutes > 0", name="ck_quest_settings_time_limit_minutes"),
        sa.PrimaryKeyConstraint("singleton_id", name="pk_quest_settings"),
        sqlite_strict=True,
    )
    settings = sa.table(
        "quest_settings",
        sa.column("singleton_id", sa.Integer()),
        sa.column("time_limit_minutes", sa.Integer()),
    )
    op.bulk_insert(settings, [{"singleton_id": 1, "time_limit_minutes": 80}])

    op.create_table(
        "score_steps",
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        stable_check("attempt_number > 0", name="ck_score_steps_attempt_number"),
        stable_check("points >= 0", name="ck_score_steps_points"),
        sa.PrimaryKeyConstraint("attempt_number", name="pk_score_steps"),
        sqlite_strict=True,
    )
    score_steps = sa.table(
        "score_steps",
        sa.column("attempt_number", sa.Integer()),
        sa.column("points", sa.Integer()),
    )
    op.bulk_insert(
        score_steps,
        [
            {"attempt_number": 1, "points": 10},
            {"attempt_number": 2, "points": 8},
            {"attempt_number": 3, "points": 5},
            {"attempt_number": 4, "points": 0},
        ],
    )

    op.create_table(
        "global_content_parts",
        sa.Column("content_kind", sa.Text(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        stable_check(
            "content_kind IN ('intro', 'success', 'timeout')",
            name="ck_global_content_parts_content_kind",
        ),
        stable_check("part_number > 0", name="ck_global_content_parts_part_number"),
        stable_check(
            f"content_type IN ({CONTENT_TYPES})", name="ck_global_content_parts_content_type"
        ),
        stable_check("length(data) > 0", name="ck_global_content_parts_data_nonempty"),
        sa.PrimaryKeyConstraint("content_kind", "part_number", name="pk_global_content_parts"),
        sqlite_strict=True,
        sqlite_with_rowid=False,
    )

    op.create_table(
        "stages",
        sa.Column("stage_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        stable_check("stage_number > 0", name="ck_stages_stage_number"),
        stable_check("length(trim(name)) > 0", name="ck_stages_name_nonempty"),
        sa.PrimaryKeyConstraint("stage_number", name="pk_stages"),
        sqlite_strict=True,
    )

    op.create_table(
        "tasks",
        sa.Column("stage_number", sa.Integer(), nullable=False),
        sa.Column("task_number", sa.Integer(), nullable=False),
        sa.Column("correct_answer_raw", sa.Text(), nullable=False),
        sa.Column("correct_answer_normalized", sa.Text(), nullable=False),
        stable_check("stage_number > 0", name="ck_tasks_stage_number"),
        stable_check("task_number > 0", name="ck_tasks_task_number"),
        stable_check("length(trim(correct_answer_raw)) > 0", name="ck_tasks_answer_raw_nonempty"),
        stable_check(
            "length(correct_answer_normalized) > 0",
            name="ck_tasks_answer_normalized_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["stage_number"],
            ["stages.stage_number"],
            name="fk_tasks_stage_number_stages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("stage_number", "task_number", name="pk_tasks"),
        sqlite_strict=True,
        sqlite_with_rowid=False,
    )

    op.create_table(
        "task_prompt_parts",
        sa.Column("stage_number", sa.Integer(), nullable=False),
        sa.Column("task_number", sa.Integer(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        stable_check("stage_number > 0", name="ck_task_prompt_parts_stage_number"),
        stable_check("task_number > 0", name="ck_task_prompt_parts_task_number"),
        stable_check("part_number > 0", name="ck_task_prompt_parts_part_number"),
        stable_check(
            f"content_type IN ({CONTENT_TYPES})", name="ck_task_prompt_parts_content_type"
        ),
        stable_check("length(data) > 0", name="ck_task_prompt_parts_data_nonempty"),
        sa.ForeignKeyConstraint(
            ["stage_number", "task_number"],
            ["tasks.stage_number", "tasks.task_number"],
            name="fk_task_prompt_parts_stage_number_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "stage_number", "task_number", "part_number", name="pk_task_prompt_parts"
        ),
        sqlite_strict=True,
        sqlite_with_rowid=False,
    )

    op.create_table(
        "captain_state",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("started_at_ms", sa.Integer(), nullable=True),
        sa.Column("current_stage_number", sa.Integer(), nullable=True),
        stable_check(f"position IN ({POSITIONS})", name="ck_captain_state_position"),
        stable_check(
            "started_at_ms IS NULL OR started_at_ms >= 0",
            name="ck_captain_state_started_at_ms",
        ),
        stable_check(
            "current_stage_number IS NULL OR current_stage_number > 0",
            name="ck_captain_state_current_stage_number",
        ),
        stable_check(
            "(position = 'not_started' AND started_at_ms IS NULL "
            "AND current_stage_number IS NULL) OR "
            "(position = 'intro' AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NULL) OR "
            "(position = 'stage' AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NOT NULL) OR "
            "(position IN ('finished', 'timed_out') AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NULL)",
            name="ck_captain_state_position_fields",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_captain_state_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_captain_state"),
        sqlite_strict=True,
    )
    op.create_index(
        "captain_state_position_idx",
        "captain_state",
        ["position", "started_at_ms"],
    )

    op.create_table(
        "captain_transitions",
        sa.Column("transition_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("from_position", sa.Text(), nullable=False),
        sa.Column("from_stage_number", sa.Integer(), nullable=True),
        sa.Column("to_position", sa.Text(), nullable=False),
        sa.Column("to_stage_number", sa.Integer(), nullable=True),
        sa.Column("event_at_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at_ms", sa.Integer(), nullable=False),
        sa.Column("source_update_id", sa.Integer(), nullable=True),
        sa.Column(
            "skipped_unsolved_tasks", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        stable_check("sequence_number > 0", name="ck_captain_transitions_sequence_number"),
        stable_check(
            f"from_position IN ({POSITIONS})",
            name="ck_captain_transitions_from_position",
        ),
        stable_check(
            "from_stage_number IS NULL OR from_stage_number > 0",
            name="ck_captain_transitions_from_stage_number",
        ),
        stable_check(f"to_position IN ({POSITIONS})", name="ck_captain_transitions_to_position"),
        stable_check(
            "to_stage_number IS NULL OR to_stage_number > 0",
            name="ck_captain_transitions_to_stage_number",
        ),
        stable_check("event_at_ms >= 0", name="ck_captain_transitions_event_at_ms"),
        stable_check("recorded_at_ms >= 0", name="ck_captain_transitions_recorded_at_ms"),
        stable_check(
            "skipped_unsolved_tasks IN (0, 1)",
            name="ck_captain_transitions_skipped_unsolved_tasks",
        ),
        stable_check(
            "(from_position = 'stage' AND from_stage_number IS NOT NULL) OR "
            "(from_position <> 'stage' AND from_stage_number IS NULL)",
            name="ck_captain_transitions_from_position_stage",
        ),
        stable_check(
            "(to_position = 'stage' AND to_stage_number IS NOT NULL) OR "
            "(to_position <> 'stage' AND to_stage_number IS NULL)",
            name="ck_captain_transitions_to_position_stage",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_captain_transitions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transition_id", name="pk_captain_transitions"),
        sa.UniqueConstraint("user_id", "sequence_number", name="uq_captain_transitions_sequence"),
        sa.UniqueConstraint("source_update_id", name="uq_captain_transitions_source_update_id"),
        sqlite_strict=True,
    )

    op.create_table(
        "task_attempts",
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stage_number", sa.Integer(), nullable=False),
        sa.Column("task_number", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("raw_answer", sa.Text(), nullable=False),
        sa.Column("normalized_answer", sa.Text(), nullable=False),
        sa.Column("event_at_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at_ms", sa.Integer(), nullable=False),
        sa.Column("source_update_id", sa.Integer(), nullable=False),
        stable_check("stage_number > 0", name="ck_task_attempts_stage_number"),
        stable_check("task_number > 0", name="ck_task_attempts_task_number"),
        stable_check("attempt_number > 0", name="ck_task_attempts_attempt_number"),
        stable_check("length(trim(raw_answer)) > 0", name="ck_task_attempts_raw_answer_nonempty"),
        stable_check(
            "length(normalized_answer) > 0",
            name="ck_task_attempts_normalized_answer_nonempty",
        ),
        stable_check("event_at_ms >= 0", name="ck_task_attempts_event_at_ms"),
        stable_check("recorded_at_ms >= 0", name="ck_task_attempts_recorded_at_ms"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_task_attempts_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_task_attempts"),
        sa.UniqueConstraint(
            "user_id",
            "stage_number",
            "task_number",
            "attempt_number",
            name="uq_task_attempts_sequence",
        ),
        sa.UniqueConstraint("source_update_id", name="uq_task_attempts_source_update_id"),
        sqlite_strict=True,
    )
    op.create_index(
        "task_attempts_answer_idx",
        "task_attempts",
        ["user_id", "stage_number", "task_number", "normalized_answer"],
    )


def downgrade() -> None:
    op.drop_index("task_attempts_answer_idx", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_table("captain_transitions")
    op.drop_index("captain_state_position_idx", table_name="captain_state")
    op.drop_table("captain_state")
    op.drop_table("task_prompt_parts")
    op.drop_table("tasks")
    op.drop_table("stages")
    op.drop_table("global_content_parts")
    op.drop_table("score_steps")
    op.drop_table("quest_settings")
    op.drop_index("users_username_nocase_idx", table_name="users")
    op.drop_table("users")
