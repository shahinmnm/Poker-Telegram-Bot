CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO system_config (key, value)
SELECT 'system_constants', to_jsonb(value)
FROM (
    SELECT jsonb_build_object(
        'lock_manager', jsonb_build_object(
            'enable_fine_grained_locks', false,
            'rollout_percentage', 0
        )
    ) AS value
) AS initial
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION update_system_config_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER system_config_timestamp
BEFORE UPDATE ON system_config
FOR EACH ROW
EXECUTE FUNCTION update_system_config_timestamp();
