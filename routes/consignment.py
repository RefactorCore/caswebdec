from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import login_required, current_user
from models import db, ConsignmentSupplier, ConsignmentReceived, ConsignmentItem, ConsignmentRemittance, CompanyProfile
from routes.decorators import role_required
from routes.utils import paginate_query, log_action
from datetime import datetime, timedelta
from sqlalchemy import func
from decimal import Decimal, ROUND_HALF_UP


consignment_bp = Blueprint('consignment', __name__, url_prefix='/consignment')

# ============================================
# CONSIGNMENT SUPPLIERS
# ============================================

def get_company_profile():
    """Helper to get company profile for templates"""
    return CompanyProfile.query.first()

# Make it available in all consignment templates
@consignment_bp.context_processor
def inject_company():
    return dict(get_company_profile=get_company_profile, datetime=datetime)

    
@consignment_bp.route('/suppliers')
@login_required
@role_required('Admin', 'Accountant')
def suppliers():
    """List all consignment suppliers"""
    search = request.args.get('search', '').strip()
    
    query = ConsignmentSupplier.query
    
    if search:
        query = query.filter(
            (ConsignmentSupplier.name.ilike(f'%{search}%')) |
            (ConsignmentSupplier.tin.ilike(f'%{search}%'))
        )
    
    query = query.order_by(ConsignmentSupplier.is_active.desc(), ConsignmentSupplier.name.asc())
    pagination = paginate_query(query, per_page=20)
    
    safe_args = {k: v for k, v in request.args.items() if k != 'page'}
    
    return render_template(
        'consignment/suppliers.html',
        suppliers=pagination.items,
        pagination=pagination,
        search=search,
        safe_args=safe_args
    )


@consignment_bp.route('/suppliers/add', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def add_supplier():
    """Add a new consignment supplier"""
    try:
        supplier = ConsignmentSupplier(
            name=request.form.get('name'),
            business_type=request.form.get('business_type'),
            tin=request.form.get('tin'),
            address=request.form.get('address'),
            contact_person=request.form.get('contact_person'),
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            default_commission_rate=float(request.form.get('commission_rate', 15)),
            payment_terms_days=int(request.form.get('payment_terms_days', 30)),
            notes=request.form.get('notes')
        )
        
        db.session.add(supplier)
        db.session.commit()
        
        log_action(f'Added consignment supplier: {supplier.name}')
        flash(f'Supplier "{supplier.name}" added successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error adding supplier: {str(e)}', 'danger')
    
    return redirect(url_for('consignment.suppliers'))


@consignment_bp.route('/suppliers/<int:supplier_id>/edit', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def edit_supplier(supplier_id):
    """Edit an existing consignment supplier"""
    supplier = ConsignmentSupplier.query.get_or_404(supplier_id)
    
    try:
        supplier.name = request.form.get('name')
        supplier.business_type = request.form.get('business_type')
        supplier.tin = request.form.get('tin')
        supplier.address = request.form.get('address')
        supplier.contact_person = request.form.get('contact_person')
        supplier.phone = request.form.get('phone')
        supplier.email = request.form.get('email')
        supplier.default_commission_rate = float(request.form.get('commission_rate', 15))
        supplier.payment_terms_days = int(request.form.get('payment_terms_days', 30))
        supplier.notes = request.form.get('notes')
        
        db.session.commit()
        
        log_action(f'Updated consignment supplier: {supplier.name}')
        flash(f'Supplier "{supplier.name}" updated successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating supplier: {str(e)}', 'danger')
    
    return redirect(url_for('consignment.suppliers'))


@consignment_bp.route('/suppliers/<int:supplier_id>/toggle', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def toggle_supplier(supplier_id):
    """Toggle supplier active status"""
    supplier = ConsignmentSupplier.query.get_or_404(supplier_id)
    
    supplier.is_active = not supplier.is_active
    db.session.commit()
    
    status = "activated" if supplier.is_active else "deactivated"
    log_action(f'{status.capitalize()} consignment supplier: {supplier.name}')
    flash(f'Supplier "{supplier.name}" {status}!', 'success')
    
    return redirect(url_for('consignment.suppliers'))


# ============================================
# RECEIVE CONSIGNMENT
# ============================================

@consignment_bp.route('/receive', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def receive():
    """Receive new consignment goods"""
    if request.method == 'POST':
        try:
            supplier_id = int(request.form.get('supplier_id'))
            commission_rate = float(request.form.get('commission_rate', 15))
            expected_return_days = request.form.get('expected_return_days')
            notes = request.form.get('notes')
            items_json = request.form.get('items_json')
            
            # Parse items
            import json
            items = json.loads(items_json) if items_json else []
            
            if not items:
                flash('Please add at least one item to the consignment.', 'warning')
                return redirect(url_for('consignment.receive'))
            
            # Get supplier
            supplier = ConsignmentSupplier.query.get_or_404(supplier_id)
            
            # Generate receipt number
            from models import CompanyProfile
            profile = CompanyProfile.query.first()
            
            if not hasattr(profile, 'next_consignment_number') or profile.next_consignment_number is None:
                profile.next_consignment_number = 1
            
            receipt_num = profile.next_consignment_number
            profile.next_consignment_number += 1
            receipt_number = f"CONS-{receipt_num:06d}"
            
            # Calculate expected return date
            expected_return_date = None
            if expected_return_days:
                expected_return_date = datetime.utcnow() + timedelta(days=int(expected_return_days))
            
            # Create consignment
            consignment = ConsignmentReceived(
                receipt_number=receipt_number,
                supplier_id=supplier_id,
                date_received=datetime.utcnow(),
                expected_return_date=expected_return_date,
                commission_rate=commission_rate,
                notes=notes,
                created_by_id=current_user.id
            )
            
            db.session.add(consignment)
            db.session.flush()
            
            # Add items
            total_items = 0
            total_value = 0.0
            
            for item_data in items:
                qty = int(item_data.get('quantity', 0))
                # ✅ IMPROVED: Use Decimal for financial calculations
                price = Decimal(str(item_data.get('retail_price', 0))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                
                if qty <= 0 or price <= 0:
                    continue
                
                item = ConsignmentItem(
                    consignment_id=consignment.id,
                    sku=item_data.get('sku'),
                    product_name=item_data.get('name'),
                    description=item_data.get('description'),
                    barcode=item_data.get('barcode'),
                    quantity_received=qty,
                    retail_price=float(price)  # Convert back to float for storage
                )
                
                db.session.add(item)
                
                total_items += qty
                total_value += float(qty * price)
            
            # Update consignment totals
            consignment.total_items = total_items
            consignment.total_value = total_value
            
            db.session.commit()
            
            log_action(f'Received consignment {receipt_number} from {supplier.name} - {total_items} items, ₱{total_value:,.2f}')
            flash(f'✅ Consignment {receipt_number} received successfully!', 'success')
            
            return redirect(url_for('consignment.view_consignment', consignment_id=consignment.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f'❌ Error receiving consignment: {str(e)}', 'danger')
            return redirect(url_for('consignment.receive'))
    
    # GET request - show form
    suppliers = ConsignmentSupplier.query.filter_by(is_active=True).order_by(ConsignmentSupplier.name).all()
    return render_template('consignment/receive.html', suppliers=suppliers)


# ============================================
# LIST & VIEW CONSIGNMENTS
# ============================================

@consignment_bp.route('/list')
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def list_received():
    """List all received consignments"""
    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', 'all')
    
    query = ConsignmentReceived.query
    
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    
    if search:
        query = query.join(ConsignmentSupplier).filter(
            (ConsignmentReceived.receipt_number.ilike(f'%{search}%')) |
            (ConsignmentSupplier.name.ilike(f'%{search}%'))
        )
    
    query = query.order_by(ConsignmentReceived.date_received.desc())
    pagination = paginate_query(query, per_page=20)
    
    safe_args = {k: v for k, v in request.args.items() if k != 'page'}
    
    return render_template(
        'consignment/list.html',
        consignments=pagination.items,
        pagination=pagination,
        search=search,
        status_filter=status_filter,
        safe_args=safe_args
    )


@consignment_bp.route('/view/<int:consignment_id>')
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def view_consignment(consignment_id):
    """View detailed consignment information"""
    consignment = ConsignmentReceived.query.get_or_404(consignment_id)
    items = ConsignmentItem.query.filter_by(consignment_id=consignment_id).all()
    
    # --- ADD THIS CALCULATION ---
    # Calculate total paid from all remittances for this consignment
    total_paid = db.session.query(func.sum(ConsignmentRemittance.amount_paid))\
        .filter(ConsignmentRemittance.consignment_id == consignment.id)\
        .scalar() or 0.0
    
    return render_template(
        'consignment/view.html',
        consignment=consignment,
        items=items,
        total_paid=total_paid  # <-- Pass the calculated value here
    )


@consignment_bp.route('/item/<int:item_id>/adjust', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def adjust_item(item_id):
    """Mark items as damaged (cannot be sold or returned)"""
    item = ConsignmentItem.query.get_or_404(item_id)
    
    try:
        qty_damaged = int(request.form.get('quantity_damaged', 0))
        damage_reason = request.form.get('damage_reason', '').strip()
        
        # Validate quantity
        if qty_damaged < 0:
            flash('Damaged quantity cannot be negative.', 'danger')
            return redirect(url_for('consignment.view_consignment', consignment_id=item.consignment_id))
        
        # Validate total doesn't exceed available
        max_can_damage = item.quantity_received - item.quantity_sold - item.quantity_returned
        if qty_damaged > max_can_damage:
            flash(
                f'Error: Cannot mark {qty_damaged} as damaged. '
                f'Maximum available to damage: {max_can_damage} '
                f'(Received: {item.quantity_received}, Sold: {item.quantity_sold}, Already Returned: {item.quantity_returned})',
                'danger'
            )
            return redirect(url_for('consignment.view_consignment', consignment_id=item.consignment_id))
            
        # Update damaged quantity
        item.quantity_damaged = qty_damaged
        
        db.session.commit()
        
        reason_text = f" - Reason: {damage_reason}" if damage_reason else ""
        log_action(
            f'Marked {qty_damaged} units of {item.product_name} as damaged on consignment {item.consignment.receipt_number}{reason_text}'
        )
        flash(
            f'✅ Marked {qty_damaged} units of "{item.product_name}" as damaged. '
            f'Available for sale/return: {item.quantity_available}',
            'success'
        )
        
    except ValueError as e:
        db.session.rollback()
        flash(f'Invalid input: {str(e)}', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating item: {str(e)}', 'danger')
    
    return redirect(url_for('consignment.view_consignment', consignment_id=item.consignment_id))


# ADD THIS NEW ROUTE for processing payment remittance
@consignment_bp.route('/consignment/<int:consignment_id>/remit', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def remit_payment(consignment_id):
    """Complete settlement: Return unsold items and remit payment to supplier"""
    consignment = ConsignmentReceived.query.get_or_404(consignment_id)
    
    try:
        amount_paid = float(request.form.get('amount_paid'))
        payment_method = request.form.get('payment_method', 'Cash')
        reference_number = request.form.get('reference_number', '').strip()
        notes = request.form.get('notes', '').strip()
        
        # Validate payment amount
        if amount_paid <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('consignment.view_consignment', consignment_id=consignment_id))
        
        # Calculate totals
        total_already_paid = db.session.query(func.sum(ConsignmentRemittance.amount_paid))\
            .filter(ConsignmentRemittance.consignment_id == consignment.id)\
            .scalar() or 0.0
        
        amount_due = consignment.get_amount_due_to_supplier()
        remaining_due = amount_due - total_already_paid
        
        # Warn if overpayment
        if amount_paid > remaining_due + 0.01:
            flash(
                f'⚠️ Warning: Payment amount (₱{amount_paid:,.2f}) exceeds remaining due (₱{remaining_due:,.2f}).',
                'warning'
            )
        
        # ✅ FIX: Calculate returns BEFORE modifying quantities
        total_returned = 0
        items_being_returned = []  # Track for receipt
        
        for item in consignment.items:
            # Calculate available BEFORE any modifications
            current_available = item.quantity_received - item.quantity_sold - item.quantity_returned - item.quantity_damaged
            
            if current_available > 0:
                # Store details for receipt
                items_being_returned.append({
                    'sku': item.sku,
                    'name': item.product_name,
                    'quantity': current_available,
                    'retail_price': item.retail_price,
                    'total_value': current_available * item.retail_price
                })
                
                # ✅ FIX: Set the final returned quantity (not +=)
                item.quantity_returned = item.quantity_received - item.quantity_sold - item.quantity_damaged
                total_returned += current_available
                
                log_action(
                    f'Auto-returned {current_available} units of {item.product_name} '
                    f'on settlement of {consignment.receipt_number}'
                )
        
        # Create remittance record with detailed info
        settlement_notes = f"Returned {total_returned} unsold items. "
        if reference_number:
            settlement_notes = f"Ref: {reference_number}. " + settlement_notes
        if notes:
            settlement_notes += notes
        
        remittance = ConsignmentRemittance(
            consignment_id=consignment.id,
            amount_paid=amount_paid,
            payment_method=payment_method,
            notes=settlement_notes,
            created_by_id=current_user.id
        )
        db.session.add(remittance)
        db.session.flush()  # Get remittance ID
        
        # Update consignment status
        new_total_paid = total_already_paid + amount_paid
        if new_total_paid >= amount_due - 0.01:
            consignment.status = 'Closed'
            status_msg = '✅ Consignment fully settled and closed!'
        else:
            consignment.status = 'Partial'
            status_msg = f'✅ Partial payment recorded. Remaining: ₱{(amount_due - new_total_paid):,.2f}'
        
        # Create journal entry
        from models import JournalEntry
        from routes.utils import get_system_account_code
        import json
        
        je_lines = [
            {
                'account_code': get_system_account_code('Consignment Payable'),
                'debit': float(amount_paid),
                'credit': 0
            },
            {
                'account_code': get_system_account_code('Cash'),
                'debit': 0,
                'credit': float(amount_paid)
            }
        ]
        
        journal_entry = JournalEntry(
            description=f'Settlement for {consignment.receipt_number}: Paid {consignment.supplier.name} ₱{amount_paid:,.2f}, Returned {total_returned} items',
            entries_json=json.dumps(je_lines)
        )
        db.session.add(journal_entry)
        
        db.session.commit()
        
        log_action(
            f'Completed settlement for {consignment.receipt_number}: '
            f'Paid ₱{amount_paid:,.2f}, Returned {total_returned} items. '
            f'Total paid: ₱{new_total_paid:,.2f} / ₱{amount_due:,.2f}'
        )
        
        flash(status_msg, 'success')
        flash(f'📦 Returned {total_returned} unsold items to supplier.', 'info')
        
        # ✅ NEW: Store settlement details in session for receipt
        from flask import session
        session['last_settlement'] = {
            'remittance_id': remittance.id,
            'consignment_id': consignment.id,
            'receipt_number': consignment.receipt_number,
            'supplier_name': consignment.supplier.name,
            'date': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'items_returned': items_being_returned,
            'total_returned': total_returned,
            'amount_paid': amount_paid,
            'payment_method': payment_method,
            'reference_number': reference_number
        }
        
        # Redirect to settlement receipt
        return redirect(url_for('consignment.settlement_receipt', remittance_id=remittance.id))

    except ValueError as e:
        db.session.rollback()
        flash(f'Invalid payment amount: {str(e)}', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error processing settlement: {str(e)}', 'danger')
    
    return redirect(url_for('consignment.view_consignment', consignment_id=consignment_id))


@consignment_bp.route('/settlement-receipt/<int:remittance_id>')
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def settlement_receipt(remittance_id):
    """Display settlement receipt showing returned items and payment"""
    remittance = ConsignmentRemittance.query.get_or_404(remittance_id)
    consignment = remittance.consignment
    
    # Get all items with their final quantities
    items = ConsignmentItem.query.filter_by(consignment_id=consignment.id).all()
    
    # Calculate totals
    total_received = sum(item.quantity_received for item in items)
    total_sold = sum(item.quantity_sold for item in items)
    total_returned = sum(item.quantity_returned for item in items)
    total_damaged = sum(item.quantity_damaged for item in items)
    
    # Get all remittances for this consignment
    all_remittances = ConsignmentRemittance.query.filter_by(consignment_id=consignment.id)\
        .order_by(ConsignmentRemittance.date_paid).all()
    
    total_paid = sum(r.amount_paid for r in all_remittances)
    
    # Calculate financial summary
    total_sold_value = consignment.get_total_sold_value()
    commission_earned = consignment.get_commission_earned()
    amount_due_total = consignment.get_amount_due_to_supplier()
    
    return render_template(
        'consignment/settlement_receipt.html',
        remittance=remittance,
        consignment=consignment,
        items=items,
        total_received=total_received,
        total_sold=total_sold,
        total_returned=total_returned,
        total_damaged=total_damaged,
        total_paid=total_paid,
        total_sold_value=total_sold_value,
        commission_earned=commission_earned,
        amount_due_total=amount_due_total,
        all_remittances=all_remittances
    )