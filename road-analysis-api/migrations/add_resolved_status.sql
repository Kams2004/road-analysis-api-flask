-- Add resolved status to the review_status enum
ALTER TYPE reviewstatus ADD VALUE IF NOT EXISTS 'resolved';

-- Add resolved_at timestamp column
ALTER TABLE detections ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP;
