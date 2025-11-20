from flask import Blueprint, render_template, request, flash, redirect, url_for
from models import db, Account
from flask_login import login_required
from .decorators import role_required
from models import JournalEntry, Account # Make sure Account is imported
import json
from datetime import datetime # Import datetime
from routes.utils import log_action # Import the log_action utility
from flask_login import current_user # Import current_user
from .utils import log_action

accounts_bp = Blueprint('accounts', __name__, url_prefix='/accounts')

SYSTEM_ACCOUNT_NAMES = [
    'Cash', 'Accounts Receivable', 'Inventory', 'Creditable Withholding Tax',
    'Accounts Payable', 'Opening Balance Equity', 'Sales Revenue', 'Sales Returns',
    'COGS', 'VAT Payable', 'VAT Input', 'Inventory Loss', 'Inventory Gain'
]

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
    if acc.code != new_code: changes.append(f'code from "{acc.code}" to "{new_code}"')
    if acc.name != new_name: changes.append(f'name from "{acc.name}" to "{new_name}"')
    if acc.type != new_type: changes.append(f'type from "{acc.type}" to "{new_type}"')

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
    total_debit = 0.0
    total_credit = 0.0

    # Process each line
    for i in range(len(account_codes)):
        code = account_codes[i]
        try:
            debit = float(debits[i] or 0.0)
            credit = float(credits[i] or 0.0)
        except ValueError:
            flash('Invalid debit/credit amount.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if not code:
            flash('All lines must have an account selected.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))
            
        if debit < 0 or credit < 0:
            flash('Debit and credit amounts cannot be negative.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))

        if debit > 0 and credit > 0:
            flash('A single line cannot have both a debit and a credit.', 'danger')
            return redirect(url_for('accounts.new_journal_entry_form'))
            
        if debit > 0 or credit > 0:
            je_lines.append({
                'account_code': code,
                'debit': round(debit, 2),
                'credit': round(credit, 2)
            })
            total_debit += debit
            total_credit += credit

    # --- CRITICAL VALIDATION ---
    if not je_lines:
        flash('Cannot create an empty journal entry.', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))
        
    if round(total_debit, 2) != round(total_credit, 2):
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
        
        # Log this action
        log_action(f'Created manual journal entry #{je.id} for "{description}" with total ₱{total_debit:,.2f}.', user=current_user)
        
        db.session.commit()
        flash('Manual journal entry created successfully.', 'success')
        
        # Redirect to the main journal list
        return redirect(url_for('core.journal_entries'))

    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred: {str(e)}', 'danger')
        return redirect(url_for('accounts.new_journal_entry_form'))