from flask import request, abort
from flask_login import current_user
from models import db, AuditLog, Account
from functools import lru_cache


def paginate_query(query, per_page=20):
    """Paginate SQLAlchemy query based on ?page= parameter."""
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return pagination

def log_action(action_description, user=None):
    """A helper function to easily create an audit log entry."""
    # Use the provided user object first, otherwise fall back to current_user
    user_to_log = user or (current_user if current_user.is_authenticated else None)

    log = AuditLog(
        user_id=user_to_log.id if user_to_log else None,
        action=action_description,
        ip_address=request.remote_addr
    )
    db.session.add(log)


# --- NEW FUNCTION TO REMOVE MAGIC NUMBERS ---
@lru_cache(maxsize=None) # Caches results for high performance
def get_system_account_code(name):
    """
    Fetches the account code for a critical system account by its name.
    Caches the result to avoid database lookups on every transaction.
    If the account is not found, it will abort the request
    because the system cannot proceed without it.
    """
    account = Account.query.filter_by(name=name).first()
    if not account:
        # Abort with a 500 error. The frontend will see this.
        # This is critical to stop a bad transaction.
        abort(500, f"Critical system account '{name}' not found. Please configure it in the Chart of Accounts.")
    return account.code