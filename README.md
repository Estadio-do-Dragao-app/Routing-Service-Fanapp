# Routing-System

### 1. Start PostgreSQL in Map Service
```bash
docker-compose up -d
```

### 2. Create .env file
```bash
cp .env.example .env
# Could be necessary to change a port 
```

### 3. Setup Python Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Load Data From Map Service
```bash
python3 load_data_db.py
```

### 5. Run Map Service
```bash
uvicorn ApiHandler:app --reload
```
### 6. Run Routing service
```python main.py```

### 7. Testing
curl "http://localhost:8002/api/route?from_node=N1&to_node=N10" | jq 