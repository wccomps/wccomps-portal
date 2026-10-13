from django.db import connection

# Arbitrary, but fixed (and not prepare_database's): every holder asks for the same lock.
SCHOOL_LIST_LOCK_ID = 7_261_640_002


def school_list_lock() -> None:
    """Wait for the school list, then hold it until the transaction commits or rolls back.

    Taken by the registration services' writes (set_team_school, remove_team_school, clear_school_list,
    import_school_list, add_to_school_list, finalize_open_event, clear_event, clear_leftover_data, delete_event,
    delete_season), by Event.open and Event.close, by the start of a first packet send (begin_distribution) and by
    the resend checks (check_resendable, and resend_to_team's contact write). Each takes it before reading the open
    event. It is released at commit, before any packet email goes out: emails are sent outside it. Re-entrant within one
    transaction, so an import can call set_team_school and Event.open.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])
