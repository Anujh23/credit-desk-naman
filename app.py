from fastapi import FastAPI, Request, UploadFile, File, Query, logger
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests
import re
import uuid
import os
import jwt
import xml.etree.ElementTree as ET
import tempfile
from urllib.parse import quote
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from scoring import calculate_sanction
from analyzer import analyze_transactions
from database import (search_customer, list_products, process_uploaded_files,
                       save_bank_statement, search_bank_statements, get_bank_statement, delete_bank_statement,
                       calculate_behavior_score, init_db, verify_user, log_activity,
                       insert_credit_analysis, update_credit_analysis_pd)

load_dotenv()

app = FastAPI(docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://credit-desk-naman.onrender.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/health")
def health():
    return {"status": "ok"}

BASE_URL = "https://naman.fiulive.finfactor.co.in/finsense/API/V2"

# Finsense credentials loaded server-side only
FINSENSE_USER_ID = os.getenv("NAMAN_FINSENSE_USER_ID", "")
FINSENSE_PASSWORD = os.getenv("NAMAN_FINSENSE_PASSWORD", "")


def get_header():
    """Generate fresh header with new rid and current timestamp."""
    return {
        "rid": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000",
        "channelId": "finsense"
    }


def auth_headers(token):
    """Build authorization headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def safe_request(method, url, **kwargs):
    """Make a request with connection error handling."""
    try:
        resp = requests.request(method, url, **kwargs)
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception:
            return JSONResponse(content={"error": resp.text}, status_code=resp.status_code)
    except requests.exceptions.ConnectionError:
        return JSONResponse(content={"error": "Cannot connect to Finsense server. Check your network/VPN."}, status_code=503)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_EXPIRY_HOURS = 12


def get_current_user(request: Request) -> str:
    """Extract username from JWT in Authorization header. Returns 'anonymous' if missing/invalid."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            payload = jwt.decode(auth_header[7:], JWT_SECRET, algorithms=["HS256"])
            return payload.get("sub", "anonymous")
        except Exception:
            pass
    return "anonymous"


def track(request: Request, action: str, details: str = None):
    """Log user activity with IP address. Best-effort — logging failures (e.g. a
    stale DB connection) must never crash the underlying request."""
    try:
        username = get_current_user(request)
        ip = request.client.host if request.client else None
        log_activity(username, action, details, ip)
    except Exception as e:
        print(f"[track] activity logging failed for {action}: {e}")


# ─── Auth Routes ─────────────────────────────────────────────

@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return JSONResponse(content={"error": "Username and password required"}, status_code=400)
    user = verify_user(username, password)
    if not user:
        return JSONResponse(content={"error": "Invalid username or password"}, status_code=401)
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    log_activity(username, "LOGIN", "JWT login", request.client.host if request.client else None)
    return {"token": token, "username": user["username"], "role": user["role"]}


@app.post("/api/auth/verify")
async def auth_verify(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(content={"valid": False}, status_code=401)
    try:
        payload = jwt.decode(auth_header[7:], JWT_SECRET, algorithms=["HS256"])
        return {"valid": True, "username": payload["sub"], "role": payload["role"]}
    except jwt.ExpiredSignatureError:
        return JSONResponse(content={"valid": False, "error": "Token expired"}, status_code=401)
    except jwt.InvalidTokenError:
        return JSONResponse(content={"valid": False, "error": "Invalid token"}, status_code=401)


# ─── Page Routes ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join("static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ─── Loan Calculator ─────────────────────────────────────────

@app.post("/calculate")
async def calculate(request: Request):
    data = await request.json()
    track(request, "LOAN_CALCULATE", f"Amount: {data.get('loan_amount')}")
    loan_amount = data["loan_amount"]
    start_date = datetime.strptime(data["start_date"], "%Y-%m-%d")
    end_date = datetime.strptime(data["end_date"], "%Y-%m-%d")
    interest_rate = data["interest_rate"]
    processing_fee = data["processing_fee"]

    days = (end_date - start_date).days
    if days <= 0:
        return JSONResponse(content={"detail": "End date must be after start date"}, status_code=400)

    interest_amount = loan_amount * (interest_rate / 100) * days
    gst_on_processing_fee = processing_fee * 0.18
    total_fees = processing_fee + gst_on_processing_fee
    disbursed_amount = loan_amount - total_fees
    total_repayment = loan_amount + interest_amount

    return {
        "loan_amount": round(loan_amount, 2),
        "disbursed_amount": round(disbursed_amount, 2),
        "days": days,
        "interest_rate": interest_rate,
        "interest_amount": round(interest_amount, 2),
        "processing_fee": round(processing_fee, 2),
        "gst_on_processing_fee": round(gst_on_processing_fee, 2),
        "total_fees": round(total_fees, 2),
        "total_repayment": round(total_repayment, 2)
    }


# ─── API Routes ──────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request):
    """1. User Login - get auth token. Uses server-side credentials (never sent to browser)."""
    payload = {
        "header": get_header(),
        "body": {
            "userId": FINSENSE_USER_ID,
            "password": FINSENSE_PASSWORD
        }
    }
    return safe_request("POST", f"{BASE_URL}/User/Login", json=payload)


@app.post("/api/consent-request")
async def consent_request(request: Request):
    """2. Create Consent Request."""
    data = await request.json()
    track(request, "CONSENT_REQUEST", f"CustId: {data.get('custId')}")
    token = data.pop("token")
    payload = {
        "header": get_header(),
        "body": {
            "custId": data["custId"],
            "consentDescription": data.get("consentDescription", "Wealth Management Service"),
            "consentTemplates": data.get("consentTemplates", ["BANK_STATEMENT_ONETIME_SALARY"]),
            "userSessionId": data.get("userSessionId", "sessionid123"),
            "redirectUrl": data.get("redirectUrl", "https://google.co.in")
        }
    }
    return safe_request("POST", f"{BASE_URL}/ConsentRequests", json=payload, headers=auth_headers(token))


@app.post("/api/consent-request-plus")
async def consent_request_plus(request: Request):
    """3. Consent Request Plus (CRP) - used for periodic consent."""
    data = await request.json()
    track(request, "CONSENT_REQUEST_PLUS", f"CustId: {data.get('custId')}")
    token = data.pop("token")
    body = {
        "custId": data["custId"],
        "consentDescription": data.get("consentDescription", "Wealth Management Service"),
        "templateName": data.get("templateName", "BANK_STATEMENT_PERIODIC"),
        "userSessionId": data.get("userSessionId", "sessionid123"),
        "redirectUrl": data.get("redirectUrl", "https://google.co.in"),
        "fip": data.get("fip", [])
    }
    if data.get("ConsentDetails"):
        body["ConsentDetails"] = data["ConsentDetails"]
    payload = {
        "header": get_header(),
        "body": body
    }
    return safe_request("POST", f"{BASE_URL}/ConsentRequestPlus", json=payload, headers=auth_headers(token))


@app.post("/api/consent-status")
async def consent_status(request: Request):
    """4. Check Consent Status."""
    data = await request.json()
    token = data["token"]
    consent_handle = data["consentHandle"]
    cust_id = data["custId"]
    return safe_request("GET", f"{BASE_URL}/ConsentStatus/{consent_handle}/{cust_id}", headers=auth_headers(token))


@app.post("/api/consent-details")
async def consent_details(request: Request):
    """5. Get Consent Details by ID."""
    data = await request.json()
    token = data["token"]
    consent_id = data["consentId"]
    return safe_request("GET", f"{BASE_URL}/Consent/{consent_id}", headers=auth_headers(token))


@app.post("/api/fi-request")
async def fi_request(request: Request):
    """6. FI Request - Request financial information."""
    data = await request.json()
    track(request, "FI_REQUEST", f"CustId: {data.get('custId')}")
    token = data.pop("token")
    payload = {
        "header": get_header(),
        "body": {
            "custId": data["custId"],
            "consentHandleId": data["consentHandleId"],
            "consentId": data["consentId"],
            "dateTimeRangeFrom": data["dateTimeRangeFrom"],
            "dateTimeRangeTo": data["dateTimeRangeTo"]
        }
    }
    return safe_request("POST", f"{BASE_URL}/FIRequest", json=payload, headers=auth_headers(token))


@app.post("/api/fi-status")
async def fi_status(request: Request):
    """7. FI Status - Check data fetch status."""
    data = await request.json()
    token = data["token"]
    consent_id = data["consentId"]
    session_id = data["sessionId"]
    consent_handle_id = data["consentHandleId"]
    cust_id = data["custId"]
    return safe_request("GET", f"{BASE_URL}/FIStatus/{consent_id}/{session_id}/{consent_handle_id}/{cust_id}", headers=auth_headers(token))


@app.post("/api/fi-data-fetch")
async def fi_data_fetch(request: Request):
    """8. FI Data Fetch - Get actual financial data."""
    data = await request.json()
    track(request, "FI_DATA_FETCH", f"ConsentHandle: {data.get('consentHandle')}")
    token = data["token"]
    consent_handle = data["consentHandle"]
    session_id = data["sessionId"]
    params = {}
    if data.get("linkRefNumber"):
        params["linkRefNumber"] = data["linkRefNumber"]
    return safe_request("GET", f"{BASE_URL}/FIDataFetch/{consent_handle}/{session_id}", headers=auth_headers(token), params=params)


@app.post("/api/parse-fi-data")
async def parse_fi_data(request: Request):
    """Parse XML bank statement data from FI fetch response into structured JSON."""
    data = await request.json()
    accounts = []
    try:
        fi_data_list = data.get("fiData", [])
        for fi_item in fi_data_list:
            fip_id = fi_item.get("fipId", "Unknown FIP")
            for datum in fi_item.get("data", []):
                link_ref = datum.get("linkRefNumber", "")
                masked_acc = datum.get("maskedAccNumber", "")
                xml_data = datum.get("decryptedFI", "") or datum.get("data", "")
                if not xml_data:
                    accounts.append({
                        "fipId": fip_id,
                        "linkRefNumber": link_ref,
                        "maskedAccNumber": masked_acc,
                        "error": "No data available"
                    })
                    continue
                try:
                    root = ET.fromstring(xml_data)
                    ns = {"aa": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
                    prefix = "aa:" if ns else ""
                    acc_info = {
                        "fipId": fip_id,
                        "linkRefNumber": link_ref,
                        "maskedAccNumber": masked_acc or root.attrib.get("maskedAccNumber", ""),
                        "type": root.attrib.get("type", ""),
                        "accountType": root.attrib.get("type", "deposit"),
                    }
                    # Parse Profile
                    profile_el = root.find(f".//{prefix}Profile", ns) or root.find(".//{*}Profile")
                    if profile_el is not None:
                        holders = profile_el.find(f".//{prefix}Holders", ns) or profile_el.find(".//{*}Holders")
                        if holders is not None:
                            holder = holders.find(f"{prefix}Holder", ns) or holders.find("{*}Holder")
                            if holder is not None:
                                acc_info["holderName"] = holder.attrib.get("name", "")
                                acc_info["pan"] = holder.attrib.get("pan", "")
                                acc_info["email"] = holder.attrib.get("email", "")
                                acc_info["mobile"] = holder.attrib.get("mobile", "")
                    # Parse Summary
                    summary_el = root.find(f".//{prefix}Summary", ns) or root.find(".//{*}Summary")
                    if summary_el is not None:
                        acc_info["summary"] = dict(summary_el.attrib)
                    # Parse Transactions
                    txns = []
                    for txn_el in root.iter():
                        if "Transaction" in txn_el.tag and txn_el.tag != root.tag:
                            txns.append(dict(txn_el.attrib))
                    acc_info["transactions"] = txns
                    acc_info["transactionCount"] = len(txns)
                    accounts.append(acc_info)
                except ET.ParseError:
                    accounts.append({
                        "fipId": fip_id,
                        "linkRefNumber": link_ref,
                        "maskedAccNumber": masked_acc,
                        "error": "Could not parse XML data",
                        "rawData": xml_data[:500]
                    })
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    return {"accounts": accounts}


@app.post("/api/analyze-statement")
async def analyze_statement(request: Request):
    """Analyze bank transactions server-side. Patterns never reach the browser."""
    try:
        data = await request.json()
        track(request, "ANALYZE_STATEMENT", f"Txn count: {len(data.get('transactions', []))}")
        transactions = data.get('transactions', [])
        if not transactions:
            return JSONResponse(content={"error": "No transactions provided"}, status_code=400)
        result = analyze_transactions(transactions)
        return {"success": True, **result}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/analyze-credit")
async def analyze_credit(request: Request):
    try:
        data = await request.json()
        track(request, "CREDIT_ANALYSIS", f"Income: {data.get('monthly_income')}, CIBIL: {data.get('cibil')}")
        required = ['monthly_income', 'fixed_obligations', 'cibil', 'cibil_overdue', 'emi_loan', 'payday_running', 'residence_type', 'enach_bounce']
        for f in required:
            if f not in data:
                return JSONResponse(content={"error": f"Missing field: {f}"}, status_code=400)
        processed = {
            'monthly_income': int(data['monthly_income']),
            'fixed_obligations': int(data['fixed_obligations']),
            'cibil': int(data['cibil']),
            'cibil_overdue': int(data['cibil_overdue']),
            'emi_loan': int(data['emi_loan']),
            'payday_running': int(data['payday_running']),
            'residence_type': str(data['residence_type']).lower(),
            'enach_bounce': int(data['enach_bounce']),
            'docs_collected': int(data.get('docs_collected', 0)),
        }

        # Validate input ranges — reject if out of bounds
        rejection_reasons = []
        if not (300 <= processed['cibil'] <= 900):
            rejection_reasons.append('CIBIL Score out of range')
        if processed['cibil_overdue'] > 10:
            rejection_reasons.append('Too many CIBIL Overdues')
        if processed['emi_loan'] > 10:
            rejection_reasons.append('Too many Active EMI/Loans')
        if processed['payday_running'] > 10:
            rejection_reasons.append('Too many Running Payday Loans')
        if processed['enach_bounce'] > 10:
            rejection_reasons.append('Too many eNACH Bounces')

        username = get_current_user(request)

        if rejection_reasons:
            # Log rejected analysis to DB
            try:
                row_id = insert_credit_analysis({
                    'created_by': username,
                    'monthly_salary': processed['monthly_income'],
                    'cibil_score': processed['cibil'],
                    'cibil_overdue': processed['cibil_overdue'],
                    'active_emi': processed['emi_loan'],
                    'payday_loans': processed['payday_running'],
                    'residence_type': processed['residence_type'],
                    'enach_bounces': processed['enach_bounce'],
                    'status': 'Cannot be Approved',
                    'worthiness_score': 0,
                    'obligation_pct': 0,
                    'sanction_pct_min': 0, 'sanction_pct_max': 0,
                    'sanction_min': 0, 'sanction_max': 0,
                })
            except Exception as e:
                logger.warning(f"Failed to log credit analysis: {e}")
                row_id = None

            return {
                'analysis_id': row_id,
                'approval_status': 'Cannot be Approved',
                'customer_worthiness': 0,
                'max_score': 10.0,
                'obligation_ratio': 0,
                'sanction_amount_range': '₹0',
                'sanction_percentage': {'min': 0, 'max': 0},
                'rejection_reasons': rejection_reasons
            }

        result = calculate_sanction(processed)

        # Parse sanction range for DB storage
        sanction_min = result.get('sanction_percentage', [0, 0])[0]
        sanction_max = result.get('sanction_percentage', [0, 0])[1]
        # Parse sanction amount range (e.g. "₹12,000 - ₹15,000")
        amounts = re.findall(r'[\d,]+', result.get('sanction_amount_range', ''))
        s_min = int(amounts[0].replace(',', '')) if len(amounts) >= 1 else 0
        s_max = int(amounts[1].replace(',', '')) if len(amounts) >= 2 else s_min

        try:
            row_id = insert_credit_analysis({
                'created_by': username,
                'monthly_salary': processed['monthly_income'],
                'cibil_score': processed['cibil'],
                'cibil_overdue': processed['cibil_overdue'],
                'active_emi': processed['emi_loan'],
                'payday_loans': processed['payday_running'],
                'residence_type': processed['residence_type'],
                'enach_bounces': processed['enach_bounce'],
                'status': result['decision'],
                'worthiness_score': round(result['final_score'], 1),
                'obligation_pct': round(result['obligation_ratio'], 1),
                'sanction_pct_min': round(sanction_min, 2),
                'sanction_pct_max': round(sanction_max, 2),
                'sanction_min': s_min,
                'sanction_max': s_max,
            })
        except Exception as e:
            logger.warning(f"Failed to log credit analysis: {e}")
            row_id = None

        return {
            'analysis_id': row_id,
            'approval_status': result['decision'],
            'customer_worthiness': round(result['final_score'], 1),
            'max_score': 10.0,
            'obligation_ratio': round(result['obligation_ratio'], 1),
            'sanction_amount_range': result['sanction_amount_range'],
            'sanction_percentage': {
                'min': round(result['sanction_percentage'][0], 2),
                'max': round(result['sanction_percentage'][1], 2)
            }
        }
    except ValueError as e:
        return JSONResponse(content={"error": f"Invalid input values: {str(e)}"}, status_code=400)
    except Exception as e:
        return JSONResponse(content={"error": f"Server error: {str(e)}"}, status_code=500)


@app.post("/api/update-pd-details")
async def update_pd_details(request: Request):
    """Update PD details on an existing credit analysis row."""
    try:
        data = await request.json()
        analysis_id = data.get('analysis_id')
        if not analysis_id:
            return JSONResponse(content={"error": "Missing analysis_id"}, status_code=400)

        track(request, "PD_UPDATE", f"Analysis ID: {analysis_id}, Customer: {data.get('customer_name')}")

        def parse_num(val):
            """Strip commas, dashes, currency symbols — return float or None."""
            if val is None: return None
            cleaned = str(val).replace(',', '').replace('₹', '').replace('-', '').strip()
            try: return float(cleaned) if cleaned else None
            except ValueError: return None

        sanction_amount = parse_num(data.get('sanction_amount'))
        admin_fee = parse_num(data.get('admin_fee'))
        roi = parse_num(data.get('roi'))
        repay = data.get('repayment_date') or None

        days_raw = str(data.get('num_days', '') or '').replace('Days', '').replace('days', '').replace('-', '').strip()
        try: num_days = int(days_raw) if days_raw else None
        except ValueError: num_days = None

        pd_data = {
            'customer_name': data.get('customer_name') or None,
            'location': data.get('location') or None,
            'case_type': data.get('case_type') or None,
            'contact_number': data.get('contact_number') or None,
            'home_address': data.get('home_address') or None,
            'office_address': data.get('office_address') or None,
            'salary_bank': data.get('salary_bank') or None,
            'sanction_amount': sanction_amount,
            'roi': roi,
            'admin_fee': admin_fee,
            'repayment_date': repay,
            'num_days': num_days,
            'pd_location_time': data.get('pd_location_time') or None,
            'verification_type': data.get('verification_type') or None,
            'remarks': data.get('remarks') or None,
        }

        updated = update_credit_analysis_pd(analysis_id, pd_data)
        if updated:
            return {"success": True, "message": "PD details saved"}
        else:
            return JSONResponse(content={"error": "Analysis record not found"}, status_code=404)

    except Exception as e:
        return JSONResponse(content={"error": f"Server error: {str(e)}"}, status_code=500)


# ─── Customer Insights Routes ────────────────────────────────

@app.get("/api/ci-databases")
async def ci_databases():
    """List all customer insight product databases."""
    try:
        products = list_products()
        return {"databases": products}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/ci-search")
async def ci_search(request: Request):
    """Search records by PAN, Name, or Mobile across all products."""
    data = await request.json()
    track(request, "CI_SEARCH", f"PAN: {data.get('pan')}, Name: {data.get('name')}, Mobile: {data.get('mobile')}")
    if not any([data.get("pan"), data.get("name"), data.get("mobile")]):
        return JSONResponse(content={"error": "At least one search parameter required"}, status_code=400)
    try:
        result = search_customer(pan=data.get("pan"), name=data.get("name"), mobile=data.get("mobile"))
        return result
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/behavior-score")
async def behavior_score(request: Request):
    """Calculate customer behavior score from loan history."""
    data = await request.json()
    track(request, "BEHAVIOR_SCORE", f"PAN: {data.get('pan')}, Name: {data.get('name')}, Mobile: {data.get('mobile')}")
    if not any([data.get("pan"), data.get("name"), data.get("mobile")]):
        return JSONResponse(content={"error": "Provide PAN, name, or mobile to search"}, status_code=400)
    try:
        result = calculate_behavior_score(pan=data.get("pan"), name=data.get("name"), mobile=data.get("mobile"))
        return result
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/ci-upload")
async def ci_upload(request: Request, disbursed: UploadFile = File(...), collection: UploadFile = File(...)):
    """Upload disbursed and collection CSV files."""
    track(request, "CI_UPLOAD", f"Files: {disbursed.filename}, {collection.filename}")
    import pandas as pd
    from io import StringIO, BytesIO

    def read_file(f_content, filename):
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext in ("xlsx", "xls"):
            return pd.read_excel(BytesIO(f_content), dtype=str)
        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                return pd.read_csv(StringIO(f_content.decode(enc)), dtype=str)
            except UnicodeDecodeError:
                continue
        raise ValueError(f"Cannot decode {filename}")

    try:
        disbursed_content = await disbursed.read()
        collection_content = await collection.read()
        disbursed_df = read_file(disbursed_content, disbursed.filename)
        collection_df = read_file(collection_content, collection.filename)
        loan_col = disbursed_df.iloc[0].get("Loan No") or disbursed_df.iloc[0].get("Loan_No") or ""
        product = str(loan_col).strip()[:3].upper() or "UNK"
        result = process_uploaded_files(disbursed_df, collection_df, product)
        return {
            "success": True,
            "product": result["product"],
            "message": f"{result['product']}: {result['disbursed_inserted']} disbursed, {result['collection_inserted']} collection records processed"
        }
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/ci-export")
async def ci_export(request: Request):
    """Export records to CSV."""
    import pandas as pd
    data = await request.json()
    records = data.get("records", [])
    if not records:
        return JSONResponse(content={"error": "No records to export"}, status_code=400)
    try:
        df = pd.DataFrame(records)
        path = os.path.join(tempfile.gettempdir(), "ci_export.csv")
        df.to_csv(path, index=False)
        return FileResponse(path, media_type="text/csv", filename="customer_export.csv")
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ─── Bank Statement Storage Routes ────────────────────────────

@app.post("/api/save-statement")
async def api_save_statement(request: Request):
    """Save a fetched bank statement to the database."""
    data = await request.json()
    track(request, "SAVE_STATEMENT", f"Customer: {data.get('account', {}).get('holderName', 'unknown')}")
    account_data = data.get("account")
    if not account_data:
        return JSONResponse(content={"error": "No account data provided"}, status_code=400)
    try:
        result = save_bank_statement(account_data, created_by=data.get("created_by"))
        return result
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/search-statements")
async def api_search_statements(request: Request):
    """Search saved bank statements by customer name or mobile."""
    data = await request.json()
    track(request, "SEARCH_STATEMENTS", f"Name: {data.get('name')}, Mobile: {data.get('mobile')}")
    name = data.get("name")
    mobile = data.get("mobile")
    if not name and not mobile:
        return JSONResponse(content={"error": "Provide customer name or mobile number"}, status_code=400)
    try:
        results = search_bank_statements(name=name, mobile=mobile)
        return {"success": True, "results": results, "total": len(results)}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/get-statement/{statement_id}")
async def api_get_statement(statement_id: int):
    """Load a full saved bank statement by ID."""
    try:
        result = get_bank_statement(statement_id)
        if not result:
            return JSONResponse(content={"error": "Statement not found"}, status_code=404)
        return {"success": True, "statement": result}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.delete("/api/delete-statement/{statement_id}")
async def api_delete_statement(request: Request, statement_id: int, admin_key: str = Query(..., description="Admin key required to delete data")):
    """Delete a saved bank statement. Requires admin key."""
    track(request, "DELETE_STATEMENT", f"Statement ID: {statement_id}")
    expected_key = os.getenv("ADMIN_DELETE_KEY", "creditdesk-admin-delete")
    if admin_key != expected_key:
        return JSONResponse(content={"error": "Unauthorized. Admin access required to delete data."}, status_code=403)
    try:
        deleted = delete_bank_statement(statement_id)
        if not deleted:
            return JSONResponse(content={"error": "Statement not found"}, status_code=404)
        return {"success": True, "message": "Statement deleted"}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ─── PAN Verification Routes ─────────────────────────────────

CASHFREE_CLIENT_ID = os.getenv("Client_ID", "").strip()
CASHFREE_CLIENT_SECRET = os.getenv("Client_Secret", "").strip()
CASHFREE_PAN_URL = os.getenv(
    "CASHFREE_PAN_URL",
    "https://api.cashfree.com/verification/pan/advance",
).strip()


@app.post("/api/verify-pan")
async def verify_pan(request: Request):
    """Verify a PAN card using Cashfree PAN Advance (PAN 360) API."""
    data = await request.json()
    pan_number = (data.get("pan") or "").strip().upper()
    if not pan_number or len(pan_number) != 10:
        return JSONResponse(content={"error": "Invalid PAN number. Must be 10 characters."}, status_code=400)
    if not CASHFREE_CLIENT_ID or not CASHFREE_CLIENT_SECRET:
        return JSONResponse(content={"error": "Cashfree credentials not configured on server."}, status_code=500)
    verification_id = f"pan-{uuid.uuid4().hex[:16]}"
    try:
        resp = requests.post(
            CASHFREE_PAN_URL,
            json={"pan": pan_number, "verification_id": verification_id},
            headers={
                "x-client-id": CASHFREE_CLIENT_ID,
                "x-client-secret": CASHFREE_CLIENT_SECRET,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except requests.exceptions.ConnectionError:
        return JSONResponse(content={"error": "Cannot connect to PAN verification service."}, status_code=503)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


CASHFREE_EMPLOYMENT_URL = "https://api.cashfree.com/verification/advance-employment"


@app.post("/api/verify-employment")
async def verify_employment(request: Request):
    """Verify employment via Cashfree Advance Employment API (UAN/EPFO). Accepts phone OR uan."""
    data = await request.json()
    phone = (data.get("phone") or "").strip()
    uan = (data.get("uan") or "").strip()
    track(request, "VERIFY_EMPLOYMENT", f"phone={phone or '-'} uan={uan or '-'}")
    if not phone and not uan:
        return JSONResponse(content={"error": "Provide either phone (10 digits) or uan (12 digits)."}, status_code=400)
    if phone and (len(phone) != 10 or not phone.isdigit()):
        return JSONResponse(content={"error": "Phone must be 10 digits."}, status_code=400)
    if uan and (len(uan) != 12 or not uan.isdigit()):
        return JSONResponse(content={"error": "UAN must be 12 digits."}, status_code=400)
    if not CASHFREE_CLIENT_ID or not CASHFREE_CLIENT_SECRET:
        return JSONResponse(content={"error": "Cashfree credentials not configured on server."}, status_code=500)
    verification_id = f"emp-{uuid.uuid4().hex[:16]}"
    body = {"verification_id": verification_id}
    if phone:
        body["phone"] = phone
    if uan:
        body["uan"] = uan
    try:
        resp = requests.post(
            CASHFREE_EMPLOYMENT_URL,
            json=body,
            headers={
                "x-client-id": CASHFREE_CLIENT_ID,
                "x-client-secret": CASHFREE_CLIENT_SECRET,
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except requests.exceptions.ConnectionError:
        return JSONResponse(content={"error": "Cannot connect to Cashfree employment verification service."}, status_code=503)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ─── Mandate / Subscription Routes (Cashfree Payment Gateway) ─────────────
# NOTE: Subscriptions are a Payment Gateway product, separate from the Verification
# suite used above. They can require different API keys. We look for dedicated PG
# creds first and fall back to the verification creds if none are set.
CASHFREE_PG_CLIENT_ID = (os.getenv("mandate_id") or os.getenv("CASHFREE_PG_CLIENT_ID") or CASHFREE_CLIENT_ID).strip()
CASHFREE_PG_CLIENT_SECRET = (os.getenv("mandate_Secret") or os.getenv("CASHFREE_PG_CLIENT_SECRET") or CASHFREE_CLIENT_SECRET).strip()
CASHFREE_PG_BASE = os.getenv("CASHFREE_PG_BASE", "https://api.cashfree.com/pg").strip().rstrip("/")
CASHFREE_API_VERSION = os.getenv("CASHFREE_API_VERSION", "2025-01-01").strip()
CASHFREE_PG_MODE = "sandbox" if "sandbox" in CASHFREE_PG_BASE.lower() else "production"

# Local store of created mandates (Cashfree has no list-all API). NOTE: this file is
# ephemeral on Render (wiped on redeploy) and holds customer PII — gitignored.
MANDATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mandates.json")
IST = timezone(timedelta(hours=5, minutes=30))


def _load_mandates():
    try:
        with open(MANDATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_mandate(record):
    """Append (or replace by subscription_id) a mandate in the local store. Best-effort."""
    try:
        mandates = [m for m in _load_mandates() if m.get("subscription_id") != record.get("subscription_id")]
        mandates.append(record)
        with open(MANDATES_FILE, "w", encoding="utf-8") as f:
            json.dump(mandates, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[mandates] failed to save {record.get('subscription_id')}: {e}")


def _pg_headers():
    return {
        "x-client-id": CASHFREE_PG_CLIENT_ID,
        "x-client-secret": CASHFREE_PG_CLIENT_SECRET,
        "x-api-version": CASHFREE_API_VERSION,
        "Content-Type": "application/json",
    }


@app.post("/api/create-subscription")
async def create_subscription(request: Request):
    """Create an eNACH / UPI Autopay mandate (Cashfree subscription) and notify the
    customer via Email/SMS to complete authorization."""
    data = await request.json()
    name = (data.get("customer_name") or "").strip()
    email = (data.get("customer_email") or "").strip()
    phone = (data.get("customer_phone") or "").strip()
    plan_type = (data.get("plan_type") or "ON_DEMAND").strip().upper()

    if not name or not email or not phone:
        return JSONResponse(content={"error": "Customer name, email and phone are required."}, status_code=400)
    if len(phone) != 10 or not phone.isdigit():
        return JSONResponse(content={"error": "Phone must be 10 digits."}, status_code=400)
    if "@" not in email:
        return JSONResponse(content={"error": "Enter a valid email address."}, status_code=400)
    if not CASHFREE_PG_CLIENT_ID or not CASHFREE_PG_CLIENT_SECRET:
        return JSONResponse(content={"error": "Cashfree PG credentials not configured on server."}, status_code=500)

    subscription_id = (data.get("subscription_id") or "").strip() or f"SUB-{uuid.uuid4().hex[:12]}"

    # ── Plan details (inline / ad-hoc plan) ──
    plan_details = {
        "plan_name": (data.get("plan_name") or "Loan Repayment Mandate").strip(),
        "plan_type": plan_type,
        "plan_currency": "INR",
    }
    if plan_type == "PERIODIC":
        try:
            plan_amount = float(data.get("plan_amount") or 0)
        except (TypeError, ValueError):
            plan_amount = 0
        if plan_amount <= 0:
            return JSONResponse(content={"error": "Enter a valid amount per cycle for a PERIODIC plan."}, status_code=400)
        plan_details["plan_amount"] = plan_amount
        plan_details["plan_interval_type"] = (data.get("plan_interval_type") or "MONTH").strip().upper()
        plan_details["plan_intervals"] = int(data.get("plan_intervals") or 1)
        max_cycles = data.get("plan_max_cycles")
        if max_cycles:
            plan_details["plan_max_cycles"] = int(max_cycles)
    else:  # ON_DEMAND
        try:
            plan_max_amount = float(data.get("plan_max_amount") or 0)
        except (TypeError, ValueError):
            plan_max_amount = 0
        if plan_max_amount <= 0:
            return JSONResponse(content={"error": "Enter a valid maximum amount for an ON_DEMAND plan."}, status_code=400)
        plan_details["plan_max_amount"] = plan_max_amount

    # ── Customer details ──
    customer_details = {
        "customer_name": name,
        "customer_email": email,
        "customer_phone": phone,
    }
    for key in ("customer_bank_account_number", "customer_bank_ifsc", "customer_bank_account_holder_name"):
        val = (data.get(key) or "").strip()
        if val:
            customer_details[key] = val
    acc_type = (data.get("customer_bank_account_type") or "").strip().upper()
    if acc_type:
        customer_details["customer_bank_account_type"] = acc_type

    payment_methods = data.get("payment_methods") or ["enach", "upi"]
    notification_channel = data.get("notification_channel") or ["EMAIL", "SMS"]
    try:
        auth_amount = float(data.get("authorization_amount") or 1)
    except (TypeError, ValueError):
        auth_amount = 1

    body = {
        "subscription_id": subscription_id,
        "customer_details": customer_details,
        "plan_details": plan_details,
        "authorization_details": {
            "authorization_amount": auth_amount,
            "authorization_amount_refund": True,
            "payment_methods": payment_methods,
        },
        "subscription_meta": {
            "return_url": (data.get("return_url") or "").strip() or "https://credit-desk-naman.onrender.com/",
            "notification_channel": notification_channel,
        },
    }
    for src, dst in (("subscription_expiry_time", "subscription_expiry_time"),
                     ("subscription_first_charge_time", "subscription_first_charge_time"),
                     ("subscription_note", "subscription_note")):
        val = (data.get(src) or "").strip()
        if val:
            body[dst] = val

    track(request, "CREATE_MANDATE", f"sub={subscription_id} phone={phone} type={plan_type}")
    try:
        resp = requests.post(
            f"{CASHFREE_PG_BASE}/subscriptions",
            json=body,
            headers=_pg_headers(),
            timeout=20,
        )
        out = resp.json()
        if isinstance(out, dict):
            out.setdefault("subscription_id", subscription_id)
            if resp.status_code < 300 and out.get("subscription_status"):
                _save_mandate({
                    "subscription_id": out.get("subscription_id", subscription_id),
                    "cf_subscription_id": out.get("cf_subscription_id"),
                    "customer_name": name,
                    "customer_phone": phone,
                    "customer_email": email,
                    "plan_max_amount": plan_details.get("plan_max_amount"),
                    "subscription_session_id": out.get("subscription_session_id"),
                    "status_at_creation": out.get("subscription_status"),
                    "created_at": datetime.now(IST).isoformat(),
                })
        return JSONResponse(content=out, status_code=resp.status_code)
    except requests.exceptions.ConnectionError:
        return JSONResponse(content={"error": "Cannot connect to Cashfree subscription service."}, status_code=503)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/subscription-status")
async def subscription_status(request: Request):
    """Fetch a mandate (subscription) status by subscription_id from Cashfree."""
    data = await request.json()
    subscription_id = (data.get("subscription_id") or "").strip()
    if not subscription_id:
        return JSONResponse(content={"error": "Subscription ID is required."}, status_code=400)
    if not CASHFREE_PG_CLIENT_ID or not CASHFREE_PG_CLIENT_SECRET:
        return JSONResponse(content={"error": "Cashfree PG credentials not configured on server."}, status_code=500)
    track(request, "MANDATE_STATUS", f"sub={subscription_id}")
    try:
        resp = requests.get(
            f"{CASHFREE_PG_BASE}/subscriptions/{quote(subscription_id, safe='')}",
            headers=_pg_headers(),
            timeout=20,
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except requests.exceptions.ConnectionError:
        return JSONResponse(content={"error": "Cannot connect to Cashfree subscription service."}, status_code=503)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/list-mandates")
async def list_mandates(request: Request):
    """Return all locally-stored mandates, refreshing each one's live status from Cashfree."""
    track(request, "LIST_MANDATES")
    stored = _load_mandates()
    have_creds = bool(CASHFREE_PG_CLIENT_ID and CASHFREE_PG_CLIENT_SECRET)
    headers = _pg_headers()
    out = []
    for m in stored:
        sid = m.get("subscription_id")
        rec = {
            "subscription_id": sid,
            "cf_subscription_id": m.get("cf_subscription_id"),
            "customer_name": m.get("customer_name"),
            "customer_phone": m.get("customer_phone"),
            "customer_email": m.get("customer_email"),
            "plan_max_amount": m.get("plan_max_amount"),
            "created_at": m.get("created_at"),
            "status": m.get("status_at_creation"),
            "live": False,
        }
        if have_creds and sid:
            try:
                r = requests.get(
                    f"{CASHFREE_PG_BASE}/subscriptions/{quote(sid, safe='')}",
                    headers=headers, timeout=10,
                )
                if r.status_code < 300:
                    j = r.json()
                    if isinstance(j, dict) and j.get("subscription_status"):
                        rec["status"] = j.get("subscription_status")
                        rec["live"] = True
                        plan = j.get("plan_details") or {}
                        if plan.get("plan_max_amount") is not None:
                            rec["plan_max_amount"] = plan.get("plan_max_amount")
            except Exception:
                pass  # keep last-known status
        out.append(rec)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return JSONResponse(content={"mandates": out})


def _fetch_and_save_mandate(sid):
    """Fetch a subscription from Cashfree by id and upsert it into the local store.
    Returns True on success. Used by the webhook to auto-record mandates."""
    if not (sid and CASHFREE_PG_CLIENT_ID and CASHFREE_PG_CLIENT_SECRET):
        return False
    try:
        r = requests.get(
            f"{CASHFREE_PG_BASE}/subscriptions/{quote(sid, safe='')}",
            headers=_pg_headers(), timeout=10,
        )
        if r.status_code >= 300:
            return False
        j = r.json()
        if not (isinstance(j, dict) and j.get("subscription_status")):
            return False
        cust = j.get("customer_details") or {}
        plan = j.get("plan_details") or {}
        _save_mandate({
            "subscription_id": j.get("subscription_id", sid),
            "cf_subscription_id": j.get("cf_subscription_id"),
            "customer_name": cust.get("customer_name"),
            "customer_phone": cust.get("customer_phone"),
            "customer_email": cust.get("customer_email"),
            "plan_max_amount": plan.get("plan_max_amount") if plan.get("plan_max_amount") is not None else plan.get("plan_amount"),
            "subscription_session_id": j.get("subscription_session_id"),
            "status_at_creation": j.get("subscription_status"),
            "created_at": j.get("subscription_created_at") or j.get("created_at") or datetime.now(IST).isoformat(),
        })
        return True
    except Exception as e:
        print(f"[mandates] fetch/save failed for {sid}: {e}")
        return False


def _extract_subscription_id(payload):
    """Pull a subscription_id out of a Cashfree webhook payload (shape varies by event type)."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for holder in (data.get("subscription_details"), data.get("subscription"), data, payload):
        if isinstance(holder, dict) and holder.get("subscription_id"):
            return str(holder["subscription_id"])
    return None


def _verify_webhook_signature(raw_body, timestamp, signature):
    """Cashfree webhook signature = base64(HMAC-SHA256(secret, timestamp + rawBody))."""
    if not (timestamp and signature and CASHFREE_PG_CLIENT_SECRET):
        return False
    try:
        signed = (timestamp + raw_body.decode("utf-8")).encode("utf-8")
        digest = hmac.new(CASHFREE_PG_CLIENT_SECRET.encode("utf-8"), signed, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode("utf-8"), signature)
    except Exception:
        return False


@app.post("/webhook/cashfree/subscription")
async def cashfree_subscription_webhook(request: Request):
    """Cashfree pushes every subscription event here (once the URL is registered in the
    dashboard). We re-fetch the canonical record by id and upsert it, so All Mandates stays
    current automatically. Re-fetching by id (not trusting the body) means a forged payload
    can inject nothing — at worst it triggers a harmless authenticated lookup."""
    raw = await request.body()
    sig = request.headers.get("x-webhook-signature")
    ts = request.headers.get("x-webhook-timestamp")
    # Signature is verified for observability only — we never trust the payload body
    # (we re-fetch canonical data by id), so a mismatch must NOT drop the event.
    if sig:
        ok = _verify_webhook_signature(raw, ts, sig)
        print(f"[webhook] signature match={ok}")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    sid = _extract_subscription_id(payload)
    print(f"[webhook] received type={payload.get('type')} sid={sid}")
    if sid:
        saved = _fetch_and_save_mandate(sid)
        print(f"[webhook] sid={sid} saved={saved}")
    return JSONResponse(content={"status": "ok"})


MANDATE_AUTH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Authorize Mandate</title>
<script src="https://sdk.cashfree.com/js/v3/cashfree.js"></script>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f7f4ef;color:#0f1923;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
  .box{background:#fff;border:1px solid #e2ddd6;border-radius:16px;box-shadow:0 2px 16px rgba(15,25,35,0.08);padding:32px;max-width:420px;width:100%;text-align:center;}
  h2{font-size:20px;margin:0 0 8px;}
  p{color:#4a5568;font-size:14px;margin:0 0 20px;line-height:1.5;}
  button{background:#2563eb;color:#fff;border:0;border-radius:12px;padding:13px 20px;font-size:15px;font-weight:600;cursor:pointer;width:100%;}
  button:hover{background:#1d4ed8;}
  #msg{margin-top:14px;font-size:13px;color:#991b1b;min-height:18px;}
</style>
</head>
<body>
  <div class="box">
    <h2>Authorize auto-debit mandate</h2>
    <p>You will be taken to Cashfree's secure page to approve the eNACH mandate.</p>
    <button id="go">Proceed to authorize</button>
    <div id="msg"></div>
  </div>
<script>
  var SID = "__SID__";
  var MODE = "__MODE__";
  function start(){
    var msg = document.getElementById('msg');
    if(!SID){ msg.textContent = 'Missing session id in the link.'; return; }
    if(typeof Cashfree === 'undefined'){ msg.textContent = 'Payment SDK failed to load. Check your connection and retry.'; return; }
    try {
      var cashfree = Cashfree({ mode: MODE });
      cashfree.subscriptionsCheckout({ subsSessionId: SID, redirectTarget: "_self" }).then(function(r){
        if(r && r.error){ msg.textContent = (r.error.message || 'Unable to start authorization.'); }
      });
    } catch(e){ msg.textContent = e.message || 'Unable to start authorization.'; }
  }
  document.getElementById('go').addEventListener('click', start);
  window.addEventListener('load', start);
</script>
</body>
</html>"""


@app.get("/mandate/authorize", response_class=HTMLResponse)
async def mandate_authorize(session_id: str = Query(default="")):
    """Public hosted page that launches Cashfree mandate authorization for a session id."""
    sid = re.sub(r"[^A-Za-z0-9_\-]", "", session_id or "")
    page = MANDATE_AUTH_PAGE.replace("__SID__", sid).replace("__MODE__", CASHFREE_PG_MODE)
    return HTMLResponse(content=page)


@app.on_event("startup")
async def startup():
    """Initialize database tables and connection pool on app startup."""
    init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=5000, reload=True)
