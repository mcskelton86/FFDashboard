from flask import Flask, render_template, request, jsonify
import re
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import io

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = '1nnEwkIrvAQwIIQDnHBPAYumoTBv04wgOnq_R8A7xuIM'

ALLOWED_EXTENSIONS = {'pdf'}
MAX_FILE_SIZE = 16 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# ===== SAVINGS GOALS =====
# Targets and deadlines for the two goals tracked on the Goals tab.
GOALS = [
    {'key': 'ilr',   'name': 'ILR (Indefinite Leave to Remain)', 'target': 16000.0, 'deadline': '2030-06-23'},
    {'key': 'house', 'name': 'House Deposit',                    'target': 50000.0, 'deadline': '2030-09-30'},
]
GOAL_SPLIT = {'ilr': 0.5, 'house': 0.5}


def get_sheets_client():
    try:
        creds_json = os.getenv('GOOGLE_CREDENTIALS')
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file('creds.json', scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception:
        return None


# ===== CATEGORIZATION =====

# Sentinel for the credit-card payment line on the current account — we want
# to recognise it (so we can exclude it from spending totals) but not surface
# it as a regular category.
CC_PAYMENT = '__cc_payment__'

# (compiled regex, category). Order matters — first match wins.
CATEGORY_RULES = [
    (re.compile(r'NATIONWIDE\s*C/CARD|MEMBER\s*CREDIT\s*CARD', re.IGNORECASE), CC_PAYMENT),
    (re.compile(r'AQUASHORE', re.IGNORECASE), 'Rent'),
    (re.compile(r'NEW\s*FOREST\s*DC|\bFOREST\s*DC\b|NEWFOREST\.GOV|COUNCIL\s*TAX', re.IGNORECASE), 'Council Tax'),
    (re.compile(r'SOUTHERN\s*WATER|\bWATER\s*BOARD\b', re.IGNORECASE), 'Water'),
    (re.compile(r'OCTOPUS\s*ENERGY|BRITISH\s*GAS|EDF\s*ENERGY|\bE\.?ON\b|SCOTTISH\s*POWER|BULB\s*ENERGY|OVO\s*ENERGY', re.IGNORECASE), 'Gas/Electric'),
    (re.compile(r'TROOLI', re.IGNORECASE), 'Internet'),
    (re.compile(r'EE\s*LIMITED|\bEE\s*MOBILE\b', re.IGNORECASE), 'Phones'),
    # Fuel must come before Groceries so "TESCO PAY AT PUMP" doesn't match Tesco.
    (re.compile(r'PAY\s*AT\s*PUMP|SERVICE\s*STATION|\bSHELL\b|\bBP\b|\bESSO\b|TEXACO|JET\s*PETROL|MORRISONS\s*PETROL|SAINSBURY.*PETROL', re.IGNORECASE), 'Fuel'),
    (re.compile(r'TESCO|SAINSBURY|ASDA|MORRISONS|WAITROSE|LIDL|ALDI|CO-?OP|COOP\b|M&S\s*FOOD|MARKS\s*&\s*SPENCER|ICELAND|OCADO|FARMFOODS', re.IGNORECASE), 'Groceries'),
]

# Per-employer person tagging for income recognition. Anything matching one of
# these on a deposit line counts as income; everything else (interest,
# cashback, internal transfers) is excluded.
INCOME_RULES = [
    (re.compile(r"JC\s*(?:OF\s*)?LYMING(?:T?ON)?", re.IGNORECASE), 'Riley'),
    (re.compile(r"L'?ANZA\s*EUROPE", re.IGNORECASE), 'Riley'),
    (re.compile(r'MONKEY\s*BREWHOUSE', re.IGNORECASE), 'Matthew'),
    (re.compile(r'TIRAMOCH', re.IGNORECASE), 'Matthew'),
    (re.compile(r'HUMBUG', re.IGNORECASE), 'Matthew'),
    (re.compile(r'PULSE\s*WELLNESS', re.IGNORECASE), 'Matthew'),
    (re.compile(r'\b(?:WAGES|SALARY)\b', re.IGNORECASE), 'Matthew'),
]


def categorize(description, is_income=False):
    if is_income:
        return 'Income'
    for pattern, cat in CATEGORY_RULES:
        if pattern.search(description or ''):
            return cat
    return 'Other'


def tag_person(description):
    for pattern, person in INCOME_RULES:
        if pattern.search(description or ''):
            return person
    return ''


def is_known_income(description):
    return tag_person(description) != ''


# ===== STATEMENT PARSING =====

_MONTH_ABBREVS = {'Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'}
_AMOUNT_RE = re.compile(r'^-?[\d,]+\.\d{2}$')


def _parse_credit_card_pdf(pdf):
    """Nationwide credit card statement.
    Lines: DD/MM/YY REFNO DESCRIPTION £AMOUNT [CR]
    """
    transactions = []
    line_pattern = re.compile(
        r'^(\d{2}/\d{2}/\d{2})\s+(\d+)\s+(.+?)\s+(-?)£([\d,]+\.\d{2})\s*(CR)?$'
    )
    for page in pdf.pages:
        text = page.extract_text() or ''
        for line in text.split('\n'):
            line = line.strip()
            m = line_pattern.match(line)
            if not m:
                continue
            date_raw, ref_no, description, neg_sign, amount_str, cr_marker = m.groups()
            try:
                amount = float(amount_str.replace(',', ''))
            except ValueError:
                continue
            is_credit = bool(cr_marker) or bool(neg_sign)
            try:
                parsed_date = datetime.strptime(date_raw, '%d/%m/%y')
                date_str = parsed_date.strftime('%d %b %Y')
            except ValueError:
                date_str = date_raw
            transactions.append({
                'date': date_str,
                'description': description.strip()[:100],
                'amountOut': 0 if is_credit else round(amount, 2),
                'amountIn': round(amount, 2) if is_credit else 0,
                'category': categorize(description, is_credit),
                'fitid': ref_no,
            })
    return transactions


def _parse_current_account_pdf(pdf):
    """Nationwide FlexDirect (current account) statement."""
    transactions = []

    statement_year = None
    for page in pdf.pages:
        text = page.extract_text() or ''
        m = re.search(r'Statementdate:?\s+\d{1,2}\s+\w+\s+(\d{4})', text)
        if m:
            statement_year = int(m.group(1))
            break
    if statement_year is None:
        statement_year = datetime.now().year

    cur_date = None

    for page in pdf.pages:
        text = page.extract_text() or ''
        if 'Description' not in text or '£Out' not in text:
            continue

        words = page.extract_words(keep_blank_chars=False)

        col_centers = {}
        for w in words:
            t = w['text']
            if t in ('£Out', '£In', '£Balance'):
                col_centers[t] = (w['x0'] + w['x1']) / 2
        col_centers.setdefault('£Out', 295.0)
        col_centers.setdefault('£In', 350.0)
        col_centers.setdefault('£Balance', 410.0)

        last_txn = None

        lines = {}
        for w in words:
            if w['x0'] >= 443:
                continue
            k = round(w['top'], 0)
            lines.setdefault(k, []).append(w)

        for top in sorted(lines.keys()):
            line_words = sorted(lines[top], key=lambda w: w['x0'])
            if not line_words:
                continue
            texts = [w['text'] for w in line_words]
            joined = ' '.join(texts)

            if any(t in texts for t in ('Description', '£Out', '£In', '£Balance', 'Date')):
                continue
            if any(s in joined for s in ('Statementdate', 'Sortcode', 'Accountno',
                                          'Statementno', 'Startbalance', 'Endbalance',
                                          'transactions(continued)', 'Balance from statement')):
                continue

            desc_start_idx = 0
            if (len(line_words) >= 2 and line_words[0]['text'].isdigit()
                    and line_words[1]['text'] in _MONTH_ABBREVS):
                day = int(line_words[0]['text'])
                mon = line_words[1]['text']
                try:
                    cur_date = datetime.strptime(f'{day} {mon} {statement_year}', '%d %b %Y')
                except ValueError:
                    pass
                desc_start_idx = 2

            desc_words = []
            out_amt = in_amt = bal_amt = None
            for w in line_words[desc_start_idx:]:
                txt = w['text']
                if _AMOUNT_RE.match(txt):
                    centre = (w['x0'] + w['x1']) / 2
                    nearest = min(col_centers.items(), key=lambda kv: abs(centre - kv[1]))[0]
                    val = float(txt.replace(',', ''))
                    if nearest == '£Out':
                        out_amt = val
                    elif nearest == '£In':
                        in_amt = val
                    else:
                        bal_amt = val
                else:
                    desc_words.append(txt)

            desc = ' '.join(desc_words).strip()

            if desc.startswith('Effective Date'):
                continue

            if out_amt is not None or in_amt is not None:
                if cur_date is None:
                    continue
                txn = {
                    'date': cur_date.strftime('%d %b %Y'),
                    'description': desc[:120],
                    'amountOut': round(out_amt, 2) if out_amt else 0,
                    'amountIn': round(in_amt, 2) if in_amt else 0,
                    'fitid': '',
                }
                txn['category'] = categorize(desc, txn['amountIn'] > 0)
                transactions.append(txn)
                last_txn = txn
            elif desc and last_txn is not None:
                last_txn['description'] = (last_txn['description'] + ' ' + desc).strip()[:120]
                last_txn['category'] = categorize(
                    last_txn['description'], last_txn['amountIn'] > 0
                )

    return transactions


def parse_nationwide_pdf(pdf_bytes):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        first_page_text = (pdf.pages[0].extract_text() or '') if pdf.pages else ''
        if 'Credit Card Statement' in first_page_text:
            return _parse_credit_card_pdf(pdf)
        return _parse_current_account_pdf(pdf)


def normalize_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:8], '%Y%m%d').strftime('%d %b %Y')
    except Exception:
        pass
    try:
        return datetime.strptime(date_str, '%d %b %Y').strftime('%d %b %Y')
    except Exception:
        pass
    return date_str


# ===== ROUTES =====

@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/upload')
def upload():
    return render_template('index.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/parse-pdf', methods=['POST'])
def parse_pdf():
    try:
        files = request.files.getlist('file') or request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'No file uploaded'})

        all_transactions = []
        per_file = []
        errors = []
        for f in files:
            try:
                pdf_bytes = f.read()
                if not pdf_bytes:
                    errors.append(f'{f.filename}: empty')
                    continue
                txns = parse_nationwide_pdf(pdf_bytes) or []
                per_file.append({'filename': f.filename, 'count': len(txns)})
                all_transactions.extend(txns)
            except Exception as e:
                errors.append(f'{f.filename}: {e}')

        if not all_transactions:
            return jsonify({'success': False, 'error': 'No transactions found. ' + '; '.join(errors)})

        return jsonify({
            'success': True,
            'transactions': all_transactions,
            'count': len(all_transactions),
            'per_file': per_file,
            'errors': errors,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/save-transactions', methods=['POST'])
def save_transactions():
    try:
        data = request.json
        transactions = data.get('transactions', [])
        if not transactions:
            return jsonify({'success': False, 'error': 'No transactions to save'})

        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed'})

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')

        header = sheet.row_values(1)
        if 'FITID' not in header:
            sheet.update_cell(1, len(header) + 1, 'FITID')
            header = sheet.row_values(1)

        def _num(v):
            try:
                return f'{float(v or 0):.2f}'
            except (TypeError, ValueError):
                return '0.00'

        def _sig(date, desc, out, inn, fitid):
            if fitid:
                return f'fitid:{str(fitid).strip()}'
            return f"{(date or '').strip()}|{(desc or '').strip().lower()}|{_num(out)}|{_num(inn)}"

        existing_records = sheet.get_all_records()
        seen = {}
        rows_to_delete = []
        for idx, r in enumerate(existing_records):
            sig = _sig(r.get('Date'), r.get('Description'),
                       r.get('Amount Out'), r.get('Amount In'), r.get('FITID'))
            if sig in seen:
                rows_to_delete.append(idx + 2)
            else:
                seen[sig] = idx + 2

        cleaned_count = 0
        for row_num in sorted(rows_to_delete, reverse=True):
            sheet.delete_rows(row_num)
            cleaned_count += 1

        existing_sigs = set(seen.keys())

        saved_count = 0
        skipped_count = 0
        rows_to_append = []
        for txn in transactions:
            sig = _sig(txn['date'], txn['description'], txn['amountOut'], txn['amountIn'], txn.get('fitid'))
            if sig in existing_sigs:
                skipped_count += 1
                continue
            existing_sigs.add(sig)
            rows_to_append.append([
                txn['date'],
                txn['description'],
                txn['amountOut'],
                txn['amountIn'],
                txn['category'],
                datetime.now().strftime('%B'),
                datetime.now().strftime('%Y'),
                'PDF Import',
                datetime.now().strftime('%d/%m/%Y'),
                txn.get('fitid', '')
            ])
            saved_count += 1

        if rows_to_append:
            sheet.append_rows(rows_to_append)

        _invalidate_txn_cache()

        msg_parts = [f'Saved {saved_count} transactions']
        if skipped_count:
            msg_parts.append(f'skipped {skipped_count} duplicate{"s" if skipped_count != 1 else ""} from upload')
        if cleaned_count:
            msg_parts.append(f'removed {cleaned_count} existing duplicate{"s" if cleaned_count != 1 else ""} from sheet')
        return jsonify({
            'success': True,
            'message': ', '.join(msg_parts),
            'saved': saved_count,
            'skipped': skipped_count,
            'cleaned': cleaned_count,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/recategorize', methods=['POST'])
def recategorize():
    """Apply current CATEGORY_RULES to every row in the Transactions sheet,
    overwriting the Category column. One-shot maintenance route."""
    try:
        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed'})

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')
        rows = sheet.get_all_values()
        if len(rows) < 2:
            return jsonify({'success': True, 'updated': 0})

        header = rows[0]
        try:
            cat_col_idx = header.index('Category')
        except ValueError:
            return jsonify({'success': False, 'error': 'No Category column found'})

        updates = []
        changed = 0
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 5:
                continue
            desc = row[1] if len(row) > 1 else ''
            try:
                amt_in = float(row[3]) if row[3] else 0
            except ValueError:
                amt_in = 0
            new_cat = categorize(desc, amt_in > 0)
            old_cat = row[cat_col_idx] if len(row) > cat_col_idx else ''
            if new_cat != old_cat:
                col_letter = chr(ord('A') + cat_col_idx)
                updates.append({'range': f'{col_letter}{i}', 'values': [[new_cat]]})
                changed += 1

        if updates:
            sheet.batch_update(updates, value_input_option='USER_ENTERED')

        _invalidate_txn_cache()

        return jsonify({'success': True, 'updated': changed, 'total': len(rows) - 1})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ===== DATA RETRIEVAL =====

_TXN_CACHE = {'data': None, 'ts': 0}
_TXN_CACHE_TTL = 30  # seconds — long enough to coalesce the 3 reads on a single page load

def _invalidate_txn_cache():
    _TXN_CACHE['data'] = None
    _TXN_CACHE['ts'] = 0

def get_all_transactions():
    import time
    now = time.time()
    if _TXN_CACHE['data'] is not None and (now - _TXN_CACHE['ts']) < _TXN_CACHE_TTL:
        return _TXN_CACHE['data']
    try:
        client = get_sheets_client()
        if not client:
            return []
        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')
        rows = sheet.get_all_values()
        transactions = []
        for row in rows[1:]:
            if len(row) >= 5:
                transactions.append({
                    'date': normalize_date(row[0]),
                    'description': row[1],
                    'amountOut': float(row[2]) if row[2] else 0,
                    'amountIn': float(row[3]) if row[3] else 0,
                    'category': row[4] if len(row) > 4 else 'Other',
                })
        _TXN_CACHE['data'] = transactions
        _TXN_CACHE['ts'] = now
        return transactions
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return _TXN_CACHE['data'] or []


def _parse_date(s):
    try:
        return datetime.strptime(s, '%d %b %Y')
    except (ValueError, TypeError):
        return None


def _spend_out(txn):
    """Amount counted as spending. Excludes the credit-card payment line on the
    current account (that money isn't spent until it shows up on the CC
    statement, where its individual line items get categorised)."""
    if txn.get('category') == CC_PAYMENT:
        return 0.0
    return txn.get('amountOut', 0) or 0


def _income_in(txn):
    """Amount counted as income. Only deposits matching a known employer count;
    interest, cashback and internal transfers are excluded."""
    amt = txn.get('amountIn', 0) or 0
    if amt <= 0:
        return 0.0
    if not is_known_income(txn.get('description', '')):
        return 0.0
    return amt


def get_this_month_summary():
    """Latest month with expenses (capped at today)."""
    transactions = get_all_transactions()
    today = datetime.now()

    months_with_spend = set()
    for txn in transactions:
        d = _parse_date(txn['date'])
        if d and d <= today and _spend_out(txn) > 0:
            months_with_spend.add((d.year, d.month))

    if months_with_spend:
        y, m = max(months_with_spend)
        ref = datetime(y, m, 1)
    else:
        ref = today.replace(day=1) - timedelta(days=1)
    current_month = ref.strftime('%B')
    current_year = ref.strftime('%Y')

    total_in = 0.0
    total_out = 0.0
    by_category = {}

    for txn in transactions:
        d = _parse_date(txn['date'])
        if not d:
            continue
        if d.strftime('%B') != current_month or d.strftime('%Y') != current_year:
            continue
        total_in += _income_in(txn)
        out = _spend_out(txn)
        total_out += out
        if out > 0:
            cat = txn.get('category') or 'Other'
            if cat == CC_PAYMENT:
                continue
            by_category[cat] = by_category.get(cat, 0) + out

    net = total_in - total_out
    return {
        'month_label': f'{current_month} {current_year}',
        'total_in': round(total_in, 2),
        'total_out': round(total_out, 2),
        'net': round(net, 2),
        'safe_to_spend': round(max(0, net), 2),
        'by_category': {k: round(v, 2) for k, v in by_category.items()},
    }


def get_12_month_trend():
    transactions = get_all_transactions()
    months = {}
    for i in range(11, -1, -1):
        d = datetime.now() - timedelta(days=30 * i)
        months[d.strftime('%b %Y')] = {'income': 0, 'expenses': 0}

    for txn in transactions:
        d = _parse_date(txn['date'])
        if not d:
            continue
        key = d.strftime('%b %Y')
        if key in months:
            months[key]['income'] += _income_in(txn)
            months[key]['expenses'] += _spend_out(txn)

    labels = list(months.keys())
    return {
        'labels': labels,
        'income': [round(months[m]['income'], 2) for m in labels],
        'expenses': [round(months[m]['expenses'], 2) for m in labels],
    }


def calculate_savings_progress():
    """Total derived savings = sum of (income - spend) over all months, floored
    at 0 (if you spend more than you earn, you haven't saved). Split per
    GOAL_SPLIT and project per-month required to hit each deadline."""
    transactions = get_all_transactions()
    today = datetime.now()

    monthly = {}
    for txn in transactions:
        d = _parse_date(txn['date'])
        if not d or d > today:
            continue
        key = (d.year, d.month)
        b = monthly.setdefault(key, {'in': 0.0, 'out': 0.0})
        b['in'] += _income_in(txn)
        b['out'] += _spend_out(txn)

    total_saved = 0.0
    for v in monthly.values():
        total_saved += max(0, v['in'] - v['out'])

    goals_out = []
    for g in GOALS:
        share = GOAL_SPLIT.get(g['key'], 0)
        saved = round(total_saved * share, 2)
        target = g['target']
        try:
            deadline = datetime.strptime(g['deadline'], '%Y-%m-%d')
        except ValueError:
            deadline = today
        months_remaining = max(
            0,
            (deadline.year - today.year) * 12 + (deadline.month - today.month)
        )
        remaining = max(0, target - saved)
        per_month = round(remaining / months_remaining, 2) if months_remaining else remaining
        pct = round((saved / target) * 100, 1) if target else 0
        goals_out.append({
            'key': g['key'],
            'name': g['name'],
            'target': target,
            'saved': saved,
            'remaining': round(remaining, 2),
            'pct': min(pct, 100),
            'deadline': g['deadline'],
            'months_remaining': months_remaining,
            'per_month_required': per_month,
        })

    return {
        'total_saved': round(total_saved, 2),
        'split': GOAL_SPLIT,
        'goals': goals_out,
    }


# ===== API =====

@app.route('/api/spending')
def api_spending():
    period = request.args.get('period', '').strip()
    transactions = get_all_transactions()

    available_months = set()
    for txn in transactions:
        d = _parse_date(txn['date'])
        if d:
            available_months.add((d.year, d.month))
    months_sorted = sorted(available_months, reverse=True)
    months_list = [f'{y:04d}-{m:02d}' for y, m in months_sorted]
    years_list = sorted({y for y, _ in months_sorted}, reverse=True)
    if not period and months_list:
        period = months_list[0]

    def _in_period(d):
        if period == 'all':
            return True
        if len(period) == 7:
            return d.strftime('%Y-%m') == period
        if len(period) == 4:
            return d.strftime('%Y') == period
        return False

    by_category = {}
    total_out = 0.0
    total_in = 0.0
    for txn in transactions:
        d = _parse_date(txn['date'])
        if not d or not _in_period(d):
            continue
        out = _spend_out(txn)
        total_out += out
        total_in += _income_in(txn)
        if out > 0 and txn.get('category') != CC_PAYMENT:
            cat = txn.get('category') or 'Other'
            by_category[cat] = by_category.get(cat, 0) + out

    return jsonify({
        'period': period,
        'available_months': months_list,
        'available_years': [str(y) for y in years_list],
        'by_category': {k: round(v, 2) for k, v in by_category.items()},
        'total_out': round(total_out, 2),
        'total_in': round(total_in, 2),
    })


@app.route('/api/income')
def api_income():
    period = request.args.get('period', '').strip()
    transactions = get_all_transactions()

    available_months = set()
    for txn in transactions:
        d = _parse_date(txn['date'])
        if d:
            available_months.add((d.year, d.month))
    months_sorted = sorted(available_months, reverse=True)
    months_list = [f'{y:04d}-{m:02d}' for y, m in months_sorted]
    years_list = sorted({y for y, _ in months_sorted}, reverse=True)
    if not period and months_list:
        period = months_list[0]

    def _in_period(d):
        if period == 'all':
            return True
        if len(period) == 7:
            return d.strftime('%Y-%m') == period
        if len(period) == 4:
            return d.strftime('%Y') == period
        return False

    items = []
    total = 0.0
    for txn in transactions:
        amt = _income_in(txn)
        if amt <= 0:
            continue
        d = _parse_date(txn['date'])
        if not d or not _in_period(d):
            continue
        items.append({
            'date': txn['date'],
            'description': txn.get('description', ''),
            'amount': round(amt, 2),
            'person': tag_person(txn.get('description', '')),
        })
        total += amt

    items.sort(key=lambda r: _parse_date(r['date']) or datetime.min, reverse=True)

    return jsonify({
        'period': period,
        'available_months': months_list,
        'available_years': [str(y) for y in years_list],
        'items': items,
        'total': round(total, 2),
    })


@app.route('/api/goals')
def api_goals():
    return jsonify(calculate_savings_progress())


@app.route('/api/dashboard-data')
def api_dashboard_data():
    try:
        summary = get_this_month_summary()
        trend = get_12_month_trend()
        return jsonify({
            'this_month': summary,
            'trend_12_months': trend,
            'goals': calculate_savings_progress(),
            'last_update': datetime.now().strftime('%d %b %Y %H:%M'),
        })
    except Exception as e:
        print(f"Error in dashboard_data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
