from functools import wraps
from flask_login import current_user
from flask import flash, redirect, url_for

def role_required(*roles):
    """
    Custom decorator to restrict access to users with specific roles.
    Example: @role_required('Admin', 'Accountant')
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                # This should be handled by @login_required, but as a fallback
                return redirect(url_for('core.login'))
            if current_user.role not in roles:
                flash('You do not have permission to access this page.', 'danger')
                return redirect(url_for('core.index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator