"""
scoring.py
Contains all scoring and sanction logic for the credit analyzer app.
"""
def get_income_bracket(income):
    """
    Returns the income bracket based on the monthly income.
    """
    if income <= 100000:
        return '<100000'
    elif income <= 200000:
        return '100001-200000'
    elif income <= 350000:
        return '200001-350000'
    else:
        return '>350001'

WEIGHTS = {
    '<100000':    {'cibil': 0.15, 'cibil_overdue': 0.20, 'emi_loan': 0.10, 'payday_running': 0.20, 'residence_type': 0.25, 'enach_bounce': 0.10, 'bonus': 0.00},
    '100001-200000': {'cibil': 0.15, 'cibil_overdue': 0.20, 'emi_loan': 0.10, 'payday_running': 0.18, 'residence_type': 0.25, 'enach_bounce': 0.10, 'bonus': 0.2},
    '200001-350000': {'cibil': 0.15, 'cibil_overdue': 0.20, 'emi_loan': 0.10, 'payday_running': 0.15, 'residence_type': 0.25, 'enach_bounce': 0.10, 'bonus': 0.5},
    '>350001':     {'cibil': 0.15, 'cibil_overdue': 0.20, 'emi_loan': 0.10, 'payday_running': 0.12, 'residence_type': 0.25, 'enach_bounce': 0.10, 'bonus': 0.8},
}

def cibil_score(val):
    if val < 300:
        return 1
    elif 300 <= val <= 449:
        return 2
    elif 450 <= val <= 549:
        return 4
    elif 550 <= val <= 600:
        return 6
    elif 601 <= val <= 650:
        return 6
    elif 651 <= val <= 700:
        return 7
    elif 701 <= val <= 750:
        return 8
    elif val > 750:
        return 10
    return 1

def cibil_overdue(val):
    if val == 0:
        return 10
    elif val == 1:
        return 9
    elif val == 2:
        return 7
    elif val >= 3:
        return 5
    return 1

def emi_score(val):
    if val == 0:
        return 10
    elif 1 <= val <= 2:
        return 7
    elif 3 <= val <= 5:
        return 5
    elif val > 5:
        return 2
    return 1

def payday_score(val):
    if 0 <= val <= 1:
        return 10
    elif 2 <= val <= 3:
        return 8
    elif 4 <= val <= 5:
        return 5
    elif val >= 6:
        return 2
    return 1

def residence_score(val):
    if val.lower() == 'own house':
        return 10
    elif val.lower() == 'rented':
        return 5
    return 1

def enach_score(val):
    if val == 0:
        return 10
    elif 1 <= val <= 3:
        return 8
    elif 4 <= val <= 6:
        return 5
    elif 7 <= val <= 10:
        return 3
    elif val > 11:
        return 1
    return 1

def calculate_weighted_score(data):
    income_bracket = get_income_bracket(data['monthly_income'])
    weight_values = WEIGHTS[income_bracket]
    scores = {
        'cibil': cibil_score(data['cibil']),
        'cibil_overdue': cibil_overdue(data['cibil_overdue']),
        'emi_loan': emi_score(data['emi_loan']),
        'payday_running': payday_score(data['payday_running']),
        'residence_type': residence_score(data['residence_type']),
        'enach_bounce': enach_score(data['enach_bounce'])
    }
    weighted_total = sum(scores[param] * weight_values[param] for param in scores)
    bonus = weight_values.get('bonus', 0)
    final_score = weighted_total + bonus
    return scores, weighted_total, bonus, final_score, income_bracket

def calculate_sanction(data):
    scores, weighted_total, bonus, final_score, bracket = calculate_weighted_score(data)
    max_param_scores = {'cibil': 10, 'cibil_overdue': 10, 'emi_loan': 10, 'payday_running': 10, 'residence_type': 10, 'enach_bounce': 10}
    max_weighted_total = sum(max_param_scores[param] * WEIGHTS[bracket][param] for param in scores)
    post_ob = max(0, data["monthly_income"] - data["fixed_obligations"])  # Ensure post_ob is not negative
    obligation_ratio = data["fixed_obligations"] / data["monthly_income"] if data["monthly_income"] else 1
    if final_score < 5:
        min_pct, max_pct = 0.0, 0.0
    elif 5 <= final_score <= 6:
        min_pct, max_pct = 0.22, 0.25
    elif 6.1 <= final_score <= 7:
        min_pct, max_pct = 0.26, 0.29
    elif 7.1 <= final_score <= 8:
        min_pct, max_pct = 0.30, 0.32
    elif 8.1 <= final_score <= 9:
        min_pct, max_pct = 0.33, 0.35
    else:
        min_pct, max_pct = 0.35, 0.36

    # Initialize default sanction values
    min_sanction = 0
    max_sanction = 0
    sanction_percentage = (0.0, 0.0)
    sanction_amount_range = "₹0"

    # Only calculate sanction if obligation_ratio <= 0.8 (80%) and final_score >= 5
    if obligation_ratio <= 0.8 and (min_pct > 0 or max_pct > 0):
        sanction_percentage = (min_pct, max_pct)
        min_sanction = min_pct * post_ob
        max_sanction = max_pct * post_ob

        income = data['monthly_income']

        if income >= 400000:
            if final_score >= 9:
                min_cap, max_cap = 48500, 50000
            elif final_score >= 8:
                min_cap, max_cap = 47500, 49000
            elif final_score >= 7:
                min_cap, max_cap = 46500, 48000
            else: 
                min_cap, max_cap = 45500, 47000
        elif income >= 350000:
            if final_score >= 9:
                min_cap, max_cap = 45500, 47000
            elif final_score >= 8:
                min_cap, max_cap = 44500, 46000
            elif final_score >= 7:
                min_cap, max_cap = 43500, 45000
            else: 
                min_cap, max_cap = 42500, 44000
        elif income >= 300000:
            if final_score >= 9:
                min_cap, max_cap = 42500, 44000
            elif final_score >= 8:
                min_cap, max_cap = 41500, 43000
            elif final_score >= 7:
                min_cap, max_cap = 40500, 42000
            else: 
                min_cap, max_cap = 39500, 41000
        elif income >= 250000:
            if final_score >= 9:
                min_cap, max_cap = 39500, 41000
            elif final_score >= 8:
                min_cap, max_cap = 38500, 40000
            elif final_score >= 7:
                min_cap, max_cap = 37500, 39000
            else: 
                min_cap, max_cap = 36500, 38000
        elif income >= 200000:
            if final_score >= 9:
                min_cap, max_cap = 36500, 38000
            elif final_score >= 8:
                min_cap, max_cap = 35500, 37000
            elif final_score >= 7:
                min_cap, max_cap = 34500, 36000
            else: 
                min_cap, max_cap = 33500, 35000
        elif income >= 150000:
            if final_score >= 9:
                min_cap, max_cap = 32500, 34000
            elif final_score >= 8:
                min_cap, max_cap = 31500, 33000
            elif final_score >= 7:
                min_cap, max_cap = 30500, 32000
            else: 
                min_cap, max_cap = 29500, 31000
        else:
            min_cap, max_cap = 27000, 30000

        min_sanction = min(min_sanction, min_cap)
        max_sanction = min(max_sanction, max_cap)

        # Add ₹177 per document collected from checklist
        doc_bonus = data.get('docs_collected', 0) * 177
        min_sanction += doc_bonus
        max_sanction += doc_bonus

        sanction_amount_range = f"₹{int(min_sanction):,} - ₹{int(max_sanction):,}"
    else:
        sanction_amount_range = "₹0"
        sanction_percentage = (0.0, 0.0)
    # Only approve if final score is good AND obligation ratio is <= 80%
    decision = "Can be Approved" if (final_score >= max_weighted_total * 0.5 and obligation_ratio <= 0.8) else "Cannot be Approved"

    # Additional rejection rule: low sanction amount with high enach_bounce
    if max_sanction < 10000 or data.get('enach_bounce', 0) > 12 or data.get('cibil_overdue', 0) > 10 or data.get('emi_loan', 0) > 12:
        decision = "Cannot be Approved"
        sanction_amount_range = "₹0"
        sanction_percentage = (0.0, 0.0)

    return {
        'sanction_amount_range': sanction_amount_range,
        'sanction_percentage': (sanction_percentage[0]*100, sanction_percentage[1]*100),
        'decision': decision,
        'final_score': final_score,
        'max_weighted_total': max_weighted_total,
        'obligation_ratio': obligation_ratio * 100  # Convert to percentage
    }