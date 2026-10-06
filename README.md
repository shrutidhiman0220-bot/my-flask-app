# Lost and Found Portal | Team S3
Department of Computer Application and Business Management

## Run
    pip install -r requirements.txt
    python app.py     (open http://127.0.0.1:5000)
Admin login: admin@s3.edu / admin123 (change after first run).
Optional email alerts: set SMTP_HOST, SMTP_PORT, SMTP_FROM environment variables.

## Objectives
Base: registration/login, search + filter (keyword, type, category, date range), admin moderation.
Advanced: image matching (average-hash + colour histogram with Pillow), map pinning (Leaflet + OpenStreetMap),
QR claim pass (verified by admin at /verify/<token>), trust score, automatic match alerts (in-app + optional email),
analytics dashboard (Chart.js), Hindi + English.
