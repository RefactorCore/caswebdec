from flask import request, abort
from flask_login import current_user
from models import db, AuditLog, Account
from functools import lru_cache
from flask_caching import Cache

cache = Cache()

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

cache = Cache(config={'CACHE_TYPE': 'simple'})

# --- NEW FUNCTION TO REMOVE MAGIC NUMBERS ---
@cache.memoize(timeout=3600)  # Cache for 1 hour
def get_system_account_code(name):
    """Retrieve account code by name with caching."""
    account = Account.query.filter_by(name=name).first()
    if not account:
        abort(500, f"Critical system account '{name}' not found.")
    return account.code