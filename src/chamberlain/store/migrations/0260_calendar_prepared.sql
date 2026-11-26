-- Convert active calendar plans once, in the same transaction as the migration ledger.
-- A nonterminal plan is needed for recovery: never fabricate an identity or retry a write.
-- Refuse unknown/malformed plans so the pre-migration backup remains recoverable.
CREATE TEMP TABLE calendar_plan_guard (ok INTEGER NOT NULL CHECK (ok = 1)) STRICT;

INSERT INTO calendar_plan_guard (ok)
SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM operations AS o
    LEFT JOIN sources AS s ON s.id = o.source_id
    WHERE o.kind IN ('calendar.create', 'calendar.update', 'calendar.delete', 'calendar.respond')
      AND o.state IN ('proposed', 'authorised', 'prepared', 'dispatched', 'uncertain', 'reconciling')
      AND o.prepared_json IS NOT NULL
      AND COALESCE(s.connector, '') != 'demo'
      AND CASE WHEN json_valid(o.prepared_json) IS NOT 1 THEN 1
          WHEN json_type(o.prepared_json, '$.data') IS NOT 'object' THEN 1
          WHEN json_extract(o.prepared_json, '$.source') IS NOT o.source_id THEN 1
          WHEN json_extract(o.prepared_json, '$.intent_kind') IS NOT o.kind THEN 1
          WHEN json_type(o.prepared_json, '$.data.payload') = 'object' THEN 0
          WHEN json_type(o.prepared_json, '$.data.payload') IS NOT NULL THEN 1
          WHEN COALESCE(s.connector, '') NOT IN ('thunderbird', 'caldav', 'google_calendar') THEN 1
          WHEN o.kind = 'calendar.create' AND
               (json_type(o.prepared_json, '$.reconcile_token') IS NOT 'text' OR
                length(json_extract(o.prepared_json, '$.reconcile_token')) = 0 OR
                (o.reconcile_token IS NOT NULL AND
                 json_extract(o.prepared_json, '$.reconcile_token') IS NOT o.reconcile_token) OR
                json_type(o.prepared_json, '$.data.fields') IS NOT 'object') THEN 1
          WHEN o.kind = 'calendar.update' AND json_type(o.prepared_json, '$.data.fields') IS NOT 'object' THEN 1
          WHEN o.kind != 'calendar.create' AND
               (json_type(o.prepared_json, '$.data.expected_etag') IS NOT 'text' OR
                length(json_extract(o.prepared_json, '$.data.expected_etag')) = 0) THEN 1
          WHEN s.connector = 'thunderbird' THEN CASE
              WHEN o.kind = 'calendar.respond' THEN 1
              WHEN json_type(o.prepared_json, '$.data.event_id') IS NOT 'text' THEN 1
              WHEN length(json_extract(o.prepared_json, '$.data.event_id')) = 0 THEN 1
              WHEN o.kind IN ('calendar.create', 'calendar.update') AND
                   json_type(o.prepared_json, '$.data.icalendar') IS NOT 'text' THEN 1
              WHEN o.kind IN ('calendar.update', 'calendar.delete') AND
                   json_type(o.prepared_json, '$.data.expected_icalendar') IS NOT 'text' THEN 1
              ELSE 0 END
          WHEN s.connector = 'caldav' THEN CASE
              WHEN json_type(o.prepared_json, '$.data.name') IS NOT 'text' THEN 1
              WHEN length(json_extract(o.prepared_json, '$.data.name')) = 0 THEN 1
              WHEN o.kind IN ('calendar.create', 'calendar.update', 'calendar.respond') AND
                   json_type(o.prepared_json, '$.data.ics') IS NOT 'text' THEN 1
              WHEN o.kind = 'calendar.delete' AND
                   json_type(o.prepared_json, '$.data.occurrence') = 'text' AND
                   json_type(o.prepared_json, '$.data.ics') IS NOT 'text' THEN 1
              WHEN o.kind = 'calendar.respond' AND
                   (json_type(o.prepared_json, '$.data.response') IS NOT 'text' OR
                    json_type(o.prepared_json, '$.data.address') IS NOT 'text') THEN 1
              ELSE 0 END
          WHEN s.connector = 'google_calendar' THEN CASE
              WHEN o.kind != 'calendar.create' AND
                   json_type(o.prepared_json, '$.data.locator') IS NOT 'text' THEN 1
              WHEN o.kind != 'calendar.create' AND
                   length(json_extract(o.prepared_json, '$.data.locator')) = 0 THEN 1
              WHEN o.kind IN ('calendar.create', 'calendar.update') AND
                   json_type(o.prepared_json, '$.data.body') IS NOT 'object' THEN 1
              WHEN o.kind = 'calendar.respond' AND
                   (json_type(o.prepared_json, '$.data.response') IS NOT 'text' OR
                    json_type(o.prepared_json, '$.data.attendees') IS NOT 'array') THEN 1
              ELSE 0 END
          ELSE 1 END = 1
) THEN 0 ELSE 1 END;

DROP TABLE calendar_plan_guard;

UPDATE operations AS o
SET prepared_json = json_set(
    o.prepared_json,
    '$.data', json_object(
        'payload', json(CASE s.connector
            WHEN 'thunderbird' THEN json_set(
                json_remove(json_extract(o.prepared_json, '$.data'), '$.fields', '$.expected_etag'),
                '$.kind', substr(o.kind, 10)
            )
            WHEN 'google_calendar' THEN CASE WHEN o.kind = 'calendar.respond' THEN json_set(
                json_remove(json_extract(o.prepared_json, '$.data'),
                    '$.fields', '$.expected_etag', '$.locator', '$.attendees'),
                '$.body', json_object('attendees', json_extract(o.prepared_json, '$.data.attendees')),
                '$.notify', json('true')
            ) ELSE json_remove(json_extract(o.prepared_json, '$.data'),
                '$.fields', '$.expected_etag', '$.locator') END
            ELSE json_remove(json_extract(o.prepared_json, '$.data'), '$.fields', '$.expected_etag')
        END),
        'locator', CASE WHEN o.kind = 'calendar.create' THEN NULL
            WHEN s.connector = 'thunderbird' THEN json_extract(o.prepared_json, '$.data.event_id')
            WHEN s.connector = 'google_calendar' THEN json_extract(o.prepared_json, '$.data.locator')
            WHEN json_type(o.prepared_json, '$.data.occurrence') = 'text' THEN
                json_extract(o.prepared_json, '$.data.name') || '#' ||
                json_extract(o.prepared_json, '$.data.occurrence')
            ELSE json_extract(o.prepared_json, '$.data.name') END,
        'expected_etag', json_extract(o.prepared_json, '$.data.expected_etag'),
        'fields', json_extract(o.prepared_json, '$.data.fields')
    )
)
FROM sources AS s
WHERE s.id = o.source_id
  AND s.connector IN ('thunderbird', 'caldav', 'google_calendar')
  AND o.kind IN ('calendar.create', 'calendar.update', 'calendar.delete', 'calendar.respond')
  AND o.state IN ('proposed', 'authorised', 'prepared', 'dispatched', 'uncertain', 'reconciling')
  AND o.prepared_json IS NOT NULL
  AND json_type(o.prepared_json, '$.data.payload') IS NULL;
