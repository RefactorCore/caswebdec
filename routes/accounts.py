from flask import Blueprint, render_template, request, flash, redirect, url_for
from models import db, Account, JournalEntry
from flask_login import login_required, current_user
from .decorators import role_required
import json
from datetime import datetime
from routes.utils import log_action
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28

accounts_bp = Blueprint('accounts', __name__, url_prefix='/accounts')

SYSTEM_ACCOUNT_NAMES = [
    'Cash', 'Accounts Receivable', 'Inventory', 'Creditable Withholding Tax',
    'Accounts Payable', 'Opening Balance Equity', 'Sales Revenue', 'Sales Returns',
    'COGS', 'VAT Payable', 'VAT Input', 'Inventory Loss', 'Inventory Gain'
]


def to_decimal(value):
    """Coerce value (None, float, int, str, Decimal) -> Decimal quantized to 2dp."""
    if value is None or value == '':
        return Decimal('0.00')
    if isinstance(value, Decimal):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if isinstance(value, int):
        return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if isinstance(value, float):
        try:
            d = Decimal(str(value))
        except Exception:
            return Decimal('0.00')
        return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    try:
        d = Decimal(value)
    except Exception:
        try:
            d = Decimal(str(value))
        except Exception:
            return Decimal('0.00')
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


@accounts_bp.route('/')
@login_required
@role_required('Admin', 'Accountant')
def chart_of_accounts():
    """Display and manage the Chart of Accounts."""
    accounts = Account.query.order_by(Account.code).all()
    return render_template(
        'chart_of_accounts.html',
        accounts=accounts,
        system_accounts=SYSTEM_ACCOUNT_NAMES
    )


@accounts_bp.route('/add', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def add_account():
    """Add a new account."""
    code = request.form.get('code')
    name = request.form.get('name')
    type = request.form.get('type')

    if not code or not name or not type:
        flash('All fields are required.', 'danger')
        return redirect(url_for('accounts.chart_of_accounts'))

    if Account.query.filter_by(code=code).first() or Account.query.filter_by(name=name).first():
        flash('Account code or name already exists.', 'danger')
        return redirect(url_for('accounts.chart_of_accounts'))

    new_account = Account(code=code, name=name, type=type)
    db.session.add(new_account)
    log_action(f'Created new account: {code} - {name} ({type}).')
    db.session.commit()
    flash('Account added successfully.', 'success')
    return redirect(url_for('accounts.chart_of_accounts'))


@accounts_bp.route('/update/<int:id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def update_account(id):
    acc = Account.query.get_or_404(id)

    new_code = request.form.get('code')
    new_name = request.form.get('name')
    new_type = request.form.get('type')

    # --- ADD THIS SERVER-SIDE VALIDATION ---
    # Check if this is a system account and if the name is being changed
    if acc.name in SYSTEM_ACCOUNT_NAMES and new_name != acc.name:
        flash(f'Cannot change the name of a critical system account ("{acc.name}").', 'danger')
        return redirect(url_for('accounts.chart_of_accounts'))
    # --- END OF NEW VALIDATION ---

    # Check for duplicate code (if changed)
    if new_code != acc.code and Account.query.filter_by(code=new_code).first():
        flash(f'Account code {new_code} already exists.', 'danger')
        return redirect(url_for('accounts.chart_of_accounts'))

    # Check for duplicate name (if changed)
    if new_name != acc.name and Account.query.filter_by(name=new_name).first():
        flash(f'Account name {new_name} already exists.', 'danger')
        return redirect(url_for('accounts.chart_of_accounts'))

    # Log what changed
    changes = []
    if acc.code != new_code:
        changes.append(f'code from "{acc.code}" to "{new_code}"')
    if acc.name != new_name:
        changes.append(f'name from "{acc.name}" to "{new_name}"')
    if acc.type != new_type:
        changes.append(f'type from "{acc.type}" to "{new_type}"')

    acc.code = new_code
    acc.name = new_name
    acc.type = new_type

    if changes:
        log_action(f'Updated account {acc.id}: Changed {", ".join(changes)}.')

    db.session.commit()
    flash('Account updated successfully.', 'success')
    return redirect(url_for('accounts.chart_of_accounts'))


# --- ADD THIS ROUTE TO SHOW THE NEW JE FORM ---
@accounts_bp.route('/journal/new', methods=['GET'])
@login_required
@role_required('Admin', 'Accountant')
def new_journal_entry_form():
    """Display the form for creating a new manual journal entry."""
    accounts = Account.query.order_by(Account.code).all()
    return render_template('new_journal_entry.html', accounts=accounts)


# --- ADD THIS ROUTE TO SAVE THE NEW JE ---
@accounts_bp.route('/journal/new', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def create_journal_entry():
    """Save a new manual journal entry."""
    description = request.form.get('description')
    date_str = request.form.get('date')

    # Get the lists of inputs
    account_codes = request.form.getlist('account_code[]')
    debits = request.form.getlist('debit[]')
    credits = request.form.getlist('credit[]')

    if not description or not date_str:
        flash('Description and Date are required.', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))

    # Parse the date
    try:
        entry_date = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        flash('Invalid date format. Please use YYYY-MM-DD.', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))

    je_lines = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')

    # Process each line
    for i in range(len(account_codes)):
        code = account_codes[i]
        try:
            debit = to_decimal(debits[i] if i < len(debits) else '0')
            credit = to_decimal(credits[i] if i < len(credits) else '0')
        except Exception:
            flash('Invalid debit/credit amount.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if not code:
            flash('All lines must have an account selected.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if debit < Decimal('0.00') or credit < Decimal('0.00'):
            flash('Debit and credit amounts cannot be negative.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if debit > Decimal('0.00') and credit > Decimal('0.00'):
            flash('A single line cannot have both a debit and a credit.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if debit > Decimal('0.00') or credit > Decimal('0.00'):
            # Store amounts as formatted strings to preserve exact values in the JE JSON
            je_lines.append({
                'account_code': code,
                'debit': format(debit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '0.2f'),
                'credit': format(credit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '0.2f')
            })
            total_debit += debit
            total_credit += credit

    # --- CRITICAL VALIDATION ---
    if not je_lines:
        flash('Cannot create an empty journal entry.', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))

    if total_debit.quantize(Decimal('0.01')) != total_credit.quantize(Decimal('0.01')):
        flash(f'Entry is unbalanced. Total Debits (₱{total_debit:,.2f}) do not equal Total Credits (₱{total_credit:,.2f}).', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))

    try:
        # Create and save the new Journal Entry
        je = JournalEntry(
            description=f"[Manual] {description}",
            entries_json=json.dumps(je_lines),
            created_at=entry_date  # Use the user-provided date
        )
        db.session.add(je)
        # flush so we can reference je.id in logs
        db.session.flush()

        # Log this action (pass current_user)
        log_action(f'Created manual journal entry #{je.id} for \"{description}\" with total ₱{total_debit:,.2f}.', user=current_user)

        db.session.commit()
        flash('Manual journal entry created successfully.', 'success')

        # Redirect to the main journal list
        return redirect(url_for('core.journal_entries'))

    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {str(e)}', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))