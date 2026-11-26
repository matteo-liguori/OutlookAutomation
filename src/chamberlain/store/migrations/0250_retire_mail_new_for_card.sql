-- Card suggestions now travel in the source's mail batch.
DELETE FROM events WHERE kind = 'mail.new_for_card';
