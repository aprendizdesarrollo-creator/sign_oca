# Copyright 2026
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

def migrate(cr, version):
    """Remove the obsolete source-record links from signature requests.

    ``record_ref`` was only a convenience link to the record used to create a
    request.  It is intentionally not part of the new standalone-document
    model.  The template's ``model_id`` is kept because it still controls
    template generation from records.
    """
    cr.execute("ALTER TABLE sign_oca_request DROP COLUMN IF EXISTS record_ref")
    cr.execute("ALTER TABLE sign_oca_request_signer DROP COLUMN IF EXISTS model")
    cr.execute("ALTER TABLE sign_oca_request_signer DROP COLUMN IF EXISTS res_id")
    cr.execute(
        """
        DELETE FROM ir_model_fields
         WHERE (model = 'sign.oca.request' AND name = 'record_ref')
            OR (model = 'sign.oca.request.signer' AND name IN ('model', 'res_id'))
        """,
    )
