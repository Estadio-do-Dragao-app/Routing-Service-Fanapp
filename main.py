import uvicorn
from api_handler import app

if __name__ == "__main__":
    print("Starting Routing Service on port 8002...")
    uvicorn.run(app, host="0.0.0.0", port=8002, reload=True)
