ALTER TABLE requests
  ADD COLUMN IF NOT EXISTS client_trace_id TEXT;
