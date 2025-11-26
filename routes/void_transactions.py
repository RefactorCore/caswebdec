from flask import Blueprint, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from models import (db, Sale, Purchase, ARInvoice, APInvoice, Payment, 
                   JournalEntry, StockAdjustment, Product, InventoryLot, SaleItem, 
                   InventoryTransaction, ARInvoiceItem, ConsignmentSale, 
                   ConsignmentItem, ConsignmentRemittance)
from datetime import datetime
import json
from .decorators import role_required
from .utils import log_action, get_system_account_code
from routes.fifo_utils import reverse_inventory_consumption
from sqlalchemy import func
from decimal import Decimal, ROUND_HALF_UP

void_bp = Blueprint('void', __name__, url_prefix='/void')

def to_decimal(value):
    """Coerce value (None, float, int, str, Decimal) -> Decimal quantized to 2dp."""
    if value is None:
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

def create_reversing_je(original_je, description_prefix, void_reason):
    """
    Create an explicit reversing JE that swaps debit/credit lines of original_je.
    IMPORTANT: We do NOT mark original_je as voided_at. 
    Both the Original (Debit) and Reversal (Credit) must remain active 
    for the Financial Reports to calculate a Net Zero balance.
    """
    try:
        try:
            original_entries = original_je.entries()
        except Exception:
            original_entries = json.loads(getattr(original_je, 'entries_json', '[]') or '[]')

        reversed_entries = []
        for entry in original_entries:
            acct = entry.get('account_code')
            debit = to_decimal(entry.get('debit', '0.00'))
            credit = to_decimal(entry.get('credit', '0.00'))
            
            # Swap debit / credit
            reversed_entries.append({
                'account_code': acct,
                'debit': format(credit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '0.2f'),
                'credit': format(debit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '0.2f')
            })

        reversing_je = JournalEntry(
            description=f'[REVERSAL] {description_prefix} - {void_reason}',
            entries_json=json.dumps(reversed_entries),
            created_at=datetime.utcnow()
        )
        db.session.add(reversing_je)
        
        original_je.description = f"{original_je.description} [REVERSED]"
        original_je.void_reason = f"Reversed by JE (Reason: {void_reason})"

        db.session.add(reversing_je)
        return reversing_je

    except Exception as e:
        print(f"Error creating reversing JE: {e}")
        return None

# --- 1. VOID SALE (Updated for Consignment & Inventory Fixes) ---
@void_bp.route('/sale/<int:sale_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant', 'Cashier')
def void_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    
    if sale.voided_at:
        flash('This sale has already been voided.', 'warning')
        return redirect(url_for('core.sales'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.sales'))
    
    try:
        # --- A. Consignment Payment Check ---
        consignment_sale = ConsignmentSale.query.filter_by(sale_id=sale.id).first()
        if consignment_sale and consignment_sale.payment_status == 'Paid':
             flash('Cannot void sale: The consignment supplier has already been paid for these items.', 'danger')
             return redirect(url_for('core.sales'))

        # --- B. Reverse Inventory (Standard FIFO) ---
        # We capture the result to know which products were actually restored
        reversed_qtys_summary = reverse_inventory_consumption(sale_id=sale.id)
        
        # --- C. Reverse Consignment Specifics ---
        if consignment_sale:
            for sale_item in sale.items:
                # 1. Find the linked Consignment Item
                # Try match by Product ID first
                c_item = ConsignmentItem.query.filter_by(
                    consignment_id=consignment_sale.consignment_id,
                    sku=sale_item.sku
                ).first()
                
                if c_item:
                    qty = sale_item.qty
                    
                    # 2. Update Consignment Counts
                    actual_reversal = min(c_item.quantity_sold, qty)
                    c_item.quantity_sold -= actual_reversal
                    
                    # 3. Fix for "POS Product Not Reversed" (Issue #2)
                    # If this item was NOT restored by FIFO reversal (likely because it had no lots),
                    # we must manually increment the Product master quantity.
                    product_id_key = sale_item.product_id
                    if product_id_key and product_id_key not in reversed_qtys_summary:
                        product = Product.query.get(product_id_key)
                        if product:
                            product.quantity += qty
            
            # Update status
            consignment_sale.payment_status = 'Voided'

            consignment = consignment_sale.consignment
            total_sold = sum(item.quantity_sold for item in consignment.items)
            total_returned = sum(item.quantity_returned for item in consignment.items)
            total_received = consignment.total_items

            if total_sold + total_returned >= total_received:
                consignment.status = 'Closed'
            elif total_sold > 0 or total_returned > 0:
                consignment.status = 'Partial'
            else:
                consignment.status = 'Active'

        # --- D. Reverse Financials ---
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Sale #{sale.id}%'),
            JournalEntry.voided_at.is_(None)
        ).first()
        
        if original_je:
            create_reversing_je(original_je, f'Sale #{sale.id} ({sale.document_number})', void_reason)
        
        # --- E. Mark Sale Void ---
        sale.voided_at = datetime.utcnow()
        sale.voided_by = current_user.id
        sale.void_reason = void_reason
        sale.status = 'Voided'
        
        log_action(f'Voided Sale #{sale.id} ({sale.document_number}). Reason: {void_reason}')
        
        db.session.commit()
        flash(f'Sale #{sale.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding sale: {str(e)}', 'danger')
    
    return redirect(url_for('core.sales'))

# --- 2. VOID PURCHASE ---
@void_bp.route('/purchase/<int:purchase_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_purchase(purchase_id):
    purchase = Purchase.query.get_or_404(purchase_id)

    if purchase.voided_at:
        flash('This purchase has already been voided.', 'warning')
        return redirect(url_for('core.purchases'))

    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.purchases'))

    try:
        active_payments = Payment.query.filter(
            Payment.ref_type == 'Purchase',
            Payment.ref_id == purchase.id,
            Payment.voided_at.is_(None)
        ).all()

        sum_active = sum((to_decimal(getattr(p, 'amount', 0)) for p in active_payments), Decimal('0.00'))

        if to_decimal(purchase.paid) != sum_active:
            purchase.paid = sum_active

        if sum_active > Decimal('0.00'):
            flash(f'Cannot void purchase. There are active payments totaling ₱{sum_active:,.2f}. Please void the payments first.', 'danger')
            return redirect(request.referrer or url_for('core.purchases'))

        for item in purchase.items:
            lots = InventoryLot.query.filter_by(purchase_id=purchase.id, purchase_item_id=item.id).all()
            for lot in lots:
                consumed = InventoryTransaction.query.filter(InventoryTransaction.lot_id == lot.id).first()
                if consumed:
                    flash(f'Cannot void purchase: inventory from this purchase has been used/sold (Product: {item.product_name}).', 'danger')
                    return redirect(url_for('core.purchases'))

        for item in purchase.items:
            lots = InventoryLot.query.filter_by(purchase_id=purchase.id, purchase_item_id=item.id).all()
            for lot in lots:
                db.session.delete(lot)

            product = Product.query.get(item.product_id)
            if product:
                try:
                    product.quantity = max(0, int(product.quantity) - int(item.qty))
                except Exception:
                    product.quantity = max(0, (product.quantity or 0) - (item.qty or 0))

        original_purchase_je = None
        if hasattr(purchase, 'journal_entry_id') and purchase.journal_entry_id:
            original_purchase_je = JournalEntry.query.get(purchase.journal_entry_id)
            if original_purchase_je and original_purchase_je.voided_at is not None:
                original_purchase_je = None

        if not original_purchase_je:
            original_purchase_je = JournalEntry.query.filter(
                JournalEntry.description.ilike(f'%Purchase #{purchase.id}%'),
                JournalEntry.voided_at.is_(None)
            ).order_by(JournalEntry.created_at.asc()).first()

        if original_purchase_je:
            create_reversing_je(original_purchase_je, f'Purchase #{purchase.id} ({purchase.supplier})', void_reason)

        purchase.voided_at = datetime.utcnow()
        purchase.voided_by = current_user.id
        purchase.void_reason = void_reason
        purchase.status = 'Voided'
        purchase.paid = Decimal('0.00')

        log_action(f'Voided Purchase #{purchase.id}. Reason: {void_reason}')
        db.session.commit()

        flash(f'Purchase #{purchase.id} has been voided successfully.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding purchase: {str(e)}', 'danger')

    return redirect(url_for('core.purchases'))


# --- 3. VOID AR INVOICE ---
@void_bp.route('/ar-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ar_invoice(invoice_id):
    invoice = ARInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.billing_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.billing_invoices'))

    try:
        active_payments = Payment.query.filter(
            Payment.ref_type.in_(['AR', 'ARInvoice']),
            Payment.ref_id == invoice.id,
            Payment.voided_at.is_(None)
        ).all()

        sum_active = Decimal('0.00')
        for p in active_payments:
            sum_active += to_decimal(getattr(p, 'amount', 0)) + to_decimal(getattr(p, 'wht_amount', 0))

        if to_decimal(invoice.paid) != sum_active:
            invoice.paid = sum_active

        if sum_active > Decimal('0.00'):
            flash(f'Cannot void invoice. There are active payments totaling ₱{sum_active:,.2f}. Please void the payments first.', 'danger')
            return redirect(request.referrer or url_for('ar_ap.billing_invoices'))
        
        reverse_inventory_consumption(ar_invoice_id=invoice.id)
        
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Billing Invoice {invoice.invoice_number}%')
        ).filter(JournalEntry.voided_at.is_(None)).first()
        
        if original_je:
            create_reversing_je(original_je, f'Billing Invoice {invoice.invoice_number}', void_reason)
            
            cogs_je = JournalEntry.query.filter(
                JournalEntry.description.like(f'%COGS for AR Invoice {invoice.invoice_number}%')
            ).filter(JournalEntry.voided_at.is_(None)).first()

            if cogs_je:
                create_reversing_je(cogs_je, f'COGS for AR Invoice {invoice.invoice_number}', void_reason)
        
        invoice.voided_at = datetime.utcnow()
        invoice.voided_by = current_user.id
        invoice.void_reason = void_reason
        invoice.status = 'Voided'
        invoice.paid = Decimal('0.00')
        
        log_action(f'Voided AR Invoice {invoice.invoice_number}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Invoice {invoice.invoice_number} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding invoice: {str(e)}', 'danger')
    
    return redirect(url_for('ar_ap.billing_invoices'))

# --- 4. VOID AP INVOICE (Auto-Void Payments) ---
@void_bp.route('/ap-invoice/<int:invoice_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_ap_invoice(invoice_id):
    invoice = APInvoice.query.get_or_404(invoice_id)
    
    if invoice.voided_at:
        flash('This invoice has already been voided.', 'warning')
        return redirect(url_for('ar_ap.ap_invoices'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('ar_ap.ap_invoices'))

    try:
        active_payments = Payment.query.filter(
            Payment.ref_type.in_(['AP', 'APInvoice']),
            Payment.ref_id == invoice.id,
            Payment.voided_at.is_(None)
        ).all()

        if active_payments:
            for payment in active_payments:
                original_payment_je = JournalEntry.query.filter(
                    JournalEntry.description.like(f'%Payment for AP #{payment.ref_id}%'),
                    JournalEntry.voided_at.is_(None)
                ).first()
                
                if not original_payment_je:
                    original_payment_je = JournalEntry.query.filter(
                        JournalEntry.description.like(f'%Payment for APInvoice #{payment.ref_id}%'),
                        JournalEntry.voided_at.is_(None)
                    ).first()

                if original_payment_je:
                    create_reversing_je(original_payment_je, f'Auto-Void Payment #{payment.id}', f'Linked to AP Void #{invoice.id}')

                payment.voided_at = datetime.utcnow()
                payment.voided_by = current_user.id
                payment.void_reason = f"Auto-voided with AP Invoice #{invoice.id} ({void_reason})"
                
                log_action(f'Auto-voided Payment #{payment.id} due to AP Invoice #{invoice.id} void.')

        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%AP Invoice #{invoice.id}%'),
            JournalEntry.voided_at.is_(None)
        ).first()
        
        if original_je:
            create_reversing_je(original_je, f'AP Invoice #{invoice.id} ({invoice.invoice_number})', void_reason)
        
        invoice.voided_at = datetime.utcnow()
        invoice.voided_by = current_user.id
        invoice.void_reason = void_reason
        invoice.status = 'Voided'
        invoice.paid = Decimal('0.00')
        
        log_action(f'Voided AP Invoice #{invoice.id} ({invoice.invoice_number}). Reason: {void_reason}')
        db.session.commit()
        
        if active_payments:
            flash(f'AP Invoice #{invoice.id} and {len(active_payments)} linked payment(s) have been voided successfully.', 'success')
        else:
            flash(f'AP Invoice #{invoice.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding invoice: {str(e)}', 'danger')
    
    return redirect(url_for('ar_ap.ap_invoices'))


# --- 5. VOID PAYMENT ---
@void_bp.route('/payment/<int:payment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)

    if payment.voided_at:
        flash('This payment has already been voided.', 'warning')
        return redirect(request.referrer or url_for('core.index'))

    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.index'))

    try:
        ref_type = (payment.ref_type or '').strip()
        ref_type_normalized = None
        if ref_type in ('AR', 'ARInvoice'):
            ref_type_normalized = 'AR'
        elif ref_type in ('AP', 'APInvoice'):
            ref_type_normalized = 'AP'
        elif ref_type == 'Purchase':
            ref_type_normalized = 'Purchase'
        else:
            ref_type_normalized = ref_type

        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Payment for {payment.ref_type} #{payment.ref_id}%'),
            JournalEntry.voided_at.is_(None)
        ).first()

        if not original_je and ref_type_normalized in ('AR', 'AP'):
            original_je = JournalEntry.query.filter(
                JournalEntry.description.like(f'%Payment for {ref_type_normalized} #{payment.ref_id}%'),
                JournalEntry.voided_at.is_(None)
            ).first()

        if original_je:
            create_reversing_je(original_je, f'Payment #{payment.id} for {payment.ref_type} #{payment.ref_id}', void_reason)

        payment.voided_at = datetime.utcnow()
        payment.voided_by = current_user.id
        payment.void_reason = void_reason

        db.session.flush()

        if ref_type_normalized == 'AR':
            invoice = ARInvoice.query.get(payment.ref_id)
            if invoice:
                active_payments = Payment.query.filter(
                    Payment.ref_type.in_(['AR', 'ARInvoice']),
                    Payment.ref_id == invoice.id,
                    Payment.voided_at.is_(None)
                ).all()
                sum_active = Decimal('0.00')
                for p in active_payments:
                    sum_active += to_decimal(getattr(p, 'amount', 0)) + to_decimal(getattr(p, 'wht_amount', 0))
                invoice.paid = sum_active
                if to_decimal(invoice.paid) == Decimal('0.00'):
                    invoice.status = 'Open'
                elif to_decimal(invoice.paid) < to_decimal(invoice.total):
                    invoice.status = 'Partially Paid'

        elif ref_type_normalized == 'AP':
            invoice = APInvoice.query.get(payment.ref_id)
            if invoice:
                active_payments = Payment.query.filter(
                    Payment.ref_type.in_(['AP', 'APInvoice']),
                    Payment.ref_id == invoice.id,
                    Payment.voided_at.is_(None)
                ).all()
                sum_active = sum((to_decimal(getattr(p, 'amount', 0)) for p in active_payments), Decimal('0.00'))
                invoice.paid = sum_active
                if to_decimal(invoice.paid) == Decimal('0.00'):
                    invoice.status = 'Open'
                elif to_decimal(invoice.paid) < to_decimal(invoice.total):
                    invoice.status = 'Partially Paid'

        elif ref_type_normalized == 'Purchase':
            purchase = Purchase.query.get(payment.ref_id)
            if purchase:
                active_payments = Payment.query.filter(
                    Payment.ref_type == 'Purchase',
                    Payment.ref_id == purchase.id,
                    Payment.voided_at.is_(None)
                ).all()
                sum_active = sum((to_decimal(getattr(p, 'amount', 0)) for p in active_payments), Decimal('0.00'))
                purchase.paid = sum_active
                if to_decimal(purchase.paid) == Decimal('0.00'):
                    purchase.status = 'Open'
                elif to_decimal(purchase.paid) < to_decimal(purchase.total):
                    purchase.status = 'Partial'

        log_action(f'Voided Payment #{payment.id} for {payment.ref_type} #{payment.ref_id}. Reason: {void_reason}')
        db.session.commit()

        flash(f'Payment #{payment.id} has been voided successfully.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding payment: {str(e)}', 'danger')

    return redirect(request.referrer or url_for('core.index'))


# --- 6. VOID STOCK ADJUSTMENT ---
@void_bp.route('/stock-adjustment/<int:adjustment_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_stock_adjustment(adjustment_id):
    adjustment = StockAdjustment.query.get_or_404(adjustment_id)
    
    if adjustment.voided_at:
        flash('This adjustment has already been voided.', 'warning')
        return redirect(url_for('core.inventory'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.inventory'))
    
    try:
        product = adjustment.product

        if adjustment.quantity_changed < 0:
            reverse_inventory_consumption(adjustment_id=adjustment.id)
            lot_total = db.session.query(func.coalesce(func.sum(InventoryLot.quantity_remaining), 0)).filter(InventoryLot.product_id == product.id).scalar() or 0
            product.quantity = int(lot_total)

        else:
            lots = InventoryLot.query.filter_by(adjustment_id=adjustment.id).all()
            for lot in lots:
                consumed = InventoryTransaction.query.filter(InventoryTransaction.lot_id == lot.id).first()
                if consumed:
                    flash(f'Cannot void adjustment: The stock added by this adjustment has already been sold or used (Lot #{lot.id}).', 'danger')
                    return redirect(url_for('core.inventory'))

            for lot in lots:
                db.session.delete(lot)
            
            db.session.flush()

            lot_total = db.session.query(func.coalesce(func.sum(InventoryLot.quantity_remaining), 0)).filter(InventoryLot.product_id == product.id).scalar() or 0
            product.quantity = int(lot_total)

        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Stock Adjustment #{adjustment.id}%'),
            JournalEntry.voided_at.is_(None)
        ).first()
        
        if original_je:
            create_reversing_je(original_je, f'Void Stock Adj #{adjustment.id}', void_reason)
        else:
            flash(f'Inventory restored, but linked Journal Entry not found for Adj #{adjustment.id}. Please check GL manually.', 'warning')

        adjustment.voided_at = datetime.utcnow()
        adjustment.voided_by = current_user.id
        adjustment.void_reason = void_reason
        
        log_action(f'Voided Stock Adjustment #{adjustment.id} for {product.name}. Reason: {void_reason}')
        db.session.commit()
        
        if original_je:
            flash(f'Stock Adjustment #{adjustment.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding adjustment: {str(e)}', 'danger')
    
    return redirect(url_for('core.inventory'))

# --- 7. VOID JOURNAL ENTRY ---
@void_bp.route('/journal-entry/<int:je_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_journal_entry(je_id):
    journal_entry = JournalEntry.query.get_or_404(je_id)
    
    if journal_entry.voided_at:
        flash('This journal entry has already been voided.', 'warning')
        return redirect(url_for('core.journal_entries'))
    
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer or url_for('core.journal_entries'))
    
    try:
        create_reversing_je(journal_entry, f'JE #{journal_entry.id}', void_reason)
        
        journal_entry.voided_at = datetime.utcnow()
        journal_entry.voided_by = current_user.id
        journal_entry.void_reason = void_reason
        
        log_action(f'Voided Journal Entry #{journal_entry.id}. Reason: {void_reason}')
        db.session.commit()
        
        flash(f'Journal Entry #{journal_entry.id} has been voided successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding JE: {str(e)}', 'danger')
    
    return redirect(url_for('core.journal_entries'))

# --- 8. VOID CONSIGNMENT REMITTANCE (For Fix #2) ---
@void_bp.route('/consignment-remittance/<int:remittance_id>', methods=['POST'])
@login_required
@role_required('Admin', 'Accountant')
def void_consignment_remittance(remittance_id):
    remittance = ConsignmentRemittance.query.get_or_404(remittance_id)
    
    if remittance.voided_at:
        flash('This remittance has already been voided.', 'warning')
        return redirect(request.referrer)
        
    void_reason = request.form.get('void_reason', '').strip()
    if not void_reason:
        flash('Void reason is required.', 'danger')
        return redirect(request.referrer)
        
    try:
        original_je = JournalEntry.query.filter(
            JournalEntry.description.like(f'%Settlement for {remittance.consignment.receipt_number}%'),
            JournalEntry.voided_at.is_(None)
        ).first()
        
        if original_je:
            create_reversing_je(original_je, f'Remittance #{remittance.id}', void_reason)
            
        linked_sales = ConsignmentSale.query.filter_by(
            consignment_id=remittance.consignment_id, 
            payment_status='Paid'
        ).all()
        
        count_reset = 0
        for cs in linked_sales:
            # Only reset sales that happened before or during this remittance
            if cs.sale.created_at <= remittance.created_at:
                cs.payment_status = 'Pending'
                count_reset += 1
        
        remittance.voided_at = datetime.utcnow()
        remittance.voided_by = current_user.id
        remittance.void_reason = void_reason

        remittance.consignment.status = 'Partial'
        
        log_action(f'Voided Consignment Remittance #{remittance.id}. Sales reset: {count_reset}')
        db.session.commit()
        
        flash(f'Remittance #{remittance.id} voided. {count_reset} sales marked back to Pending.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error voiding remittance: {str(e)}', 'danger')
        
    return redirect(request.referrer)