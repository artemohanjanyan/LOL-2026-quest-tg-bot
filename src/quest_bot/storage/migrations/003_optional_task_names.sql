ALTER TABLE tasks ADD COLUMN name TEXT
    CHECK (name IS NULL OR length(trim(name)) > 0);
