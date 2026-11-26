-- Preserve the exact historical approval payload and its hash. Only its execution
-- representation becomes a one-item batch. Never fabricate recovery evidence.
UPDATE operations
SET refs_json = json_set(coalesce(refs_json, '{}'), '$._original_move_intent', intent_json),
    intent_json = json_remove(
        json_set(intent_json, '$.messages', json(CASE
            WHEN json_type(intent_json, '$.message') = 'text'
            THEN json_array(json_extract(intent_json, '$.message'))
            ELSE json_extract(intent_json, '$.messages') END)),
        '$.message', '$.internet_message_id')
WHERE kind IN ('mail.move', 'mail.trash') AND intent_json IS NOT NULL
  AND (json_type(intent_json, '$.message') IS NOT NULL
       OR json_type(intent_json, '$.internet_message_id') IS NOT NULL)
  AND (json_type(intent_json, '$.message') = 'text' OR json_type(intent_json, '$.messages') = 'array');
