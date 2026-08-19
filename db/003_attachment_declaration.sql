-- Record HOW the attachment claim was reached, not just what it was.
--
-- The claim is a workflow-level fact — "this case says it has logs attached" —
-- so it belongs on the workflow, not on attachment_event, which is one row per
-- file. Until now `declared_attached` reached bronze.raw_event's payload and
-- then stopped: nothing in silver read it, so the verdict was unqueryable.
--
-- Three companions come with it because a NULL verdict has two very different
-- causes that were previously indistinguishable once the data left the share:
--
--   declaration_source      '' means record_attachment_declaration() never ran,
--                           i.e. nobody pressed Click AI on the attachment page.
--                           'select_attachments_ai_summary' means it did run.
--   declaration_confidence  how the verdict was reached — 'explicit_field' when
--                           the summary named it outright, 'sentence' when it
--                           was inferred from prose, 'none' when the AI looked
--                           and found nothing to go on.
--   declaration_conflict    true when two statements in the same summary
--                           disagreed, which needs a human, not a re-run.
--
-- Measured on the live share at the time of writing: 23 of 25 workflows have
-- declared = NULL with the source key absent entirely — the AI was never run.
-- Zero had the AI run and fail to decide. Those two populations need opposite
-- follow-up (change the UI vs. improve the prompt), and without these columns
-- they are the same number.

BEGIN;

ALTER TABLE avatar_silver_workflow
    ADD COLUMN declared_attached      boolean NULL,
    ADD COLUMN declaration_source     text NOT NULL DEFAULT '',
    ADD COLUMN declaration_confidence text NOT NULL DEFAULT '',
    ADD COLUMN declaration_conflict   boolean NOT NULL DEFAULT false,
    -- A verdict can only exist if something produced it. Guards against a
    -- future writer setting the boolean while leaving its provenance blank,
    -- which would recreate exactly the ambiguity this migration removes.
    ADD CONSTRAINT avatar_silver_workflow_declaration_ck CHECK (
        declared_attached IS NULL OR declaration_source <> '');

COMMENT ON COLUMN avatar_silver_workflow.declaration_source IS
'Empty means the attachment AI never ran for this workflow; a non-empty value
names the step that produced the verdict. Distinguishes "not asked" from
"asked and could not tell".';

COMMIT;
