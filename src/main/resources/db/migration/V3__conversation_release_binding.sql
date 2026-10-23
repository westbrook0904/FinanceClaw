-- Preserve the exact release of the most recently admitted Turn. A missing historical
-- manifest must block execution; this migration cannot reconstruct it from a hash.
ALTER TABLE business.conversations ADD COLUMN release_hash text;

UPDATE business.conversations c SET release_hash = latest.release_hash
FROM (
    SELECT DISTINCT ON (conversation_id) conversation_id, release_hash
    FROM business.turns ORDER BY conversation_id, created_at DESC, id DESC
) latest
WHERE c.id = latest.conversation_id;

-- Legacy empty conversations bind on their first admission. New conversations bind
-- in their creation transaction. P1 upgrades use a new conversation, not an overwrite.
CREATE FUNCTION business.guard_conversation_release() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.release_hash IS NOT NULL AND NEW.release_hash IS DISTINCT FROM OLD.release_hash THEN
        RAISE EXCEPTION 'conversation release is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER conversation_release_immutable
BEFORE UPDATE OF release_hash ON business.conversations
FOR EACH ROW EXECUTE FUNCTION business.guard_conversation_release();
