from flask import Flask, render_template, request, jsonify
import re
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import base64
from PIL import Image
import io

app = Flask(__name__)

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SHEET_ID = '1nnEwkIrvAQwIIQDnHBPAYumoTBv04wgOnq_R8A7xuIM'

# File upload configuration
UPLOAD_FOLDER = '/tmp/payslips'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'pdf'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

def get_sheets_client():
    """Get authenticated Google Sheets client"""
    try:
        creds_json = os.getenv('GOOGLE_CREDENTIALS')
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file('creds.json', scopes=SCOPES)
        return gspread.authorize(creds)
    except:
        return None

# ===== STATEMENT PARSING =====

_MONTH_ABBREVS = {'Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'}
_AMOUNT_RE = re.compile(r'^-?[\d,]+\.\d{2}$')

def _parse_credit_card_pdf(pdf):
    """Nationwide credit card statement: lines look like
        DD/MM/YY REFNO DESCRIPTION £AMOUNT [CR]
    REFNO is the bank's unique reference and is stored as FITID.
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
                'category': categorize_transaction(description, is_credit),
                'fitid': ref_no,
                'keepUncategorized': False,
            })
    return transactions

def _parse_current_account_pdf(pdf):
    """Nationwide FlexDirect (current account) statement.

    Layout has columns: Date | Description | £Out | £In | £Balance, and
    descriptions span multiple lines. We use word x-coordinates to assign
    amounts to columns since the date-only inheritance and multi-line
    descriptions defeat plain regex parsing.

    No FITID is supplied by the bank in this format, so dedup falls back to
    date+description+amount.
    """
    transactions = []

    # Statement year — used to qualify dates like '25 Feb' that lack a year.
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
        # Skip back-of-statement boilerplate pages (no transaction header).
        if 'Description' not in text or '£Out' not in text:
            continue

        words = page.extract_words(keep_blank_chars=False)

        # Find this page's column header positions so we can bucket amount
        # words even if margins differ slightly between pages.
        col_centers = {}
        for w in words:
            t = w['text']
            if t in ('£Out', '£In', '£Balance'):
                col_centers[t] = (w['x0'] + w['x1']) / 2
        # Fallbacks if a header is missing (e.g. continuation page).
        col_centers.setdefault('£Out', 295.0)
        col_centers.setdefault('£In', 350.0)
        col_centers.setdefault('£Balance', 410.0)

        # Reset continuation tracking per page so trailing text on a page
        # cannot leak into the next page's first transaction.
        last_txn = None

        # Group words into lines by their top-coord.
        lines = {}
        for w in words:
            # Ignore right-margin metadata column (averages, BIC, IBAN, etc.)
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

            # Skip header / metadata rows.
            if any(t in texts for t in ('Description', '£Out', '£In', '£Balance', 'Date')):
                continue
            if any(s in joined for s in ('Statementdate', 'Sortcode', 'Accountno',
                                          'Statementno', 'Startbalance', 'Endbalance',
                                          'transactions(continued)', 'Balance from statement')):
                continue

            # Detect leading date: '<day-num> <Mon>'.
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

            # Bucket each remaining word: amounts go to the nearest of Out/In/Balance
            # column centers; everything else is part of the description.
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

            # Drop "Effective Date ..." metadata lines outright.
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
                    'keepUncategorized': False,
                }
                txn['category'] = categorize_transaction(desc, txn['amountIn'] > 0)
                transactions.append(txn)
                last_txn = txn
            elif desc and last_txn is not None:
                # Continuation line (description spans multiple rows).
                last_txn['description'] = (last_txn['description'] + ' ' + desc).strip()[:120]
                # Re-categorise with the now-richer description.
                last_txn['category'] = categorize_transaction(
                    last_txn['description'], last_txn['amountIn'] > 0
                )

    return transactions

def parse_nationwide_pdf(pdf_bytes):
    """Parse a Nationwide statement PDF — credit card or current account.

    Detects the statement type from the page text and dispatches to the right
    parser. Both produce the same transaction shape.
    """
    import pdfplumber
    import io

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        first_page_text = (pdf.pages[0].extract_text() or '') if pdf.pages else ''
        if 'Credit Card Statement' in first_page_text:
            return _parse_credit_card_pdf(pdf)
        # FlexDirect / current account
        return _parse_current_account_pdf(pdf)

def normalize_date(date_str):
    """Normalize date from various formats to 'DD Mmm YYYY'"""
    if not date_str:
        return None

    # Try parsing as raw OFX format (YYYYMMDD...)
    try:
        date_part = date_str[:8]
        dt = datetime.strptime(date_part, '%Y%m%d')
        return dt.strftime('%d %b %Y')
    except:
        pass

    # Try parsing as already formatted (DD Mmm YYYY)
    try:
        dt = datetime.strptime(date_str, '%d %b %Y')
        return dt.strftime('%d %b %Y')
    except:
        pass

    return date_str

def categorize_transaction(description, is_income=False):
    """Categorize transaction based on description"""
    desc_upper = description.upper()

    # Any positive-amount transaction defaults to Income.
    if is_income:
        return 'Income'

    # Expense categorization. Order matters — first match wins, so put more
    # specific categories before broader ones.
    categories = {
        'Eating Out': ['CAFE', 'COFFEE', 'COSTA', 'STARBUCKS', 'PUB', 'BAR ',
                        'BREWHOUSE', 'RESTAURANT', 'MAYFLOWER', 'TRES BON',
                        'STONEGATE', 'MCDONALD', 'GREGGS', 'PIZZA', 'KFC',
                        'NANDOS', 'WAGAMAMA', 'BURGER'],
        'Groceries': ['TESCO', 'SAINSBURY', 'ASDA', 'MORRISONS', 'WAITROSE', 'LIDL', 'ALDI', 'CO-OP', 'COOP', 'M&S FOOD'],
        'Shopping': ['AMAZON', 'EBAY', 'ARGOS', 'JOHN LEWIS', 'MARKS', 'DEBENHAMS'],
        'Utilities': ['OCTOPUS', 'WATER', 'GAS', 'ELECTRIC', 'EDFENERGY'],
        'Entertainment': ['CINEMA', 'NETFLIX', 'SPOTIFY', 'BT SPORT', 'NOW TV'],
        'Transport': ['UBER', 'TFL', 'SERVICE STATION', 'PETROL', 'FUEL', 'SHELL ', ' BP ', 'ESSO', 'WIGHTLINK', 'PAYBYPHONE'],
        'Subscriptions': ['APPLE.COM', 'MICROSOFT', 'PARAMOUNT', 'CRUNCHYROLL', 'ADOBE', 'SLACK'],
        'Phone & Internet': ['VODAFONE', 'TROOLI', 'VIRGIN MEDIA', 'SKY '],
        'Healthcare': ['SPECSAVERS', 'BOOTS', 'PHARMACY', 'DOCTOR', 'DENTIST', 'NHS'],
        'Council Tax': ['FOREST DC', 'COUNCIL'],
        'Housing': ['RENT', 'MORTGAGE', 'LANDLORD'],
    }

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in desc_upper:
                return category

    return 'Other'

# ===== PAYSLIP HANDLING =====

def parse_payslip_text(ocr_text):
    """Extract net pay, gross pay, hours, and date from OCR'd payslip text.

    Prefers labelled fields (Net Pay, Gross Pay, Payment Date) — falling back
    to looser regexes if labels aren't found.
    """
    result = {
        'amount': None,       # net pay (what hits the bank account)
        'gross': None,
        'hours': None,
        'hourly_rate': None,
        'pension': None,
        'tax': None,
        'ni': None,
        'date': None,
        'employer': None,
    }

    def _money(s):
        try:
            return float(s.replace(',', '').replace('£', '').replace('€', '').strip())
        except (TypeError, ValueError):
            return None

    # Net Pay: £551.20  (or just "Net Pay 551.20")
    m = re.search(r'Net\s*Pay[:\s]*[£€]?\s*([\d,]+\.\d{2})', ocr_text, re.IGNORECASE)
    if m:
        result['amount'] = _money(m.group(1))

    m = re.search(r'Gross\s*Pay[:\s]*[£€]?\s*([\d,]+\.\d{2})', ocr_text, re.IGNORECASE)
    if m:
        result['gross'] = _money(m.group(1))

    # Hours — supports "53.0000 hours", "40 hrs", "Regular Hours 53.00".
    m = re.search(r'(\d{1,4}\.?\d*)\s*(?:hours?|hrs?)\b', ocr_text, re.IGNORECASE)
    if m:
        try:
            result['hours'] = float(m.group(1))
        except ValueError:
            pass

    # Payment Date first; fall back to any DD/MM/YY-ish date.
    m = re.search(r'Payment\s*Date[:\s]*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', ocr_text, re.IGNORECASE)
    if not m:
        m = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', ocr_text)
    if m:
        try:
            day, month, year = m.groups()
            if len(year) == 2:
                year = '20' + year
            result['date'] = datetime(int(year), int(month), int(day)).strftime('%d %b %Y')
        except (ValueError, TypeError):
            pass

    # Hourly rate — e.g. "53.0000 hours @ £13.00" or "Rate 13.00".
    m = re.search(r'@\s*[£€]?\s*([\d,]+(?:\.\d{1,2})?)', ocr_text)
    if not m:
        m = re.search(r'Rate[:\s]+[£€]?\s*([\d,]+(?:\.\d{1,2})?)', ocr_text, re.IGNORECASE)
    if m:
        result['hourly_rate'] = _money(m.group(1))

    # Strip the "Employer Contributions" block before searching for deductions —
    # employer-side NI/Pension are costs to the employer, not deducted from net.
    deductions_text = re.split(r'Employer\s+Contribution', ocr_text, maxsplit=1, flags=re.IGNORECASE)[0]

    for key, label in (('pension', r'Pension'), ('tax', r'(?:PAYE\s*Tax|Tax|PAYE)'), ('ni', r'National\s*Insurance|\bNI\b')):
        m = re.search(rf'(?:{label})[^\d£€\n]{{0,40}}[£€]?\s*([\d,]+\.\d{{2}})', deductions_text, re.IGNORECASE)
        if m:
            result[key] = _money(m.group(1))

    # Employer: line after "PAID BY" (lets us guess Person on save).
    m = re.search(r'PAID\s*BY[\s:]*\n?([^\n]+)', ocr_text, re.IGNORECASE)
    if m:
        result['employer'] = m.group(1).strip()[:80]

    return result

def ocr_via_ocrspace(image_bytes, filename):
    """Send an image to the OCR.space API. Free tier: no setup needed for
    light use with the public 'helloworld' key; for real use set the env var
    OCR_SPACE_API_KEY to your own free key (25k requests/month).

    Returns the raw extracted text, or raises Exception on failure.
    """
    import requests
    api_key = os.environ.get('OCR_SPACE_API_KEY', 'helloworld')
    resp = requests.post(
        'https://api.ocr.space/parse/image',
        files={'file': (filename or 'payslip.jpg', image_bytes)},
        data={
            'apikey': api_key,
            'language': 'eng',
            'OCREngine': '2',
            'isTable': 'true',
            'scale': 'true',
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get('IsErroredOnProcessing'):
        msg = body.get('ErrorMessage') or body.get('ErrorDetails') or 'OCR error'
        if isinstance(msg, list):
            msg = '; '.join(msg)
        raise RuntimeError(f'OCR.space: {msg}')
    parts = [r.get('ParsedText', '') for r in body.get('ParsedResults', [])]
    return '\n'.join(parts)

# ===== API ROUTES =====

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/upload')
def upload():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/payslip-review')
def payslip_review():
    return render_template('payslip_review.html')

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

        # Ensure the sheet has a FITID column (the bank's unique transaction ID,
        # which is the most reliable dedup key).
        header = sheet.row_values(1)
        if 'FITID' not in header:
            sheet.update_cell(1, len(header) + 1, 'FITID')
            header = sheet.row_values(1)

        # Signature: prefer FITID alone; fall back to date|desc|out|in for rows
        # without a FITID (e.g. older imports, or banks that don't supply it).
        def _num(v):
            try:
                return f'{float(v or 0):.2f}'
            except (TypeError, ValueError):
                return '0.00'

        def _sig(date, desc, out, inn, fitid):
            if fitid:
                return f'fitid:{str(fitid).strip()}'
            return f"{(date or '').strip()}|{(desc or '').strip().lower()}|{_num(out)}|{_num(inn)}"

        # Clean duplicates already in the sheet.
        existing_records = sheet.get_all_records()
        seen = {}
        rows_to_delete = []  # 1-indexed sheet row numbers
        for idx, r in enumerate(existing_records):
            sig = _sig(
                r.get('Date'),
                r.get('Description'),
                r.get('Amount Out'),
                r.get('Amount In'),
                r.get('FITID'),
            )
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
            'cleaned': cleaned_count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload-payslip', methods=['POST'])
def upload_payslip():
    """Upload and process payslip image with OCR"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})

        # Validate file extension
        if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS):
            return jsonify({'success': False, 'error': 'Invalid file type. Use PNG, JPG, etc.'})

        ext = file.filename.rsplit('.', 1)[1].lower()
        file_data = file.read()
        image_base64 = ''

        if ext == 'pdf':
            # Digital payslip — extract text directly with pdfplumber.
            with pdfplumber.open(io.BytesIO(file_data)) as pdf:
                ocr_text = '\n'.join((p.extract_text() or '') for p in pdf.pages)
        else:
            image = Image.open(io.BytesIO(file_data))
            if image.mode in ('RGBA', 'LA', 'P'):
                rgb_image = Image.new('RGB', image.size, (255, 255, 255))
                rgb_image.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                image = rgb_image
            ocr_buf = io.BytesIO()
            image.save(ocr_buf, format='PNG')
            ocr_text = ocr_via_ocrspace(ocr_buf.getvalue(), file.filename)
            buffered = io.BytesIO()
            image.save(buffered, format='PNG')
            image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

        extracted = parse_payslip_text(ocr_text)

        # Riley's employer name is "JC of Lymingon Ltd" (typo in the original — keep both spellings).
        emp = (extracted.get('employer') or '').lower()
        full = ocr_text.lower()
        if 'jc of lymingon' in full or 'jc of lymington' in full or 'jc lymingon' in full:
            extracted['person'] = 'Riley'
        else:
            extracted['person'] = 'Matthew'

        return jsonify({
            'success': True,
            'extracted': extracted,
            'image_base64': image_base64,
            'ocr_text': ocr_text
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'OCR processing failed: {str(e)}'})

@app.route('/save-payslip', methods=['POST'])
def save_payslip():
    """Save confirmed payslip data to Google Sheets"""
    try:
        data = request.json or {}
        date_str = data.get('date')
        net = data.get('amount')

        if not date_str or net in (None, ''):
            return jsonify({'success': False, 'error': 'Date and Net Pay are required'})

        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Google Sheets authentication failed'})

        spreadsheet = client.open_by_key(SHEET_ID)
        headers = ['Date', 'Employer', 'Hours', 'Hourly Rate', 'Gross Pay',
                   'Pension', 'Tax', 'NI', 'Net Pay', 'Month', 'Year', 'Upload Date']
        try:
            sheet = spreadsheet.worksheet('Payslips')
        except Exception:
            sheet = spreadsheet.add_worksheet('Payslips', 1000, len(headers))
            sheet.append_row(headers)

        # Derive Month/Year from the payslip date (e.g. "30 Apr 2026").
        month_str, year_str = '', ''
        try:
            d = datetime.strptime(date_str, '%d %b %Y')
            month_str = d.strftime('%b')
            year_str = d.strftime('%Y')
        except ValueError:
            pass

        def _num(v):
            try:
                return float(v) if v not in (None, '') else ''
            except (TypeError, ValueError):
                return ''

        row = [
            date_str,
            data.get('employer', '') or '',
            _num(data.get('hours')),
            _num(data.get('hourly_rate')),
            _num(data.get('gross')),
            _num(data.get('pension')),
            _num(data.get('tax')),
            _num(data.get('ni')),
            _num(net),
            month_str,
            year_str,
            datetime.now().strftime('%d/%m/%Y %H:%M'),
        ]

        # Dedup: same date + net pay = same payslip.
        try:
            existing = sheet.get_all_values()
            for r in existing[1:]:
                if r and r[0] == date_str and len(r) > 8 and r[8] and abs(float(r[8]) - float(net)) < 0.01:
                    return jsonify({'success': True, 'message': 'Payslip already saved (skipped)'})
        except Exception:
            pass

        # Retry once on transient 500.
        import time
        for attempt in range(3):
            try:
                sheet.append_row(row)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))

        return jsonify({
            'success': True,
            'message': 'Payslip saved successfully'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ===== DATA RETRIEVAL =====

def get_all_transactions():
    """Fetch all transactions from Google Sheets"""
    try:
        client = get_sheets_client()
        if not client:
            return []

        sheet = client.open_by_key(SHEET_ID).worksheet('Transactions')
        rows = sheet.get_all_values()

        # Skip header row
        transactions = []
        for row in rows[1:]:
            if len(row) >= 5:
                # Normalize date from stored format
                date_str = normalize_date(row[0])
                transactions.append({
                    'date': date_str,
                    'description': row[1],
                    'amountOut': float(row[2]) if row[2] else 0,
                    'amountIn': float(row[3]) if row[3] else 0,
                    'category': row[4] if len(row) > 4 else 'Other'
                })

        return transactions
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

def get_all_payslips():
    """Fetch all payslips from Google Sheets"""
    try:
        client = get_sheets_client()
        if not client:
            return []

        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            sheet = spreadsheet.worksheet('Payslips')
        except:
            return []

        rows = sheet.get_all_values()

        # Schema: Date | Employer | Hours | Hourly Rate | Gross Pay | Pension | Tax | NI | Net Pay | Month | Year | Upload Date
        def _f(v):
            try:
                return float(str(v).replace(',', '').replace('£', '')) if v not in (None, '') else 0
            except ValueError:
                return 0

        payslips = []
        for row in rows[1:]:
            if not row or not row[0]:
                continue
            date_str = normalize_date(row[0])
            payslips.append({
                'date': date_str,
                'employer': row[1] if len(row) > 1 else '',
                'hours': _f(row[2]) if len(row) > 2 else 0,
                'hourly_rate': _f(row[3]) if len(row) > 3 else 0,
                'gross': _f(row[4]) if len(row) > 4 else 0,
                'pension': _f(row[5]) if len(row) > 5 else 0,
                'tax': _f(row[6]) if len(row) > 6 else 0,
                'ni': _f(row[7]) if len(row) > 7 else 0,
                'amount': _f(row[8]) if len(row) > 8 else 0,  # Net Pay
                'person': 'Matthew',
            })

        return payslips
    except Exception as e:
        print(f"Error fetching payslips: {e}")
        return []

def get_this_month_summary():
    """Get summary for the most recent month that has transaction data.
    We don't have a live bank feed, so rather than the literal current/previous
    calendar month (which may be empty), fall back to the latest month that
    actually has transactions in the sheet."""
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    # Pick the latest month that actually has expenses — we don't have a live
    # bank feed, so a strict "previous calendar month" is often empty.
    today = datetime.now()
    months_with_spend = set()
    for txn in transactions:
        try:
            if txn.get('amountOut', 0) > 0:
                d = datetime.strptime(txn['date'], '%d %b %Y')
                if d <= today:
                    months_with_spend.add((d.year, d.month))
        except (ValueError, TypeError):
            continue

    if months_with_spend:
        y, m = max(months_with_spend)
        ref = datetime(y, m, 1)
    else:
        now = datetime.now()
        ref = now.replace(day=1) - timedelta(days=1)
    current_month = ref.strftime('%B')
    current_year = ref.strftime('%Y')

    total_in = 0
    total_out = 0
    by_category = {}

    # Payslip net amounts for this month — used to suppress matching bank
    # deposits so we don't double-count salary income.
    payslip_nets_this_month = []
    for payslip in payslips:
        try:
            d = datetime.strptime(payslip['date'], '%d %b %Y')
            if d.strftime('%B') == current_month and d.strftime('%Y') == current_year:
                total_in += payslip['amount']
                payslip_nets_this_month.append(payslip['amount'])
        except (ValueError, TypeError):
            continue

    def _matches_payslip(amt):
        for net_amt in payslip_nets_this_month:
            if abs(amt - net_amt) < 1.0:
                return True
        return False

    # Process transactions
    for txn in transactions:
        try:
            txn_date = datetime.strptime(txn['date'], '%d %b %Y')
            if txn_date.strftime('%B') == current_month and txn_date.strftime('%Y') == current_year:
                amt_in = txn['amountIn']
                # Skip income deposits already represented by a payslip.
                if amt_in > 0 and _matches_payslip(amt_in):
                    pass
                else:
                    total_in += amt_in
                total_out += txn['amountOut']

                category = txn['category']
                if category not in by_category:
                    by_category[category] = 0
                by_category[category] += txn['amountOut']
        except:
            continue

    net = total_in - total_out
    safe_to_spend = max(0, net)

    return {
        'month_label': f'{current_month} {current_year}',
        'total_in': round(total_in, 2),
        'total_out': round(total_out, 2),
        'net': round(net, 2),
        'safe_to_spend': round(safe_to_spend, 2),
        'by_category': {k: round(v, 2) for k, v in by_category.items()}
    }

def get_12_month_trend():
    """Get income and expenses for last 12 months"""
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    # Get last 12 months
    months = {}
    for i in range(11, -1, -1):
        date = datetime.now() - timedelta(days=30*i)
        month_key = date.strftime('%b %Y')
        months[month_key] = {'income': 0, 'expenses': 0}

    # Process transactions
    for txn in transactions:
        try:
            txn_date = datetime.strptime(txn['date'], '%d %b %Y')
            month_key = txn_date.strftime('%b %Y')

            if month_key in months:
                months[month_key]['income'] += txn['amountIn']
                months[month_key]['expenses'] += txn['amountOut']
        except:
            continue

    # Process payslips
    for payslip in payslips:
        try:
            payslip_date = datetime.strptime(payslip['date'], '%d %b %Y')
            month_key = payslip_date.strftime('%b %Y')

            if month_key in months:
                months[month_key]['income'] += payslip['amount']
        except:
            continue

    labels = list(months.keys())
    income = [round(months[m]['income'], 2) for m in labels]
    expenses = [round(months[m]['expenses'], 2) for m in labels]

    return {
        'labels': labels,
        'income': income,
        'expenses': expenses
    }

def calculate_income_projections():
    """Calculate income projections with two modes"""
    payslips = get_all_payslips()

    if not payslips:
        return {
            'monthly_average': 0,
            'annual_average': 0,
            'by_schedule_hours': 0,
            'by_actual_hours': 0
        }

    # Get payslips from this year
    current_year = datetime.now().strftime('%Y')
    year_payslips = [p for p in payslips if datetime.strptime(p['date'], '%d %b %Y').strftime('%Y') == current_year]

    if not year_payslips:
        year_payslips = payslips[-3:] if len(payslips) >= 3 else payslips

    # Calculate averages
    total_amount = sum(p['amount'] for p in year_payslips)
    total_hours = sum(p['hours'] for p in year_payslips if p['hours'])

    monthly_average = total_amount / len(year_payslips) if year_payslips else 0
    annual_average = monthly_average * 12

    # Based on schedule hours (assume 40 hrs/week = ~173 hrs/month)
    by_schedule_hours = (monthly_average / total_hours * 173) if total_hours else monthly_average

    # Based on actual averaged hours
    avg_actual_hours = total_hours / len(year_payslips) if year_payslips else 0
    by_actual_hours = (monthly_average / avg_actual_hours * 173) if avg_actual_hours else monthly_average

    return {
        'monthly_average': round(monthly_average, 2),
        'annual_average': round(annual_average, 2),
        'by_schedule_hours': round(by_schedule_hours * 12, 2),
        'by_actual_hours': round(by_actual_hours * 12, 2),
        'average_hours_per_month': round(avg_actual_hours, 2)
    }

# ===== API ENDPOINTS =====

@app.route('/api/spending')
def api_spending():
    """Spending breakdown for a chosen period.

    period query: 'YYYY-MM' (specific month), 'YYYY' (full year), or 'all'.
    Returns by_category, totals, and the list of available months/years for
    populating a selector.
    """
    period = request.args.get('period', '').strip()
    transactions = get_all_transactions()

    available_months = set()
    for txn in transactions:
        try:
            d = datetime.strptime(txn['date'], '%d %b %Y')
            available_months.add((d.year, d.month))
        except (ValueError, TypeError):
            continue
    months_sorted = sorted(available_months, reverse=True)
    months_list = [f'{y:04d}-{m:02d}' for y, m in months_sorted]
    years_list = sorted({y for y, _ in months_sorted}, reverse=True)

    # Default = latest month with data.
    if not period and months_list:
        period = months_list[0]

    def _in_period(d):
        if period == 'all':
            return True
        if len(period) == 7:  # YYYY-MM
            return d.strftime('%Y-%m') == period
        if len(period) == 4:  # YYYY
            return d.strftime('%Y') == period
        return False

    by_category = {}
    total_out = 0.0
    total_in = 0.0
    for txn in transactions:
        try:
            d = datetime.strptime(txn['date'], '%d %b %Y')
        except (ValueError, TypeError):
            continue
        if not _in_period(d):
            continue
        out = txn.get('amountOut', 0) or 0
        total_out += out
        total_in += txn.get('amountIn', 0) or 0
        if out > 0:
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
    """Income for a chosen period (payslips + positive bank transactions),
    deduping deposits that match a payslip net within £1.
    """
    period = request.args.get('period', '').strip()
    transactions = get_all_transactions()
    payslips = get_all_payslips()

    available_months = set()
    for src in (transactions, payslips):
        for r in src:
            try:
                d = datetime.strptime(r['date'], '%d %b %Y')
                available_months.add((d.year, d.month))
            except (ValueError, TypeError):
                continue
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
    payslip_nets_by_month = {}

    for p in payslips:
        try:
            d = datetime.strptime(p['date'], '%d %b %Y')
        except (ValueError, TypeError):
            continue
        ym = d.strftime('%Y-%m')
        payslip_nets_by_month.setdefault(ym, []).append(p['amount'])
        if _in_period(d):
            items.append({
                'date': p['date'],
                'description': f"Payslip — {p.get('employer') or p.get('person', '')}",
                'amount': p['amount'],
                'source': 'payslip',
                'person': p.get('person', 'Matthew'),
            })
            total += p['amount']

    for txn in transactions:
        amt_in = txn.get('amountIn', 0) or 0
        if amt_in <= 0:
            continue
        try:
            d = datetime.strptime(txn['date'], '%d %b %Y')
        except (ValueError, TypeError):
            continue
        ym = d.strftime('%Y-%m')
        nets = payslip_nets_by_month.get(ym, [])
        if any(abs(amt_in - n) < 1.0 for n in nets):
            continue
        if not _in_period(d):
            continue
        desc_upper = txn.get('description', '').upper()
        person = 'Riley' if 'JC LYMINGON' in desc_upper or 'JC OF LYMINGON' in desc_upper or 'JC LYMINGTON' in desc_upper else ''
        items.append({
            'date': txn['date'],
            'description': txn.get('description', ''),
            'amount': amt_in,
            'source': 'bank',
            'person': person,
        })
        total += amt_in

    items.sort(key=lambda r: datetime.strptime(r['date'], '%d %b %Y'), reverse=True)

    return jsonify({
        'period': period,
        'available_months': months_list,
        'available_years': [str(y) for y in years_list],
        'items': items,
        'total': round(total, 2),
    })


@app.route('/api/dashboard-data')
def api_dashboard_data():
    try:
        summary = get_this_month_summary()
        trend = get_12_month_trend()
        projections = calculate_income_projections()
        payslips = get_all_payslips()

        return jsonify({
            'this_month': {
                'month_label': summary['month_label'],
                'total_in': summary['total_in'],
                'total_out': summary['total_out'],
                'net': summary['net'],
                'safe_to_spend': summary['safe_to_spend'],
                'by_category': summary['by_category']
            },
            'trend_12_months': {
                'labels': trend['labels'],
                'income': trend['income'],
                'expenses': trend['expenses']
            },
            'projections': projections,
            'payslips': payslips,
            'last_update': datetime.now().strftime('%d %b %Y %H:%M')
        })
    except Exception as e:
        print(f"Error in dashboard_data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)