ALTER TABLE users RENAME COLUMN username TO display_name;

DROP INDEX users_username_nocase_idx;

CREATE INDEX users_display_name_nocase_idx
    ON users(display_name COLLATE NOCASE);

UPDATE users
SET display_name = '@' || display_name
WHERE display_name NOT LIKE '@%';
