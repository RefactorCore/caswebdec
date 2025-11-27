"""
Universal SKU Auto-Generation System
Works for ANY retail business type
"""
from models import db, Product
import re
from sqlalchemy.exc import IntegrityError



# ✅ UNIVERSAL CATEGORY PRESETS (Expandable)
INDUSTRY_CATEGORIES = {
    # Automotive
    'automotive': {
        'TIR': 'Tires',
        'FIL': 'Filters',
        'BRK': 'Brakes',
        'BAT': 'Battery',
        'OIL': 'Oil/Lubricants',
        'SPK': 'Spark Plugs',
        'WIP': 'Wipers',
        'MIR': 'Mirrors',
        'LGT': 'Lights',
        'CAB': 'Cables',
        'BLT': 'Belts',
    },
    
    # Construction/Hardware
    'construction': {
        'CEM': 'Cement',
        'SND': 'Sand',
        'GRV': 'Gravel',
        'PLY': 'Plywood',
        'PNT': 'Paint',
        'NAL': 'Nails/Screws',
        'WIR': 'Wire/Cable',
        'TUB': 'Pipes/Tubes',
        'TOL': 'Tools',
        'ELC': 'Electrical',
    },
    
    # Apparel/Boutique
    'apparel': {
        'DRS': 'Dresses',
        'TOP': 'Tops/Blouses',
        'PNT': 'Pants/Jeans',
        'SKT': 'Skirts',
        'SHO': 'Shoes',
        'BAG': 'Bags',
        'ACC': 'Accessories',
        'UND': 'Underwear',
        'SWT': 'Sweaters',
        'OUT': 'Outerwear',
    },
    
    # Beauty & Skincare
    'beauty': {
        'SKN': 'Skincare',
        'MKP': 'Makeup',
        'FRG': 'Fragrance',
        'HRC': 'Haircare',
        'BDY': 'Body Care',
        'TON': 'Toner',
        'SRM': 'Serum',
        'MST': 'Moisturizer',
        'CLN': 'Cleanser',
        'MSK': 'Mask',
    },
    
    # Food & Beverage
    'foodbev': {
        'MLK': 'Milk Tea',
        'COF': 'Coffee',
        'JCE': 'Juice',
        'SNK': 'Snacks',
        'SIN': 'Sinkers/Add-ons',
        'CUP': 'Cups',
        'SYR': 'Syrup',
        'PWD': 'Powder',
        'ICE': 'Ice/Frozen',
        'PCK': 'Packaging',
    },
    
    # General/Universal (Default)
    'general': {
        'PRD': 'Product',
        'ITM': 'Item',
        'GDS': 'Goods',
        'MRC': 'Merchandise',
        'SUP': 'Supplies',
    }
}


def generate_sku(product_name, category=None, custom_sku=None, industry=None):
    """
    Universal SKU generator for any retail business.  

    Args:
        product_name: Name of the product
        category: Optional category code (e.g., "TIR", "DRS", "SKN")
        custom_sku: Optional manual SKU (validated for uniqueness)
        industry: Optional industry hint for auto-detection

    Returns:
        Unique SKU string
    """
    import re
    from datetime import datetime
    from models import Product

    # 1.  CUSTOM SKU: Validate and use if provided
    if custom_sku and custom_sku.strip():
        custom_sku = custom_sku.strip().upper()

        # Validate format
        if not re.match(r'^[A-Z0-9-]+$', custom_sku):
            raise ValueError("SKU can only contain letters, numbers, and hyphens")

        if len(custom_sku) > 64:
            raise ValueError("SKU is too long (max 64 characters)")

        # Check uniqueness
        existing = Product.query.filter_by(sku=custom_sku).first()
        if existing:
            raise ValueError(f"SKU '{custom_sku}' already exists for: {existing.name}")

        return custom_sku

    # 2.  DETERMINE PREFIX
    if category and category.strip():
        prefix = category.strip().upper()[:3]
    else:
        prefix = auto_detect_category(product_name, industry)

    # ✅ FIX: Use simpler, more reliable SKU generation
    max_retries = 5
    
    for attempt in range(max_retries):
        try:
            # Get engine name to determine locking strategy
            engine_name = db.session.bind.dialect.name
            
            # ✅ SIMPLIFIED: Just get the highest number for this prefix
            if engine_name == 'sqlite':
                # SQLite: Simple query without locking
                existing_skus = db.session.query(Product.sku).filter(
                    Product.sku.like(f'{prefix}-%')
                ).all()
            else:
                # PostgreSQL/MySQL/MariaDB: Use SELECT FOR UPDATE
                existing_skus = db.session.query(Product.sku).filter(
                    Product.sku.like(f'{prefix}-%')
                ).with_for_update().all()
            
            # Extract numbers from existing SKUs
            max_num = 0
            # ✅ FIX: Simplified regex - match PREFIX-DIGITS at end of string
            pattern = re.compile(rf'^{re.escape(prefix)}-(\d+)$')
            
            for (sku,) in existing_skus:
                match = pattern.match(sku)
                if match:
                    try:
                        num = int(match.group(1))
                        if num > max_num:
                            max_num = num
                    except ValueError:
                        continue
            
            # Generate next SKU
            next_num = max_num + 1
            new_sku = f"{prefix}-{next_num:05d}"
            
            # ✅ CRITICAL: Double-check uniqueness before returning
            if not Product.query.filter_by(sku=new_sku).first():
                return new_sku  # Safe to use
            else:
                # Race condition detected, retry
                if attempt < max_retries - 1:
                    continue
                # If last attempt, fall through to raise error
                    
        except Exception as e:
            db.session.rollback()
            if attempt < max_retries - 1:
                continue
            # Last attempt failed, break to fallback
            break
    
    # ✅ FALLBACK: If all retries fail, use a shorter unique identifier
    # Use microseconds (6 digits) + random 3 digits instead of full timestamp
    from random import randint
    microseconds = datetime.now().strftime('%f')  # 6 digits
    random_suffix = f"{randint(100, 999)}"
    fallback_sku = f"{prefix}-{microseconds}{random_suffix}"
    
    # Ensure fallback is also unique
    attempt_count = 0
    while Product.query.filter_by(sku=fallback_sku).first() and attempt_count < 10:
        random_suffix = f"{randint(100, 999)}"
        fallback_sku = f"{prefix}-{microseconds}{random_suffix}"
        attempt_count += 1
    
    return fallback_sku


def auto_detect_category(product_name, industry=None):
    """
    Smart category detection from product name.
    
    Args:
        product_name: Product name to analyze
        industry: Optional industry hint
    
    Returns:
        3-letter category prefix
    """
    name_lower = product_name.lower()
    
    # Define keyword mappings for auto-detection
    keywords = {
        # Automotive
        'TIR': ['tire', 'tires', 'gulong'],
        'FIL': ['filter', 'air filter', 'oil filter'],
        'BRK': ['brake', 'brakes', 'preno'],
        'OIL': ['oil', 'lubricant', 'langis'],
        'BAT': ['battery', 'baterya'],
        'SPK': ['spark plug', 'spark'],
        
        # Construction
        'CEM': ['cement', 'semento'],
        'SND': ['sand', 'buhangin'],
        'PLY': ['plywood', 'wood'],
        'PNT': ['paint', 'pintura'],
        
        # Apparel
        'DRS': ['dress', 'damit'],
        'TOP': ['top', 'blouse', 'shirt'],
        'PNT': ['pants', 'jeans', 'slacks'],
        'SHO': ['shoes', 'sapatos'],
        'BAG': ['bag', 'purse'],
        
        # Beauty
        'SKN': ['skin', 'skincare', 'face'],
        'MKP': ['makeup', 'lipstick', 'foundation'],
        'CLN': ['cleanser', 'wash'],
        'TON': ['toner'],
        'SRM': ['serum'],
        
        # Food & Beverage
        'MLK': ['milk tea', 'milktea'],
        'COF': ['coffee', 'kape'],
        'JCE': ['juice'],
        'SNK': ['snack'],
    }
    
    # Try to match keywords
    for prefix, keywords_list in keywords.items():
        for keyword in keywords_list:
            if keyword in name_lower:
                return prefix
    
    # No match found: Use generic prefix
    return 'PRD'


def get_industry_categories(industry='general'):
    """
    Get category presets for a specific industry.
    
    Args:
        industry: Industry type ('automotive', 'construction', 'apparel', etc.)
    
    Returns:
        dict: Category code -> name mapping
    """
    return INDUSTRY_CATEGORIES.get(industry, INDUSTRY_CATEGORIES['general'])


def get_all_categories():
    """
    Get all available category presets across all industries.
    
    Returns:
        dict: Combined categories from all industries
    """
    combined = {}
    for industry_cats in INDUSTRY_CATEGORIES.values():
        combined.update(industry_cats)
    return combined


def get_category_suggestions():
    """
    Get suggested category prefixes for the bulk upload template.
    Returns a flattened list of all categories across industries.
    
    Returns:
        dict: Category prefix -> description mapping
    """
    suggestions = {}
    
    # Combine all industry categories into one dictionary
    for industry_name, categories in INDUSTRY_CATEGORIES.items():
        for prefix, description in categories.items():
            # Add industry hint to description
            if industry_name != 'general':
                suggestions[prefix] = f"{description} ({industry_name.title()})"
            else:
                suggestions[prefix] = description
    
    return dict(sorted(suggestions.items()))


def validate_sku(sku):
    """
    Validate SKU format and uniqueness.
    
    Args:
        sku: SKU string to validate
    
    Returns:
        tuple: (is_valid, error_message)
    """
    if not sku or not sku.strip():
        return False, "SKU cannot be empty"
    
    sku = sku.strip().upper()
    
    if len(sku) > 64:
        return False, "SKU is too long (max 64 characters)"
    
    if not re.match(r'^[A-Z0-9-]+$', sku):
        return False, "SKU can only contain letters, numbers, and hyphens"
    
    existing = Product.query.filter_by(sku=sku).first()
    if existing:
        return False, f"SKU already exists for: {existing.name}"
    
    return True, None


def suggest_sku(product_name, industry=None):
    """
    Suggest multiple SKU options for a product.
    
    Args:
        product_name: Product name
        industry: Optional industry hint
    
    Returns:
        list: List of suggested SKUs
    """
    suggestions = []
    
    # Option 1: Auto-detected category
    auto_prefix = auto_detect_category(product_name, industry)
    suggestions.append({
        'sku': generate_sku(product_name, category=auto_prefix),
        'description': f'Auto-detected ({auto_prefix})'
    })
    
    # Option 2: Generic
    if auto_prefix != 'PRD':
        suggestions.append({
            'sku': generate_sku(product_name, category='PRD'),
            'description': 'Generic product code'
        })
    
    # Option 3: From product name initials
    words = re.sub(r'[^A-Za-z0-9\s]', '', product_name).split()
    if len(words) >= 2:
        initials = ''.join(word[0] for word in words[:3]).upper()
        suggestions.append({
            'sku': generate_sku(product_name, category=initials),
            'description': f'Name-based ({initials})'
        })
    
    return suggestions