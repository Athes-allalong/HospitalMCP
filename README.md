# Hospital MCP – Project README

## Project Structure

```
Hospital_mcp/
├── server.py          # MCP server (FastMCP + MongoDB)
├── scraper.py         # Scrapes dummyhospital.dev.aiwillbuild.com → seeds MongoDB
├── requirements.txt   # Python dependencies
├── ui/
│   ├── app.py         # Flask web UI
│   ├── templates/     # Jinja2 HTML templates
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── departments.html
│   │   ├── doctors.html
│   │   ├── doctor_detail.html
│   │   ├── patients.html
│   │   ├── patient_detail.html
│   │   └── appointments.html
│   └── static/
│       └── css/style.css
└── seed.py            # (legacy) manual seed – replaced by scraper.py
```

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Start MongoDB
Make sure MongoDB is running on `localhost:27017` (default), or set:
```bash
set MONGO_URI=mongodb://localhost:27017
```

### 3. Scrape data from the dummy website
```bash
python scraper.py
```
This fetches data from **http://dummyhospital.dev.aiwillbuild.com** and populates MongoDB.  
Dev tokens (valid 30 days) are printed to the console.

### 4. Start the Web UI
```bash
python ui/app.py
```
Open **http://localhost:5000** in your browser.

### 5. Start the MCP Server (separate terminal)
```bash
# stdio mode (default, for Claude Desktop / Cursor etc.)
set ALLOW_DEV_IDENTITY=1
set DEV_ROLE=admin
set DEV_SUBJECT=A001
python server.py

# HTTP mode
set MCP_TRANSPORT=streamable-http
python server.py
```

---

## Environment Variables

| Variable           | Default                     | Description                        |
|--------------------|-----------------------------|------------------------------------|
| `MONGO_URI`        | `mongodb://localhost:27017` | MongoDB connection string          |
| `MONGO_DB`         | `hospital`                  | Database name                      |
| `HOSPITAL_TZ`      | `Asia/Kolkata`              | Hospital local timezone            |
| `ALLOW_DEV_IDENTITY` | (unset)                   | Set to `1` to enable dev tokens    |
| `DEV_ROLE`         | (unset)                     | Role for stdio dev mode            |
| `DEV_SUBJECT`      | (unset)                     | Staff ID / MRN for stdio dev mode  |
| `UI_PORT`          | `5000`                      | Flask UI port                      |

---

## Scraper Behavior

`scraper.py` tries the following in order for each entity type:

1. **JSON API** – tries `/api/departments`, `/departments.json`, etc.
2. **HTML scraping** – tries common CSS selectors for tables and cards
3. **Fallback defaults** – rich sample data with 5 departments, 5 doctors, 5 patients

If the site has an API (e.g. `/api/doctors`), all real data is used. Otherwise the scraper
falls back gracefully so the system is always usable.
