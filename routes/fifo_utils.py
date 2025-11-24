"""
FIFO Inventory Costing Utilities
"""
from models import db, InventoryLot, InventoryTransaction, Product
from sqlalchemy import func
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28


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


def create_inventory_lot(product_id, quantity, unit_cost, purchase_id=None,
                         purchase_item_id=None, adjustment_id=None, movement_id=None, is_opening_balance=False):
    """
    Create a new inventory lot when receiving inventory.
    Accepts optional movement_id to link the lot to an InventoryMovement (receive).
    """
    if quantity <= 0:
        raise ValueError("Quantity must be positive")

    unit_cost = to_decimal(unit_cost)
    if unit_cost < Decimal('0.00'):
        raise ValueError("Unit cost cannot be negative")

    lot = InventoryLot(
        product_id=product_id,
        quantity_remaining=int(quantity),
        unit_cost=unit_cost,
        purchase_id=purchase_id,
        purchase_item_id=purchase_item_id,
        adjustment_id=adjustment_id,
        movement_id=movement_id,
        is_opening_balance=is_opening_balance,
        created_at=datetime.utcnow()
    )

    db.session.add(lot)
    return lot


def consume_inventory_fifo(product_id, quantity_needed, sale_id=None, sale_item_id=None,
                           ar_invoice_id=None, ar_invoice_item_id=None, adjustment_id=None):
    """
    Consume inventory using FIFO method and return total COGS.

    Args:
        product_id: ID of the product
        quantity_needed: Number of units to consume
        sale_id: Reference to sale (optional)
        sale_item_id: Reference to sale item (optional)
        ar_invoice_id: Reference to AR invoice (optional)
        ar_invoice_item_id: Reference to AR invoice item (optional)
        adjustment_id: Reference to stock adjustment (optional)

    Returns:
        tuple: (total_cogs (Decimal), list of InventoryTransaction objects)

    Raises:
        ValueError: If insufficient inventory
    """
    if quantity_needed <= 0:
        raise ValueError("Quantity must be positive")

    # Get product to verify total quantity
    product = Product.query.get(product_id)
    if not product:
        raise ValueError(f"Product {product_id} not found")

    if product.quantity < int(quantity_needed):
        raise ValueError(
            f"Insufficient inventory for {product.name}. "
            f"Available: {product.quantity}, Requested: {quantity_needed}"
        )

    # Get oldest lots first (FIFO). Use row-level locking to prevent concurrent consumption races.
    query = InventoryLot.query.filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).order_by(InventoryLot.created_at.asc())

    # Acquire SELECT ... FOR UPDATE (works on engines that support row locking)
    try:
        lots = query.with_for_update().all()
    except Exception:
        # Fallback if with_for_update not supported in this environment
        lots = query.all()

    if not lots:
        raise ValueError(f"No inventory lots found for product {product_id}")

    total_cogs = Decimal('0.00')
    remaining_to_consume = int(quantity_needed)
    transactions = []

    for lot in lots:
        if remaining_to_consume <= 0:
            break

        # Determine how much to take from this lot
        qty_from_lot = int(min(lot.quantity_remaining, remaining_to_consume))
        if qty_from_lot <= 0:
            continue

        unit_cost = to_decimal(lot.unit_cost)
        cost_from_lot = (to_decimal(qty_from_lot) * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Create transaction record
        transaction = InventoryTransaction(
            lot_id=lot.id,
            quantity_used=qty_from_lot,
            unit_cost=unit_cost,
            total_cost=cost_from_lot,
            sale_id=sale_id,
            sale_item_id=sale_item_id,
            ar_invoice_id=ar_invoice_id,
            ar_invoice_item_id=ar_invoice_item_id,
            adjustment_id=adjustment_id,
            created_at=datetime.utcnow()
        )
        db.session.add(transaction)
        # flush ensures transaction.id and persistence within the same tx (but not committing)
        try:
            db.session.flush()
        except Exception:
            # ignore flush errors here; caller will handle commit/rollback
            pass

        transactions.append(transaction)

        # Update lot
        lot.quantity_remaining = int(lot.quantity_remaining) - qty_from_lot

        # Accumulate COGS
        total_cogs += cost_from_lot
        remaining_to_consume -= qty_from_lot

    if remaining_to_consume > 0:
        raise ValueError(
            f"Could not consume {quantity_needed} units. "
            f"Only {int(quantity_needed) - remaining_to_consume} available in lots."
        )

    total_cogs = total_cogs.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return total_cogs, transactions


def get_fifo_cost(product_id, quantity):
    """
    Calculate what the COGS would be for a given quantity without consuming.
    Useful for estimates and previews.

    Args:
        product_id: ID of the product
        quantity: Number of units

    Returns:
        Decimal: Estimated COGS
    """
    lots = InventoryLot.query.filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).order_by(InventoryLot.created_at.asc()).all()

    total_cost = Decimal('0.00')
    remaining = int(quantity)

    for lot in lots:
        if remaining <= 0:
            break
        qty_from_lot = int(min(lot.quantity_remaining, remaining))
        total_cost += (to_decimal(qty_from_lot) * to_decimal(lot.unit_cost))
        remaining -= qty_from_lot

    total_cost = total_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return total_cost


def get_weighted_average_cost(product_id):
    """
    Calculate the current weighted average cost for a product.
    This is useful for display purposes and reporting.

    Args:
        product_id: ID of the product

    Returns:
        Decimal: Weighted average cost per unit
    """
    result = db.session.query(
        func.coalesce(func.sum(InventoryLot.quantity_remaining * InventoryLot.unit_cost), 0),
        func.coalesce(func.sum(InventoryLot.quantity_remaining), 0)
    ).filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).first()

    total_value, total_qty = result

    total_value = to_decimal(total_value)
    total_qty = int(total_qty or 0)

    if total_qty == 0:
        return Decimal('0.00')

    avg = (total_value / Decimal(total_qty)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return avg


def get_inventory_lots_summary(product_id):
    """
    Get a summary of all active inventory lots for a product.
    """
    lots = InventoryLot.query.filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).order_by(InventoryLot.created_at.asc()).all()

    summary = []
    for lot in lots:
        unit_cost = to_decimal(lot.unit_cost)
        total_value = (to_decimal(lot.quantity_remaining) * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        summary.append({
            'lot_id': lot.id,
            'quantity': int(lot.quantity_remaining),
            'unit_cost': unit_cost,
            'total_value': total_value,
            'created_at': lot.created_at,
            'age_days': (datetime.utcnow() - lot.created_at).days,
            'is_opening_balance': lot.is_opening_balance,
            'movement_id': getattr(lot, 'movement_id', None),
            'purchase_id': getattr(lot, 'purchase_id', None)
        })

    return summary


def reconcile_inventory_lots(product_id):
    """
    Reconcile inventory lots with the product quantity.
    Returns discrepancies if any.

    Args:
        product_id: ID of the product

    Returns:
        dict: Reconciliation results
    """
    product = Product.query.get(product_id)
    if not product:
        return {'error': 'Product not found'}

    lot_total = db.session.query(
        func.coalesce(func.sum(InventoryLot.quantity_remaining), 0)
    ).filter(
        InventoryLot.product_id == product_id
    ).scalar() or 0

    lot_total = int(lot_total)
    discrepancy = int(product.quantity) - lot_total

    return {
        'product_quantity': int(product.quantity),
        'lot_total': lot_total,
        'discrepancy': discrepancy,
        'is_balanced': discrepancy == 0
    }


def reverse_inventory_consumption(sale_id=None, ar_invoice_id=None, adjustment_id=None, movement_id=None):
    """
    Reverse FIFO inventory consumption for voided transactions.
    Restores inventory lots and deletes the consumption records.

    Args:
        sale_id: ID of the voided sale
        ar_invoice_id: ID of the voided AR invoice
        adjustment_id: ID of the stock adjustment or movement (consume calls may have set adjustment_id)
        movement_id: alias for adjustment_id when inventory movement used same field

    Returns:
        dict: Summary of reversed quantities by product
    """
    # Build query for affected InventoryTransaction rows
    query = InventoryTransaction.query

    if sale_id:
        query = query.filter(InventoryTransaction.sale_id == sale_id)
    elif ar_invoice_id:
        query = query.filter(InventoryTransaction.ar_invoice_id == ar_invoice_id)
    elif adjustment_id:
        query = query.filter(InventoryTransaction.adjustment_id == adjustment_id)
    elif movement_id:
        query = query.filter(InventoryTransaction.adjustment_id == movement_id)
    else:
        raise ValueError("Must provide either sale_id, ar_invoice_id, adjustment_id, or movement_id")

    transactions = query.all()

    reversed_summary = {}

    for trans in transactions:
        # Restore the lot quantity
        lot = InventoryLot.query.get(trans.lot_id)
        if lot:
            lot.quantity_remaining = int(lot.quantity_remaining) + int(trans.quantity_used)

            # Track what we reversed
            product_id = lot.product_id
            if product_id not in reversed_summary:
                reversed_summary[product_id] = 0
            reversed_summary[product_id] += int(trans.quantity_used)

        # Delete the transaction record
        db.session.delete(trans)

    # After restoration, sync Product.quantity for affected products
    for pid in list(reversed_summary.keys()):
        total_remaining = db.session.query(func.coalesce(func.sum(InventoryLot.quantity_remaining), 0)).filter(InventoryLot.product_id == pid).scalar() or 0
        prod = Product.query.get(pid)
        if prod:
            prod.quantity = int(total_remaining)

    # Flush so callers can rely on in-session state prior to commit
    try:
        db.session.flush()
    except Exception:
        pass

    return reversed_summary