CREATE TABLE task_correct_answers (
    stage_number INTEGER NOT NULL CHECK (stage_number > 0),
    task_number INTEGER NOT NULL CHECK (task_number > 0),
    answer_number INTEGER NOT NULL CHECK (answer_number > 0),
    raw_answer TEXT NOT NULL CHECK (length(trim(raw_answer)) > 0),
    normalized_answer TEXT NOT NULL CHECK (length(normalized_answer) > 0),
    PRIMARY KEY (stage_number, task_number, answer_number),
    UNIQUE (stage_number, task_number, normalized_answer),
    FOREIGN KEY (stage_number, task_number)
        REFERENCES tasks(stage_number, task_number) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

INSERT INTO task_correct_answers (
    stage_number, task_number, answer_number, raw_answer, normalized_answer
)
SELECT stage_number, task_number, 1, correct_answer_raw, correct_answer_normalized
FROM tasks;

DROP VIEW task_progress;

ALTER TABLE tasks DROP COLUMN correct_answer_raw;

ALTER TABLE tasks DROP COLUMN correct_answer_normalized;

CREATE VIEW task_progress AS
SELECT users.user_id,
       tasks.stage_number,
       tasks.task_number,
       min(task_attempts.attempt_number) AS attempt_number
FROM users
CROSS JOIN tasks
LEFT JOIN task_attempts
  ON task_attempts.user_id = users.user_id
 AND task_attempts.stage_number = tasks.stage_number
 AND task_attempts.task_number = tasks.task_number
 AND EXISTS (
     SELECT 1
     FROM task_correct_answers
     WHERE task_correct_answers.stage_number = tasks.stage_number
       AND task_correct_answers.task_number = tasks.task_number
       AND task_correct_answers.normalized_answer = task_attempts.normalized_answer
 )
GROUP BY users.user_id, tasks.stage_number, tasks.task_number;
