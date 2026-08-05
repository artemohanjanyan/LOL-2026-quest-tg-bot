CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL CHECK (length(trim(username)) > 0),
    role TEXT NOT NULL CHECK (role IN ('admin', 'captain')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
) STRICT;

CREATE INDEX users_username_nocase_idx
    ON users(username COLLATE NOCASE);

CREATE TABLE quest_settings (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    time_limit_minutes INTEGER NOT NULL DEFAULT 80
        CHECK (time_limit_minutes > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
) STRICT;

INSERT INTO quest_settings (singleton_id, time_limit_minutes, updated_at_ms)
VALUES (1, 80, 0);

CREATE TABLE score_steps (
    attempt_number INTEGER PRIMARY KEY CHECK (attempt_number > 0),
    points INTEGER NOT NULL CHECK (points >= 0)
) STRICT;

INSERT INTO score_steps (attempt_number, points)
VALUES (1, 10), (2, 8), (3, 5), (4, 0);

CREATE TABLE quest_intro_parts (
    part_number INTEGER PRIMARY KEY CHECK (part_number > 0),
    content_type TEXT NOT NULL CHECK (
        content_type IN (
            'text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note'
        )
    ),
    data TEXT NOT NULL CHECK (length(data) > 0),
    caption TEXT
) STRICT;

CREATE TABLE success_outro_parts (
    part_number INTEGER PRIMARY KEY CHECK (part_number > 0),
    content_type TEXT NOT NULL CHECK (
        content_type IN (
            'text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note'
        )
    ),
    data TEXT NOT NULL CHECK (length(data) > 0),
    caption TEXT
) STRICT;

CREATE TABLE timeout_outro_parts (
    part_number INTEGER PRIMARY KEY CHECK (part_number > 0),
    content_type TEXT NOT NULL CHECK (
        content_type IN (
            'text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note'
        )
    ),
    data TEXT NOT NULL CHECK (length(data) > 0),
    caption TEXT
) STRICT;

CREATE TABLE stages (
    stage_number INTEGER PRIMARY KEY CHECK (stage_number > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
) STRICT;

CREATE TABLE tasks (
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    task_number INTEGER NOT NULL CHECK (task_number > 0),
    correct_answer_raw TEXT NOT NULL CHECK (length(trim(correct_answer_raw)) > 0),
    correct_answer_normalized TEXT NOT NULL
        CHECK (length(correct_answer_normalized) > 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    PRIMARY KEY (stage_number, task_number),
    FOREIGN KEY (stage_number) REFERENCES stages(stage_number) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE task_prompt_parts (
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    task_number INTEGER NOT NULL CHECK (task_number > 0),
    part_number INTEGER NOT NULL CHECK (part_number > 0),
    content_type TEXT NOT NULL CHECK (
        content_type IN (
            'text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note'
        )
    ),
    data TEXT NOT NULL CHECK (length(data) > 0),
    caption TEXT,
    PRIMARY KEY (stage_number, task_number, part_number),
    FOREIGN KEY (stage_number, task_number)
        REFERENCES tasks(stage_number, task_number) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

CREATE TABLE captain_state (
    user_id INTEGER PRIMARY KEY,
    position TEXT NOT NULL CHECK (
        position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')
    ),
    started_at_ms INTEGER CHECK (started_at_ms IS NULL OR started_at_ms >= 0),
    position_changed_at_ms INTEGER NOT NULL CHECK (position_changed_at_ms >= 0),
    current_stage_number INTEGER CHECK (
        current_stage_number IS NULL OR current_stage_number > 0
    ),
    terminal_at_ms INTEGER CHECK (terminal_at_ms IS NULL OR terminal_at_ms >= 0),
    timeout_deadline_at_ms INTEGER CHECK (
        timeout_deadline_at_ms IS NULL OR timeout_deadline_at_ms >= 0
    ),
    timeout_limit_minutes INTEGER CHECK (
        timeout_limit_minutes IS NULL OR timeout_limit_minutes > 0
    ),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    CHECK (
        (
            position = 'not_started'
            AND started_at_ms IS NULL
            AND current_stage_number IS NULL
            AND terminal_at_ms IS NULL
            AND timeout_deadline_at_ms IS NULL
            AND timeout_limit_minutes IS NULL
        )
        OR (
            position = 'intro'
            AND started_at_ms IS NOT NULL
            AND current_stage_number IS NULL
            AND terminal_at_ms IS NULL
            AND timeout_deadline_at_ms IS NULL
            AND timeout_limit_minutes IS NULL
        )
        OR (
            position = 'stage'
            AND started_at_ms IS NOT NULL
            AND current_stage_number IS NOT NULL
            AND terminal_at_ms IS NULL
            AND timeout_deadline_at_ms IS NULL
            AND timeout_limit_minutes IS NULL
        )
        OR (
            position = 'finished'
            AND started_at_ms IS NOT NULL
            AND current_stage_number IS NULL
            AND terminal_at_ms IS NOT NULL
            AND timeout_deadline_at_ms IS NULL
            AND timeout_limit_minutes IS NULL
        )
        OR (
            position = 'timed_out'
            AND started_at_ms IS NOT NULL
            AND current_stage_number IS NULL
            AND terminal_at_ms IS NOT NULL
            AND timeout_deadline_at_ms IS NOT NULL
            AND timeout_limit_minutes IS NOT NULL
        )
    )
) STRICT;

CREATE INDEX captain_state_position_idx
    ON captain_state(position, started_at_ms);

CREATE TABLE captain_transitions (
    transition_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    from_position TEXT NOT NULL CHECK (
        from_position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')
    ),
    from_stage_number INTEGER CHECK (
        from_stage_number IS NULL OR from_stage_number > 0
    ),
    to_position TEXT NOT NULL CHECK (
        to_position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')
    ),
    to_stage_number INTEGER CHECK (
        to_stage_number IS NULL OR to_stage_number > 0
    ),
    event_at_ms INTEGER NOT NULL CHECK (event_at_ms >= 0),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
    source_update_id INTEGER UNIQUE,
    skipped_unsolved_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (skipped_unsolved_tasks IN (0, 1)),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE (user_id, sequence_number),
    CHECK (
        (from_position = 'stage' AND from_stage_number IS NOT NULL)
        OR (from_position <> 'stage' AND from_stage_number IS NULL)
    ),
    CHECK (
        (to_position = 'stage' AND to_stage_number IS NOT NULL)
        OR (to_position <> 'stage' AND to_stage_number IS NULL)
    )
) STRICT;

CREATE INDEX captain_transitions_user_idx
    ON captain_transitions(user_id, sequence_number);

CREATE TABLE task_attempts (
    attempt_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    task_number INTEGER NOT NULL CHECK (task_number > 0),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    raw_answer TEXT NOT NULL CHECK (length(trim(raw_answer)) > 0),
    normalized_answer TEXT NOT NULL CHECK (length(normalized_answer) > 0),
    event_at_ms INTEGER NOT NULL CHECK (event_at_ms >= 0),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
    source_update_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE (user_id, stage_number, task_number, attempt_number)
) STRICT;

CREATE INDEX task_attempts_task_idx
    ON task_attempts(user_id, stage_number, task_number, attempt_number);

CREATE INDEX task_attempts_answer_idx
    ON task_attempts(user_id, stage_number, task_number, normalized_answer);
