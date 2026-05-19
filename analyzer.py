"""
Bank Statement Transaction Analyzer
────────────────────────────────────
Classifies bank transactions into: Salary, EMI/Loans, ACH,
NACH Bounces, and Frequent Transfers using pattern matching
and temporal analysis.

Salary Detection (keywords first, then pattern analysis):
  Priority 1 — Keyword match ("salary", "sal cr", "payroll") → instant confirm
  Priority 2 — Recurring pattern: group by source, hike-aware stability,
               company/NEFT bonus scoring. Filters out UPI P2P, refunds,
               loan disbursers, and non-salary sources.

All patterns are server-side only — never exposed to the browser.
"""

import re
from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any


# ═══════════════════════════════════════════════════════════════════════
# 1. SALARY PATTERNS
# ═══════════════════════════════════════════════════════════════════════
# SALARY_KEYWORDS  – narrations that explicitly mention salary/wage credits
# COMPANY_PATTERNS – company types for inferred-salary detection
# SALARY_TRANSFER_MODE – salary usually arrives via NEFT/RTGS/IMPS
# LOAN_EXCLUDE_PATTERNS – block loan disbursements from being tagged as salary

SALARY_KEYWORDS = re.compile(
    '|'.join([
        r'salary',
        r'sal cr',
        r'salar',
        r'sal/',
        r'monthly pay',
        r'payroll',
        r'stipend',
        r'wages',
        r'branch\s*imprest',
    ]),
    re.IGNORECASE,
)

COMPANY_PATTERNS = re.compile(
    '|'.join([
        r'pvt\.?\s*ltd', r'private\s+limited', r'\blimited\b', r'\bltd\b',
        r'\bllp\b', r'\bcorp\b', r'\binc\b',
        r'technologies', r'solutions', r'services', r'enterprises', r'consulting',
        r'infosys', r'wipro', r'tcs\b', r'hcl\b', r'cognizant', r'accenture',
        r'hospital', r'\bhosp\b',
    ]),
    re.IGNORECASE,
)

SALARY_TRANSFER_MODE = re.compile(
    r'^(neft|rtgs|imps)[/\s-]|^ft\s*(neft|rtgs|imps)[/\s-]|^recd\s*:\s*imps/|^mmt/imps/',
    re.IGNORECASE,
)

LOAN_EXCLUDE_PATTERNS = re.compile(
    '|'.join([
        r'\bldr\b', r'loan', r'disb', r'lend', r'financ', r'fincorp', r'nbfc',
        r'capital', r'credit', r'commodit', r'trading', r'nidhi', r'chit\s*fund',
        r'micro\s*fin', r'pay\s*day', r'leasing', r'fincap', r'assignments',
        r'securities', r'metals',
        r'bajaj\s?fin', r'bajaj\s*housing', r'lic\s*housing',
        r'home\s*credit', r'capital\s*first', r'fullerton',
        r'tata\s?capital', r'piramal', r'shriram', r'sundaram', r'idfc\s*first',
        r'manappuram', r'muthoot',
        r'paysense', r'kreditbee', r'cashe', r'navi\b',
        r'money\s*tap', r'earlysalary', r'mpokket',
        r'protium', r'stashfin', r'kissht',
        r'ola\s*financial', r'pay\s*later', r'branch\s*(?:payment|online)',
        r'sampati', r'unifinz', r'chintamani', r'goldline', r'chinmay',
        r'rk\s*bansal', r'solomon', r'vaishali',
        r'd\.?pal', r'day\s*to\s*day', r'goodskill', r'easyfincare',
        r'agrim', r'naman\s*finlease', r'junoon', r'datta\s*finance',
        r'gagan\s*metals', r'gaganmetals', r'achiievers', r'sawalsha',
        r'woodland\s*securities', r'konark', r'kasar', r'tycoon',
        r'sashi\s*enterprises', r'sashienterprises',
        r'xpressloan', r'growing', r'sabharwal', r'sabkaloan',
        r'mahashakti', r'speedo\s*loans', r'salora', r'comero',
        r'loanpe', r'girdhar', r'fast\s*solutions\s*fin',
        r'uca\b', r'devmuni', r'ayaan\b',
        r'agarwal\s*assignments', r'digner', r'devashish', r'skyrise',
        r'bazarloan', r'tsb\s*finance', r'cashmypayment',
        r'loanhub', r'loanforcare', r'salary4sure', r'salary\s*now',
        r'bharatloan', r'gdl\s*leasing', r'agf\b',
        r'mahavira', r'innofinsolu', r'respo',
        r'akara', r'northern\s*arc', r'aman\s*fincap',
        r'auro\s*fin', r'fincfriend', r'citra', r'ava\s*fina',
        r'finagle', r'khosya', r'finkurve', r'upmove', r'speel',
        r'vivifi', r'meghdoo', r'salaryontime', r'minutesloan',
        r'altura', r'jublee', r'richman', r'surya.?shakti', r'\bdsg\b',
        r'shreeloan', r'loanprime', r'neena\s*imp',
        r'pawansut', r'tapstart', r'u\.?\s*y\.?\s*finc',
        r'gagandeep\s*services', r'zed\s*leafin',
        r'ampire', r'vanshika', r'kisga', r'ramchandra\s*leasing',
        r'avinash\s*capital', r'julania', r'subhlakshmi',
        r'flexsalary',
    ]),
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════
# 2. EMI / LOAN PATTERNS
# ═══════════════════════════════════════════════════════════════════════
# EMI_UPI_STRICT   – stricter EMI keywords for UPI transactions only
# ACH_NACH_PATTERN – narrations that start with ACH/NACH/ECS/SI
# LENDER_PATTERNS  – known lenders (generic finance keywords + named lenders)

EMI_UPI_STRICT = re.compile(
    r'\bemi\b|loan|repayment|installment',
    re.IGNORECASE,
)

ACH_NACH_PATTERN = re.compile(
    r'\bach|^nach|^ecs|^si/',
    re.IGNORECASE,
)

# Reuse LOAN_EXCLUDE_PATTERNS for lender detection on debits too
LENDER_PATTERNS = LOAN_EXCLUDE_PATTERNS


# ═══════════════════════════════════════════════════════════════════════
# 3. NACH BOUNCE PATTERNS
# ═══════════════════════════════════════════════════════════════════════
# Detects failed auto-debit (NACH/ECS) transactions — critical red flag.
#
#   HDFC:  "NACH RET", "NACH RETURN"
#   ICICI: "ECSRTN1_0402..."
#   SBI:   "ACH...RET", "NACH...FAIL"
#   Kotak: "Chrg: ECS Mandate"

NACH_BOUNCE_PATTERNS = re.compile(
    '|'.join([
        r'nach ret', r'nach return', r'nach bounce', r'nach.?fail',
        r'ecs.*ret', r'ecs return', r'ecsrtn',
        r'ach.*ret',
        r'mandate reject',
        r'insufficient',
        r'bounce',
        r'dishon',
        r'unpaid',
        r'return.*nach',
        r'ret\s*ch',
        r'nach.?ad.?rtn',
    ]),
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════
# 4. HELPER PATTERNS
# ═══════════════════════════════════════════════════════════════════════
# UPI_PATTERN    – identifies UPI transactions (varies by bank)
# ECOM_PATTERN   – ECOM-prefixed debits
# CHARGE_PATTERNS– bank fees/penalties (kept for internal use)
# _UPI_HANDLE    – extracts UPI handle (xxx@yyy) from narration
# _ACCOUNT_NUM   – extracts account/reference numbers for transfer grouping

UPI_PATTERN = re.compile(
    r'^upi[\s/]|^upiout/|^upi\s?in/',
    re.IGNORECASE,
)

ECOM_PATTERN = re.compile(r'^ecom', re.IGNORECASE)

CHARGE_PATTERNS = re.compile(
    '|'.join([
        r'\bcharges?\b', r'(?<!re)charge\b', r'\bfee\b', r'\bchrg\b',
        r'penalty', r'penal', r'interest.*debit', r'min.?bal', r'maint',
        r'non.?maint', r'sms\s*(?:charge|alert)', r'instaalert',
        r'atm.?(?:charge|fee|maint)', r'overdue', r'gst', r'cess',
        r'service tax', r'stamp duty', r'folio', r'annual', r'late.*fee',
        r'posdec.?chg', r'rtnchg', r'nach.?rtn.?chrg',
    ]),
    re.IGNORECASE,
)

_UPI_HANDLE = re.compile(r'[\w.\-]+@[\w.]+')
_ACCOUNT_NUM = re.compile(r'\b\d{9,16}\b')


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _get_narration(txn: dict) -> str:
    return (
        txn.get('narration')
        or txn.get('transactionNarration')
        or txn.get('reference')
        or txn.get('Narration')
        or ''
    )


def _get_type(txn: dict) -> str:
    return (txn.get('type') or txn.get('txnType') or txn.get('Type') or '').upper()


def _get_amount(txn: dict) -> float:
    raw = txn.get('amount') or txn.get('transactionAmount') or txn.get('Amount') or 0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def _get_date(txn: dict) -> str:
    return (
        txn.get('valueDate')
        or txn.get('transactionTimestamp')
        or txn.get('txnDate')
        or txn.get('Date')
        or ''
    )


def _extract_destination(narr: str) -> str:
    """Extract UPI handle or account number from a narration for grouping."""
    m = _UPI_HANDLE.search(narr)
    if m:
        return m.group().lower()
    m = _ACCOUNT_NUM.search(narr)
    if m:
        return m.group()
    # Fallback: 2nd segment of UPI/xxx/... format
    if re.match(r'upi', narr, re.IGNORECASE):
        parts = narr.split('/')
        if len(parts) >= 2:
            dest = parts[1].strip().lower()
            if dest and dest not in ('dr', 'cr', 'in', 'out'):
                return dest
    return ''


def _parse_date_obj(date_str: str):
    """Try to parse a date string into a datetime object."""
    if not date_str:
        return None
    # Try each format against the full string first, then truncated
    for fmt, slen in (
        ('%Y-%m-%dT%H:%M:%S.%f', None),
        ('%Y-%m-%dT%H:%M:%S', 19),
        ('%Y-%m-%d', 10),
        ('%d/%m/%Y', 10),
        ('%d-%m-%Y', 10),
    ):
        try:
            s = date_str[:slen] if slen else date_str
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════
# 5. SALARY DETECTION ENGINE (Top-down, hike-aware)
# ═══════════════════════════════════════════════════════════════════════
#
# APPROACH: Start from the biggest credits and work down.
#
#   Step 1 — Collect ALL credits, sort by amount (biggest first)
#   Step 2 — Group by source (who sent the money)
#   Step 3 — Filter out noise:
#            • UPI from person names ("Payment fr") → SKIP
#            • Known loan disbursers → SKIP
#            • Only 1 occurrence → SKIP (not recurring)
#            • More than 2 transactions below ₹20,000 → SKIP (too small for salary)
#   Step 4 — For surviving groups, pick ONE credit per month (the largest)
#   Step 5 — Check hike-aware stability:
#            • Amount can go UP (hike) or stay flat (±10%)
#            • Small drops OK (≤25%) — tax, fewer days
#            • Big drops (>25%) = NOT salary
#            • ≥70% of month-to-month pairs must be "stable"
#   Step 6 — Score with bonus for keywords / company / NEFT mode
#
# TRANSFER MODE TRUST RANKING:
#   NEFT/RTGS = HIGH (companies use this for salary)
#   IMPS      = MEDIUM
#   UPI       = LOW (mostly P2P, rarely salary)

# Minimum months to consider a stream as salary
SALARY_MIN_MONTHS = 2

# Max allowed big-drop ratio (if >30% of pairs are big drops, reject)
SALARY_MAX_BAD_PAIRS = 0.30

# What counts as a "big drop" — more than 25% decrease month-to-month
SALARY_BIG_DROP_PCT = 0.25

# Minimum credit amount for pattern-based salary detection (Step 2)
# Keyword matches (Step 1) have no minimum — "SALARY" at any amount is valid
SALARY_MIN_AMOUNT = 5000  # ₹5K — below this, no company pays salary

# Credits that are NEVER salary — refunds, cashbacks, interest, reversals
NOT_SALARY_PATTERNS = re.compile(
    '|'.join([
        r'refund', r'cashback', r'cash\s*back', r'reversal', r'reversed',
        r'interest\s*(cr|credit)', r'int\.?\s*cr', r'reward',
        r'dividend', r'coupon', r'discount',
    ]),
    re.IGNORECASE,
)

# Known non-salary companies — food, shopping, rides, wallets
# Credits from these are refunds/cashbacks, not salary
NOT_SALARY_SOURCES = re.compile(
    '|'.join([
        r'zomato', r'swiggy', r'amazon', r'flipkart', r'myntra',
        r'paytm', r'phonepe', r'googlepay', r'google\s*pay',
        r'uber', r'ola\b', r'rapido', r'dunzo',
        r'netflix', r'hotstar', r'spotify', r'youtube',
        r'razorpay', r'cashfree', r'paypal',
        r'gpay', r'bhim', r'cred\b',
    ]),
    re.IGNORECASE,
)


def _is_self_transfer(narr: str) -> bool:
    """Detect own account transfers where sender name appears twice in narration.
    e.g. 'NEFT CR-UBIN0808172-KONENI REDDY PADMAJA-KONENI REDDY PADMAJA-002593691359'
    """
    narr_upper = narr.upper()
    # Split NEFT/RTGS/IMPS narrations by '-' and check for repeated name segments
    parts = [p.strip() for p in narr_upper.split('-') if p.strip()]
    # Filter out short parts (bank codes, refs, numbers)
    name_parts = [p for p in parts if len(p) >= 4 and not p.isdigit()
                  and not re.match(r'^[A-Z]{4}\d+$', p)  # bank IFSC like UBIN0808172
                  and not re.match(r'^(NEFT|RTGS|IMPS|UPI|FT)\b', p)
                  and not re.match(r'^(CR|DR)$', p)]
    # If same name appears 2+ times, it's a self-transfer
    for i, a in enumerate(name_parts):
        for b in name_parts[i + 1:]:
            if a == b and len(a) >= 5:
                return True
    return False


def _extract_source(narr: str) -> str:
    """Extract payer/source identifier from a CREDIT narration for grouping."""
    narr_lower = narr.lower()

    # NEFT/RTGS/IMPS with slash separator: MODE/BANK/SENDER_NAME/REF
    for prefix in ('neft/', 'rtgs/', 'imps/', 'ft neft/', 'ft rtgs/', 'ft imps/'):
        if narr_lower.startswith(prefix):
            parts = narr.split('/')
            if len(parts) >= 3:
                return parts[2].strip().lower()
            if len(parts) >= 2:
                return parts[1].strip().lower()

    # NEFT/RTGS with dash separator (ICICI, etc): NEFT-BANKREF-SENDER NAME--ACCTNO
    # e.g. "NEFT-BOFAN52025091710661676-ACCENTURE SOLUTIONS PVT LTD--72447190"
    m = re.match(r'^(?:neft|rtgs|ft\s*neft|ft\s*rtgs)[\s-]+\S+-(.+?)(?:--|\s*$)', narr, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        # Remove trailing reference codes: digits, dash-digit groups, alphanumeric refs
        # e.g. "WIPRO LIMITED 2244-0001-0009" → "WIPRO LIMITED"
        # e.g. "ACCENTURE SOLUTIONS PVT LTD--72447190" → "ACCENTURE SOLUTIONS PVT LTD"
        sender = re.sub(r'[-\s]+[\d][\d\-]*$', '', sender).strip()
        sender = re.sub(r'[-\s]+[A-Z]?\d{3,}.*$', '', sender).strip()
        if sender and len(sender) >= 3:
            return sender.lower()

    # NEFT/RTGS with space separator (Kotak, etc): NEFT REF SENDER NEFTINW-
    # e.g. "NEFT IN22536009235468 LARSEN NEFTINW-"
    m = re.match(r'^(?:neft|rtgs)\s+\S+\s+(.+?)(?:\s+NEFTINW|\s*$)', narr, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        sender = re.sub(r'[-\s]*\d{5,}.*$', '', sender).strip()
        if sender and len(sender) >= 3:
            return sender.lower()

    # Recd:IMPS (Kotak, etc): Recd:IMPS/REF/SENDER/BANK/ACCT/NOTE
    # e.g. "Recd:IMPS/535020166107/SOLOMON CA/KKBK/X0399/IMPSX"
    m = re.match(r'^recd\s*:\s*imps/\d+/([^/]+)', narr, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        if sender and len(sender) >= 3:
            return sender.lower()

    # MMT/IMPS: MMT/IMPS/REF/SENDER/BANK
    # e.g. "MMT/IMPS/526126101617/PAYOUTS/PayoutRBL/Ratnakar Bank"
    m = re.match(r'^mmt/imps/\d+/([^/]+)', narr, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        if sender and len(sender) >= 3:
            return sender.lower()

    # NEFT with NEFTINW prefix (Kotak inward): NEFT YESF... SENDER NEFTINW-
    m = re.match(r'^neft\s+\S+\s+(.+?)\s+neftinw', narr, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        if sender and len(sender) >= 3:
            return sender.lower()

    # UPI: extract sender name (2nd segment)
    if re.match(r'^upi', narr_lower):
        parts = narr.split('/')
        if len(parts) >= 2:
            src = parts[1].strip().lower()
            if src and src not in ('dr', 'cr', 'in', 'out'):
                return src

    # Fallback: first 40 chars normalized
    return re.sub(r'\s+', ' ', narr[:40]).strip().lower()


_extract_transfer_dest = _extract_source


def _get_transfer_mode(narr: str) -> str:
    """Classify transfer mode: 'neft_rtgs', 'imps', 'upi', or 'other'."""
    narr_lower = narr.lower()
    # NEFT/RTGS: slash, dash, or space separator
    if re.match(r'^(neft|rtgs|ft\s*neft|ft\s*rtgs)[/\s-]', narr_lower):
        return 'neft_rtgs'
    # IMPS: slash, dash, or space separator; also Recd:IMPS and MMT/IMPS
    if re.match(r'^(imps|ft\s*imps)[/\s-]', narr_lower):
        return 'imps'
    if re.match(r'^recd\s*:\s*imps/', narr_lower):
        return 'imps'
    if re.match(r'^mmt/imps/', narr_lower):
        return 'imps'
    if re.match(r'^upi', narr_lower):
        return 'upi'
    return 'other'


def _is_upi_p2p(narr: str) -> bool:
    """Check if UPI narration is a personal transfer (not company/payroll)."""
    narr_lower = narr.lower()
    if not re.match(r'^upi', narr_lower):
        return False
    # "Payment fr", "pay to", "transfer" → P2P
    if re.search(r'payment\s*fr|pay\s*to|transfer|sent|received', narr_lower):
        return True
    # Person name sender — split by '/' or '-' (both formats exist)
    # Format 1: UPI/SENDER/UPI_ID/REF
    # Format 2: UPI-SENDER-UPI_ID@BANK-IFSC-REF
    for sep in ['/', '-']:
        parts = narr.split(sep)
        if len(parts) >= 2:
            sender = parts[1].strip()
            words = sender.split()
            if 1 <= len(words) <= 3 and not COMPANY_PATTERNS.search(sender):
                # Extra check for '-' split: make sure sender isn't a bank code or ref number
                if sep == '-' and (re.match(r'^[A-Z]{4}\d+$', sender) or sender.isdigit()):
                    continue
                return True
    return False


def _pick_one_per_month(txns: list[dict]) -> list[dict]:
    """Pick the LARGEST credit per calendar month from a source's transactions."""
    by_month: dict[tuple, list[dict]] = defaultdict(list)
    for t in txns:
        if t['date_obj']:
            key = (t['date_obj'].year, t['date_obj'].month)
            by_month[key].append(t)

    picked = []
    for month_key in sorted(by_month):
        best = max(by_month[month_key], key=lambda t: t['amount'])
        picked.append(best)
    return picked


def _check_hike_aware_stability(amounts: list[float], max_drop: float = None) -> dict:
    """
    Check if month-to-month amount changes look like salary (with possible hikes).

    Rules for each consecutive pair:
      • Amount goes UP by any %     → OK (hike / bonus component)
      • Stays within ±10%           → OK (normal salary variation)
      • Drops ≤max_drop             → OK (tax adjustment, fewer days, bonus spike)
      • Drops >max_drop             → BAD (random P2P pattern)

    Returns dict with:
      stable_pairs : count of OK pairs
      bad_pairs    : count of BAD pairs
      total_pairs  : total consecutive pairs
      stability    : ratio of OK pairs (0.0–1.0)
      latest_amount: most recent month's amount (for "current salary")
    """
    if max_drop is None:
        max_drop = SALARY_BIG_DROP_PCT

    if len(amounts) < 2:
        return {
            'stable_pairs': 0, 'bad_pairs': 0, 'total_pairs': 0,
            'stability': 1.0, 'latest_amount': amounts[0] if amounts else 0,
        }

    stable = 0
    bad = 0
    for i in range(1, len(amounts)):
        prev, curr = amounts[i - 1], amounts[i]
        if prev == 0:
            stable += 1
            continue
        change = (curr - prev) / prev

        if change >= 0:
            stable += 1
        elif change >= -0.10:
            stable += 1
        elif change >= -max_drop:
            stable += 1
        else:
            bad += 1

    total = len(amounts) - 1
    return {
        'stable_pairs': stable,
        'bad_pairs': bad,
        'total_pairs': total,
        'stability': round(stable / total, 3) if total > 0 else 1.0,
        'latest_amount': amounts[-1],
    }


def _detect_salary(transactions: list[dict]) -> list[dict]:
    """
    Main salary detection — keywords first, then analysis.

    Priority order:
      FIRST  — Keyword scan: "salary", "sal cr", "payroll", etc.
               If keyword found → it's salary. Done. No further analysis.
      SECOND — For remaining credits (no keyword): group by source,
               check recurring pattern, hike-aware stability, score.

    Returns list of salary stream dicts, sorted by confidence.
    """

    # ══════════════════════════════════════════════════════════════
    # PRIORITY 1: KEYWORD MATCH (instant — no grouping needed)
    # ══════════════════════════════════════════════════════════════
    # Any CREDIT with "salary", "sal cr", "payroll", etc. in narration
    # is salary. Period. Even if it's UPI, even if it's one-time.

    keyword_hits = []       # transactions confirmed by keyword
    remaining = []          # credits that need further analysis

    for txn in transactions:
        narr_raw = _get_narration(txn)
        narr = narr_raw.lower()
        txn_type = _get_type(txn)
        amt = _get_amount(txn)
        date_str = _get_date(txn)
        date_obj = _parse_date_obj(date_str)

        if txn_type != 'CREDIT' or amt <= 0:
            continue

        # NEVER salary: refunds, cashbacks, reversals
        if NOT_SALARY_PATTERNS.search(narr):
            continue

        # NEVER salary: known non-salary sources (Zomato, Swiggy, Amazon, etc)
        if NOT_SALARY_SOURCES.search(narr):
            continue

        # NEVER salary: own account transfers (sender name = account holder)
        if _is_self_transfer(narr_raw):
            continue

        # NEVER salary: MMT/IMPS bill payments / personal transfers
        if re.match(r'^mmt/imps/', narr):
            continue

        entry = {
            'narration': narr_raw,
            'narration_lower': narr,
            'amount': amt,
            'date': date_str,
            'date_obj': date_obj,
            'mode': _get_transfer_mode(narr_raw),
            'has_company_pattern': bool(COMPANY_PATTERNS.search(narr)),
            'is_loan_disburser': bool(LOAN_EXCLUDE_PATTERNS.search(narr)),
            'is_upi_p2p': _is_upi_p2p(narr_raw),
        }

        # KEYWORD CHECK — highest priority
        if SALARY_KEYWORDS.search(narr) and not entry['is_loan_disburser']:
            keyword_hits.append(entry)
        elif amt >= SALARY_MIN_AMOUNT:
            # Only consider for pattern analysis if amount ≥ ₹5K
            remaining.append(entry)

    # Build keyword stream(s) — group keyword hits by source
    keyword_streams = []
    if keyword_hits:
        kw_groups: dict[str, list[dict]] = defaultdict(list)
        for h in keyword_hits:
            source = _extract_source(h['narration'])
            kw_groups[source or 'unknown'].append(h)

        for source, hits in kw_groups.items():
            hits.sort(key=lambda t: t['date_obj'] or datetime.min)
            amounts = [t['amount'] for t in hits]
            keyword_streams.append({
                'source': source,
                'tier': 1,
                'confidence': 0.95,
                'avg_amount': round(mean(amounts), 2),
                'latest_amount': round(amounts[-1], 2),
                'months_detected': len(set(
                    (t['date_obj'].year, t['date_obj'].month)
                    for t in hits if t['date_obj']
                )),
                'stability': 1.0,
                'transfer_mode': hits[0]['mode'],
                'has_keyword': True,
                'has_company': any(t['has_company_pattern'] for t in hits),
                'transactions': [
                    {'date': t['date'], 'narration': t['narration'], 'amount': t['amount']}
                    for t in hits
                ],
            })

    # ══════════════════════════════════════════════════════════════
    # PRIORITY 2: RECURRING PATTERN ANALYSIS (for non-keyword credits)
    # ══════════════════════════════════════════════════════════════
    # Group remaining credits by source → filter noise → check stability

    source_groups: dict[str, list[dict]] = defaultdict(list)
    for c in remaining:
        source = _extract_source(c['narration'])
        if source and len(source) >= 3:
            source_groups[source].append(c)

    pattern_streams = []
    for source, group_txns in source_groups.items():
        # --- Filter: skip loan disbursers ---
        if group_txns[0]['is_loan_disburser']:
            continue

        # --- Filter: skip UPI P2P (person-to-person transfers) ---
        if group_txns[0]['is_upi_p2p']:
            continue

        # --- Filter: need at least SALARY_MIN_MONTHS occurrences ---
        if len(group_txns) < SALARY_MIN_MONTHS:
            continue

        # --- Filter: skip if more than 2 transactions below ₹20,000 ---
        low_amount_count = sum(1 for t in group_txns if t['amount'] < 20000)
        if low_amount_count > 2:
            continue

        # --- Pick one credit per month (the largest) ---
        monthly = _pick_one_per_month(group_txns)
        if len(monthly) < SALARY_MIN_MONTHS:
            continue

        # --- Sort by date for sequential analysis ---
        monthly.sort(key=lambda t: t['date_obj'] or datetime.min)
        amounts = [t['amount'] for t in monthly]
        dates = [t['date_obj'] for t in monthly if t['date_obj']]
        months_detected = len(set((d.year, d.month) for d in dates))

        # --- Check hike-aware stability ---
        has_company = any(t['has_company_pattern'] for t in group_txns)
        # Companies get a higher drop tolerance (bonuses cause spikes)
        drop_pct = 0.40 if has_company else SALARY_BIG_DROP_PCT
        stability = _check_hike_aware_stability(amounts, max_drop=drop_pct)

        # If too many big drops → not salary
        if stability['total_pairs'] > 0:
            bad_ratio = stability['bad_pairs'] / stability['total_pairs']
            if bad_ratio > SALARY_MAX_BAD_PAIRS:
                continue

        # --- Compute transfer mode for the group ---
        modes = [t['mode'] for t in group_txns]
        primary_mode = max(set(modes), key=modes.count)

        # --- Build confidence score ---
        base_score = stability['stability']

        # Transfer mode bonus
        mode_bonus = {'neft_rtgs': 0.20, 'imps': 0.10, 'upi': -0.10, 'other': 0.0}
        base_score += mode_bonus.get(primary_mode, 0.0)

        # Company pattern bonus
        if has_company:
            base_score += 0.15

        # Streak bonus
        streak_bonus = min(0.15, months_detected * 0.03)
        base_score += streak_bonus

        # Penalty: too many transfers per month from same source
        total_txns_in_group = len(group_txns)
        if months_detected > 0 and total_txns_in_group / months_detected > 1.5:
            base_score -= 0.20

        confidence = round(max(0.0, min(1.0, base_score)), 3)

        # Need at least 0.50 confidence (no keyword to rescue it)
        if confidence < 0.50:
            continue

        # --- Determine tier ---
        if has_company and primary_mode in ('neft_rtgs', 'imps'):
            tier = 2
        else:
            tier = 3

        pattern_streams.append({
            'source': source,
            'tier': tier,
            'confidence': confidence,
            'avg_amount': round(mean(amounts), 2),
            'latest_amount': round(stability['latest_amount'], 2),
            'months_detected': months_detected,
            'stability': stability['stability'],
            'transfer_mode': primary_mode,
            'has_keyword': False,
            'has_company': has_company,
            'transactions': [
                {'date': t['date'], 'narration': t['narration'], 'amount': t['amount']}
                for t in monthly
            ],
        })

    # ══════════════════════════════════════════════════════════════
    # COMBINE: keyword streams first (highest priority), then pattern
    # ══════════════════════════════════════════════════════════════
    all_streams = keyword_streams + sorted(
        pattern_streams,
        key=lambda s: (s['confidence'], s['avg_amount']),
        reverse=True,
    )
    return all_streams


# ═══════════════════════════════════════════════════════════════════════
# MAIN ANALYSIS FUNCTION
# ═══════════════════════════════════════════════════════════════════════

def analyze_transactions(transactions: list[dict]) -> dict[str, Any]:
    """
    Analyze a list of bank transactions and classify them.

    Returns
    -------
    dict with keys:
        salary             : salary credits with confidence scores and tier
        salary_streams     : detected recurring income streams
        emi_loans          : EMI payments + loan repayments (merged)
        nach_bounce        : NACH/ECS bounce transactions
        frequent_transfers : repeated transfers (UPI + NEFT/RTGS/IMPS)
                             where total ≥ 20% of monthly income/inflow
        charges            : bank charges / fees (kept for reference)
        summary            : totals, counts, monthly estimates
    """

    # ── Step 1: Detect salary using new top-down engine ──────────
    salary_streams = _detect_salary(transactions)

    # Flatten streams into individual salary transactions
    salary = []
    for stream in salary_streams:
        for txn_entry in stream['transactions']:
            salary.append({
                **txn_entry,
                'confidence': stream['confidence'],
                'tier': stream['tier'],
                'source': stream['source'],
            })

    # ── Step 2: Classify DEBIT transactions (EMI, bounces, charges)
    emi_loans = []
    nach_bounce = []
    charges = []

    total_credits = 0.0
    total_debits = 0.0
    credit_count = 0
    debit_count = 0

    for txn in transactions:
        narr_raw = _get_narration(txn)
        narr = narr_raw.lower()
        txn_type = _get_type(txn)
        amt = _get_amount(txn)
        date = _get_date(txn)

        row = {'date': date, 'narration': narr_raw, 'amount': amt}

        if txn_type == 'CREDIT':
            total_credits += amt
            credit_count += 1
        elif txn_type == 'DEBIT':
            total_debits += amt
            debit_count += 1

        # ── NACH Bounces (highest priority, exit early) ──
        if NACH_BOUNCE_PATTERNS.search(narr):
            nach_bounce.append({**row, 'type': txn_type})
            continue

        if txn_type != 'DEBIT':
            continue

        is_ach_nach = bool(ACH_NACH_PATTERN.search(narr))
        is_upi = bool(UPI_PATTERN.search(narr))
        is_ecom = bool(ECOM_PATTERN.search(narr))
        is_pg_pcd = bool(re.match(r'^(pg\s|pcd/)', narr))
        is_bil = bool(re.match(r'^(bil/onl/|bil/inft/|bil/neft/|inf/inft/)', narr))

        # For UPI: strip the last segment (bank name) before lender matching
        # UPI format: UPI/P2M/ref/RECIPIENT/remark/BANK NAME
        # Without this, "IDFC FIRST BANK" or "EQUITAS SMALL FINANC" at the
        # end would falsely match lender patterns like "idfc first" / "financ"
        if is_upi:
            lender_check_narr = narr.rsplit('/', 1)[0]
        else:
            lender_check_narr = narr
        is_lender = bool(LENDER_PATTERNS.search(lender_check_narr))

        # ── EMI / Loans (merged) ──
        if is_lender and amt >= 500 and (is_ach_nach or is_ecom or SALARY_TRANSFER_MODE.search(narr) or is_upi or is_pg_pcd or is_bil):
            emi_loans.append(row)
        elif is_ach_nach and amt >= 500:
            emi_loans.append(row)
        elif amt >= 500 and EMI_UPI_STRICT.search(narr):
            emi_loans.append(row)

        # ── Bank charges ──
        if CHARGE_PATTERNS.search(narr):
            charges.append(row)

    # ── Step 3: Frequent Transfer Detection ────────────────────
    # Base = monthly income. Use salary if detected, otherwise use
    # total credits (handles savings accounts, freelancers, etc.)
    parsed_dates = [_parse_date_obj(_get_date(t)) for t in transactions]
    parsed_dates = [d for d in parsed_dates if d]
    if parsed_dates:
        delta_months = max(1, (max(parsed_dates).year - min(parsed_dates).year) * 12
                          + (max(parsed_dates).month - min(parsed_dates).month) + 1)
    else:
        delta_months = 3

    total_salary = sum(r['amount'] for r in salary)
    monthly_salary = total_salary / delta_months if total_salary > 0 else 0

    # Monthly inflow = total credits / months (always available)
    monthly_inflow = total_credits / delta_months if total_credits > 0 else 0

    # Use salary if detected, otherwise fall back to total inflow
    # This way frequent transfers always scale to the account's activity
    if monthly_salary >= 20000:
        monthly_base = monthly_salary
        base_label = 'salary'
    elif monthly_inflow > 0:
        monthly_base = monthly_inflow
        base_label = 'inflow'
    else:
        monthly_base = 0
        base_label = 'none'

    # Threshold = 20% of monthly base (salary or inflow)
    transfer_threshold = (0.2 * monthly_base) if monthly_base >= 5000 else 5000

    emi_loan_narrs = {r['narration'] for r in emi_loans}

    dest_groups: dict[str, dict] = {}
    charge_narrs = {r['narration'] for r in charges}

    for txn in transactions:
        narr_raw = _get_narration(txn)
        narr = narr_raw.lower()
        txn_type = _get_type(txn)
        amt = _get_amount(txn)
        date = _get_date(txn)

        if txn_type != 'DEBIT':
            continue
        # Skip already-classified transactions
        if narr_raw in emi_loan_narrs:
            continue
        if NACH_BOUNCE_PATTERNS.search(narr):
            continue
        if narr_raw in charge_narrs:
            continue

        # Extract destination — works for UPI, NEFT, RTGS, IMPS
        dest = _extract_destination(narr)
        if not dest:
            # For NEFT/RTGS/IMPS: extract receiver from narration
            dest = _extract_transfer_dest(narr_raw)
        if not dest:
            continue

        if dest not in dest_groups:
            dest_groups[dest] = {'count': 0, 'total': 0.0, 'transactions': []}
        dest_groups[dest]['count'] += 1
        dest_groups[dest]['total'] += amt
        dest_groups[dest]['transactions'].append({'date': date, 'narration': narr_raw, 'amount': amt})

    frequent_transfers = []
    for dest, info in dest_groups.items():
        if info['count'] >= 2 and info['total'] >= transfer_threshold:
            pct = round((info['total'] / monthly_base) * 100, 1) if monthly_base > 0 else 0
            frequent_transfers.append({
                'destination': dest,
                'count': info['count'],
                'total': round(info['total'], 2),
                'pct_of_income': pct,
                'transactions': info['transactions'],
            })

    frequent_transfers.sort(key=lambda x: x['total'], reverse=True)

    # ── Step 4: Build summary ──────────────────────────────────
    # Primary = highest latest_amount stream, secondary = next
    primary_salary = salary_streams[0]['latest_amount'] if salary_streams else 0
    secondary_salary = salary_streams[1]['latest_amount'] if len(salary_streams) > 1 else 0

    total_emi_loans = sum(r['amount'] for r in emi_loans)
    total_charges = sum(r['amount'] for r in charges)

    return {
        'salary': salary,
        'salary_streams': [
            {
                'source': s['source'],
                'tier': s['tier'],
                'confidence': s['confidence'],
                'avg_amount': s['avg_amount'],
                'latest_amount': s['latest_amount'],
                'months_detected': s['months_detected'],
                'stability': s['stability'],
                'transfer_mode': s['transfer_mode'],
            }
            for s in salary_streams
        ],
        'emi_loans': emi_loans,
        'nach_bounce': nach_bounce,
        'frequent_transfers': frequent_transfers,
        'charges': charges,
        'summary': {
            'total_credits': total_credits,
            'total_debits': total_debits,
            'credit_count': credit_count,
            'debit_count': debit_count,
            'total_salary': total_salary,
            'salary_count': len(salary),
            'monthly_salary_estimate': round(monthly_salary, 2),
            'monthly_inflow': round(monthly_inflow, 2),
            'income_base': base_label,  # 'salary', 'inflow', or 'none'
            'primary_salary': round(primary_salary, 2),
            'secondary_salary': round(secondary_salary, 2),
            'salary_streams_detected': len(salary_streams),
            'total_emi_loans': total_emi_loans,
            'emi_loans_count': len(emi_loans),
            'nach_bounce_count': len(nach_bounce),
            'frequent_transfers_count': len(frequent_transfers),
            'total_charges': total_charges,
            'charges_count': len(charges),
        },
    }
