# Routing Service with Dynamic Re-routing

## Overview
The Routing Service provides optimal pathfinding between stadium locations with real-time dynamic re-routing capabilities. It combines REST API for initial route requests with MQTT for push-based route updates.

## Features
- **Pathfinding**: Calculate optimal routes using Dijkstra's algorithm with A* heuristic
- **Real-time Updates**: MQTT-based push notifications for route changes
- **Session Management**: Track active routing sessions with position estimation
- **Dynamic Re-routing**: Automatic route recalculation when conditions change
- **Waypoint Tracking**: Smart waypoint identification at junctions and level changes
- **Confidence-based Suggestions**: Adaptive thresholds based on position confidence

## Architecture

### REST API
- **POST /api/route**: Calculate initial route
  - Request: `{start, destination_type, destination_id, ticket_id (optional)}`
  - Response: `{path, distance, duration, session_id, mqtt_topic}`

### MQTT Topics

#### Subscriptions (Client → Service)
- `stadium/clients/{ticket_id}/heartbeat` - Client heartbeat every 45s
- `stadium/clients/{ticket_id}/waypoint` - Waypoint confirmation with node_id
- `stadium/clients/{ticket_id}/route/cancel` - Route cancellation

#### Subscriptions (Services → Routing)
- `stadium/services/waittime/#` - Wait time updates
- `stadium/services/congestion` - Congestion changes
- `stadium/services/alerts/#` - Alert notifications

#### Publications (Service → Client)
- `stadium/routing/{session_id}` - Route update suggestions

## Setup

### 1. Configure Environment
Create `.env` file:
```bash
# MQTT Brokers
CLIENT_BROKER=localhost
CLIENT_PORT=1884
STADIUM_BROKER=localhost
STADIUM_PORT=1883

# Services
MAP_SERVICE_URL=http://localhost:8001
WAITTIME_SERVICE_URL=http://localhost:8003
CONGESTION_SERVICE_URL=http://localhost:8004
```

### 2. Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run Service
```bash
python main.py
```

### 4. Docker Deployment
```bash
docker-compose up -d
```

## Usage

### Request Initial Route
```bash
curl -X POST "http://localhost:8002/api/route" \
  -H "Content-Type: application/json" \
  -d '{
    "start": {"x": 100.0, "y": 200.0, "level": 0},
    "destination_type": "node_id",
    "destination_id": "N10",
    "ticket_id": "TICKET_ABC123"
  }'
```

Response:
```json
{
  "path": [...],
  "distance": 150.5,
  "duration": 107.5,
  "session_id": "route-TICKET_ABC123-1234567890",
  "mqtt_topic": "stadium/services/routing/route-TICKET_ABC123-1234567890"
}
```

### Client Heartbeat (MQTT)
```json
{
  "ticket_id": "TICKET_ABC123",
  "timestamp": "2025-01-10T12:00:00Z"
}
```
Topic: `stadium/clients/TICKET_ABC123/heartbeat`

### Waypoint Confirmation (MQTT)
```json
{
  "ticket_id": "TICKET_ABC123",
  "node_id": "N3",
  "timestamp": "2025-01-10T12:01:00Z"
}
```
Topic: `stadium/clients/TICKET_ABC123/waypoint`

### Route Update (MQTT - from service)
```json
{
  "type": "route_suggestion",
  "session_id": "route-TICKET_ABC123-1234567890",
  "reason": "waittime_decreased",
  "new_route": {
    "path": [...],
    "distance": 120.0,
    "duration": 85.7,
    "improvement_percent": 40.2
  },
  "confidence": 0.85
}
```
Topic: `stadium/services/routing/route-TICKET_ABC123-1234567890`

## Session Management

### Position Estimation
- Dead reckoning using 1.4 m/s walking speed
- Confidence decay: 0.9 (<30s) → 0.7 (<60s) → 0.5 (<90s) → 0.3 (>90s)

### Re-routing Thresholds
- High confidence (>0.7): 25% improvement required
- Medium confidence (>0.4): 40% improvement required
- Low confidence: 50% improvement required

### Session Expiry
- Total timeout: 5 minutes
- Heartbeat timeout: 2 minutes
- Expired sessions cleaned up every 60 seconds

## Testing
```bash
# Basic route query  
curl -X POST "http://localhost:8002/api/route" \
  -H "Content-Type: application/json" \
  -d '{"start": {"x": 100, "y": 200, "level": 0}, "destination_type": "node_id", "destination_id": "N10"}' | jq

# Route with session tracking (using ticket)
curl -X POST "http://localhost:8002/api/route" \
  -H "Content-Type: application/json" \
  -d '{"start": {"x": 100, "y": 200, "level": 0}, "destination_type": "node_id", "destination_id": "N10", "ticket_id": "TICKET_TEST"}' | jq

# Health check
curl "http://localhost:8002/health"
```

## MQTT Testing
```bash
# Subscribe to route updates
mosquitto_sub -h localhost -p 1884 -t "stadium/services/routing/#" -v

# Send heartbeat
mosquitto_pub -h localhost -p 1884 -t "stadium/clients/TICKET_TEST/heartbeat" \
  -m '{"ticket_id":"TICKET_TEST","timestamp":"2025-01-10T12:00:00Z"}'

# Send waypoint confirmation
mosquitto_pub -h localhost -p 1884 -t "stadium/clients/TICKET_TEST/waypoint" \
  -m '{"ticket_id":"TICKET_TEST","node_id":"N3","timestamp":"2025-01-10T12:01:00Z"}'
```

## Network Requirements
- **stadium-network**: Communication with Map, WaitTime, Congestion services
- **client-network**: Access to client-mosquitto broker (port 1884)
- External access to stadium-mosquitto (optional, port 1883)

## Dependencies
- fastapi
- uvicorn
- httpx
- paho-mqtt
- python-dotenv
- pydantic 