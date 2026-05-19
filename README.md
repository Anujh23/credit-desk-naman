# CreditDesk — Loan Analysis & Credit Risk Platform

A comprehensive credit assessment tool that combines loan calculations, Account Aggregator (AA) bank statement fetching, credit scoring, and customer insight analysis.

## Project Structure
```
credit_desk/
├── app.py               ← FastAPI backend (all routes)
├── database.py           ← PostgreSQL database operations
├── scoring.py            ← Credit scoring engine
├── requirements.txt      ← Python dependencies
├── .env                  ← Environment variables
├── test.py               ← AA API test script
└── static/
    └── index.html        ← Frontend SPA
```

## Setup & Run

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
Create a `.env` file with:
```
AA_agf_username=<your_finsense_username>
AA_agf_password=<your_finsense_password>
DATABASE_URL=<your_postgresql_url>
```

### 3. Start the server
```bash
uvicorn app:app --host 0.0.0.0 --port 5000 --reload
```

### 4. Open in browser
```
http://localhost:5000
```

### 5. API Documentation (auto-generated)
```
http://localhost:5000/docs
```

## Key Features

- **Loan Calculator** — Compute interest, GST, processing fees, and total repayment
- **Account Aggregator Integration** — Fetch encrypted bank statements via Finsense AA API (consent flow, FI request, data parsing)
- **Credit Scoring** — Weighted scoring model with income-bracket-based thresholds (CIBIL, EMI count, bounce count, residence type, etc.)
- **Customer Insights** — Search loan history across multiple products by PAN, name, or mobile; calculate behavior scores from disbursement & collection data
- **Bank Statement Storage** — Save, search, and view parsed bank statements

## API Endpoints

### Loan Calculator
| Route | Method | Description |
|-------|--------|-------------|
| `/calculate` | POST | Calculate loan summary (interest, fees, repayment) |

### Account Aggregator (Finsense)
| Route | Method | Description |
|-------|--------|-------------|
| `/api/login` | POST | Get auth token |
| `/api/consent-request` | POST | Create one-time consent |
| `/api/consent-request-plus` | POST | Create periodic consent |
| `/api/consent-status` | POST | Check consent approval |
| `/api/consent-details` | POST | Get consent metadata |
| `/api/fi-request` | POST | Request financial information |
| `/api/fi-status` | POST | Check FI fetch progress |
| `/api/fi-data-fetch` | POST | Download bank data |
| `/api/parse-fi-data` | POST | Parse XML statements to JSON |

### Credit Analysis
| Route | Method | Description |
|-------|--------|-------------|
| `/api/analyze-credit` | POST | Score customer and return approval/sanction decision |

### Customer Insights
| Route | Method | Description |
|-------|--------|-------------|
| `/api/ci-databases` | GET | List all product databases |
| `/api/ci-search` | POST | Search customer across products |
| `/api/behavior-score` | POST | Calculate behavior score from loan history |
| `/api/ci-upload` | POST | Upload disbursed & collection CSV files |
| `/api/ci-export` | POST | Export search results to CSV |

### Bank Statement Storage
| Route | Method | Description |
|-------|--------|-------------|
| `/api/save-statement` | POST | Save a fetched bank statement |
| `/api/search-statements` | POST | Search saved statements |
| `/api/get-statement/<id>` | GET | Retrieve full statement |
| `/api/delete-statement/<id>` | DELETE | Delete a statement |

## Tech Stack
- **Backend:** FastAPI + Uvicorn
- **Database:** PostgreSQL (hosted on Render)
- **Frontend:** HTML/CSS/JS (single-page app)
- **External API:** Finsense Account Aggregator

## Author
Created by **Anuj**
