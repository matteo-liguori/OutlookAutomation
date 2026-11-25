-- 0220 u-settle: a write left unfinished in an old harness's journal is closed, not reviewed.
-- The old harness owns its own journals (both run side by side), and the owner is never asked to
-- settle them. Imported operations hold no stored request, so nothing could ever resend one.
-- They become `abandoned` (terminal, frees nothing) with a receipt, so they drop out of every
-- list of work. The harness's audit chain is not extended here (SQL cannot sign a row); the
-- import report and this note are the record. Open `operation.uncertain` events for them close
-- on the next tick, because the detector no longer reports them.
UPDATE operations
SET state = 'abandoned',
    settled_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    lease_owner = NULL,
    lease_expires_at = NULL,
    receipt_json = json_object(
        'summary', json_object(
            'intent_kind', kind,
            'source', source_id,
            'state', 'abandoned',
            'intent_sha256', intent_sha256,
            'settled_at', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            'verification', json_object('client_verified', json('false'), 'server_verified', json('false')),
            'items_ok', 0,
            'items_failed', 0),
        'items', json('[]'),
        'warnings', json('[]'))
WHERE state IN ('uncertain', 'reconciling')
  AND intent_json IS NULL
  AND idempotency_key LIKE 'import:%';
