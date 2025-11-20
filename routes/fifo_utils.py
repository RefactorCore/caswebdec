"""
FIFO Inventory Costing Utilities
"""
from models import db, InventoryLot, InventoryTransaction, Product
from sqlalchemy import func
from datetime import datetime


def create_inventory_lot(product_id, quantity, unit_cost, purchase_id=None, 
                         purchase_item_id=None, adjustment_id=None, movement_id=None, is_opening_balance=False):
    """
    Create a new inventory lot when receiving inventory.
    Accepts optional movement_id to link the lot to an InventoryMovement (receive).
    """
    if quantity <= 0:
        raise ValueError("Quantity must be positive")

    if unit_cost < 0:
        raise ValueError("Unit cost cannot be negative")
    
    lot = InventoryLot(
        product_id=product_id,
        quantity_remaining=quantity,
        unit_cost=unit_cost,
        purchase_id=purchase_id,
        purchase_item_id=purchase_item_id,
        adjustment_id=adjustment_id,
        movement_id=movement_id,   # <-- set movement link
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
        tuple: (total_cogs, list of InventoryTransaction objects)
    
    Raises:
        ValueError: If insufficient inventory
    """
    if quantity_needed <= 0:
        raise ValueError("Quantity must be positive")
    
    # Get product to verify total quantity
    product = Product.query.get(product_id)
    if not product:
        raise ValueError(f"Product {product_id} not found")
    
    if product.quantity < quantity_needed:
        raise ValueError(
            f"Insufficient inventory for {product.name}. "
            f"Available: {product.quantity}, Requested: {quantity_needed}"
        )
    
    # Get oldest lots first (FIFO)
    lots = InventoryLot.query.filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).order_by(InventoryLot.created_at.asc()).all()
    
    if not lots:
        raise ValueError(f"No inventory lots found for product {product_id}")
    
    total_cogs = 0.0
    remaining_to_consume = quantity_needed
    transactions = []
    
    for lot in lots:
        if remaining_to_consume <= 0:
            break
        
        # Determine how much to take from this lot
        qty_from_lot = min(lot.quantity_remaining, remaining_to_consume)
        cost_from_lot = round(qty_from_lot * lot.unit_cost, 2)
        
        # Create transaction record
        transaction = InventoryTransaction(
            lot_id=lot.id,
            quantity_used=qty_from_lot,
            unit_cost=lot.unit_cost,
            total_cost=cost_from_lot,
            sale_id=sale_id,
            sale_item_id=sale_item_id,
            ar_invoice_id=ar_invoice_id,
            ar_invoice_item_id=ar_invoice_item_id,
            adjustment_id=adjustment_id,
            created_at=datetime.utcnow()
        )
        db.session.add(transaction)
        transactions.append(transaction)
        
        # Update lot
        lot.quantity_remaining -= qty_from_lot
        
        # Accumulate COGS
        total_cogs += cost_from_lot
        remaining_to_consume -= qty_from_lot
    
    if remaining_to_consume > 0:
        raise ValueError(
            f"Could not consume {quantity_needed} units. "
            f"Only {quantity_needed - remaining_to_consume} available in lots."
        )
    
    return round(total_cogs, 2), transactions


def get_fifo_cost(product_id, quantity):
    """
    Calculate what the COGS would be for a given quantity without consuming.
    Useful for estimates and previews.
    
    Args:
        product_id: ID of the product
        quantity: Number of units
    
    Returns:
        float: Estimated COGS
    """
    lots = InventoryLot.query.filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).order_by(InventoryLot.created_at.asc()).all()
    
    total_cost = 0.0
    remaining = quantity
    
    for lot in lots:
        if remaining <= 0:
            break
        qty_from_lot = min(lot.quantity_remaining, remaining)
        total_cost += qty_from_lot * lot.unit_cost
        remaining -= qty_from_lot
    
    return round(total_cost, 2)


def get_weighted_average_cost(product_id):
    """
    Calculate the current weighted average cost for a product.
    This is useful for display purposes and reporting.
    
    Args:
        product_id: ID of the product
    
    Returns:
        float: Weighted average cost per unit
    """
    result = db.session.query(
        func.sum(InventoryLot.quantity_remaining * InventoryLot.unit_cost),
        func.sum(InventoryLot.quantity_remaining)
    ).filter(
        InventoryLot.product_id == product_id,
        InventoryLot.quantity_remaining > 0
    ).first()
    
    total_value, total_qty = result
    
    if not total_qty or total_qty == 0:
        return 0.0
    
    return round(total_value / total_qty, 2)


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
        summary.append({
            'lot_id': lot.id,
            'quantity': lot.quantity_remaining,
            'unit_cost': lot.unit_cost,
            'total_value': round(lot.quantity_remaining * lot.unit_cost, 2),
            'created_at': lot.created_at,
            'age_days': (datetime.utcnow() - lot.created_at).days,
            'is_opening_balance': lot.is_opening_balance,
            'movement_id': getattr(lot, 'movement_id', None),   # <-- include movement id
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
        func.sum(InventoryLot.quantity_remaining)
    ).filter(
        InventoryLot.product_id == product_id
    ).scalar() or 0
    
    discrepancy = product.quantity - lot_total
    
    return {
        'product_quantity': product.quantity,
        'lot_total': lot_total,
        'discrepancy': discrepancy,
        'is_balanced': discrepancy == 0
    }



def reverse_inventory_consumption(sale_id=None, ar_invoice_id=None):
    """
    Reverse FIFO inventory consumption for voided transactions.
    Restores inventory lots and deletes the consumption records.
    
    Args:
        sale_id: ID of the voided sale
        ar_invoice_id: ID of the voided AR invoice
    
    Returns:
        dict: Summary of reversed quantities by product
    """
    # Find all inventory transactions for this sale/invoice
    query = InventoryTransaction.query
    
    if sale_id:
        query = query.filter(InventoryTransaction.sale_id == sale_id)
    elif ar_invoice_id:
        query = query.filter(InventoryTransaction.ar_invoice_id == ar_invoice_id)
    else:
        raise ValueError("Must provide either sale_id or ar_invoice_id")
    
    transactions = query.all()
    
    reversed_summary = {}
    
    for trans in transactions:
        # Restore the lot quantity
        lot = InventoryLot.query.get(trans.lot_id)
        if lot:
            lot.quantity_remaining += trans.quantity_used
            
            # Track what we reversed
            product_id = lot.product_id
            if product_id not in reversed_summary:
                reversed_summary[product_id] = 0
            reversed_summary[product_id] += trans.quantity_used
        
        # Delete the transaction record
        db.session.delete(trans)
    
    return reversed_summary