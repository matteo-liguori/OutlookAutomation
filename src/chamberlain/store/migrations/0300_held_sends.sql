-- A one-off send grant can hold a drafted reply for the owner: it names the card action
-- (waiting for the owner) that asks them to approve it. Once they confirm it in the viewer,
-- the harness sends exactly that text and finishes the action. Existing grants name none.
ALTER TABLE grants ADD COLUMN action TEXT;
