-- 在 financeclaw_native 执行；先停用索引写入，配合 langgraph.json 和 EMBEDDING_DIMENSIONS=1024。
-- 仅修复从未成功写入向量的旧 1536 维索引；存在向量时拒绝，禁止截断或清空数据。
BEGIN;
SET LOCAL lock_timeout = '5s';
LOCK TABLE public.store_vectors IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE
    current_type text;
BEGIN
    SELECT format_type(atttypid, atttypmod) INTO current_type
    FROM pg_attribute
    WHERE attrelid = 'public.store_vectors'::regclass
      AND attname = 'embedding' AND NOT attisdropped;
    IF current_type = 'vector(1024)' THEN
        RETURN;
    END IF;
    IF current_type IS DISTINCT FROM 'vector(1536)' THEN
        RAISE EXCEPTION 'Unexpected Store vector type: %', current_type;
    END IF;
    IF EXISTS (SELECT 1 FROM public.store_vectors LIMIT 1) THEN
        RAISE EXCEPTION 'Store vectors are not empty; rebuild under a separately reviewed plan';
    END IF;
    ALTER TABLE public.store_vectors
        ALTER COLUMN embedding TYPE vector(1024) USING embedding::vector(1024);
END $$;
COMMIT;
