#!/usr/bin/env python3
"""
MQTT Test Client for Routing Service

This script simulates a mobile app client that:
1. Requests a route via REST API
2. Sends heartbeats via MQTT
3. Sends waypoint confirmations via MQTT
4. Listens for route update suggestions via MQTT
"""

import paho.mqtt.client as mqtt
import requests
import json
import time
import uuid
from datetime import datetime


# Configuration
API_URL = "http://localhost:8002"
MQTT_BROKER = "localhost"
MQTT_PORT = 1884
TICKET_ID = f"TICKET_{uuid.uuid4().hex[:8].upper()}"

# Route parameters
FROM_NODE = "N1"
TO_NODE = "N10"

# State
current_route = None
session_id = None
mqtt_topic = None
waypoint_index = 0


def on_connect(client, userdata, flags, rc):
    """Callback when connected to MQTT broker"""
    print(f"✓ Connected to MQTT broker (code: {rc})")
    if mqtt_topic:
        client.subscribe(mqtt_topic)
        print(f"✓ Subscribed to {mqtt_topic}")


def on_message(client, userdata, msg):
    """Callback when message received from broker"""
    print(f"\n📨 Received message on {msg.topic}")
    try:
        payload = json.loads(msg.payload.decode())
        print(f"   Type: {payload.get('type')}")
        
        if payload.get('type') == 'route_suggestion':
            print(f"   Reason: {payload.get('reason')}")
            print(f"   Confidence: {payload.get('confidence')}")
            new_route = payload.get('new_route', {})
            print(f"   New distance: {new_route.get('distance')} m")
            print(f"   New duration: {new_route.get('duration')} s")
            print(f"   Improvement: {new_route.get('improvement_percent')}%")
            print("   → Client should validate with actual GPS position")
            
        elif payload.get('type') == 'session_expired':
            print(f"   Reason: {payload.get('reason')}")
            print("   → Session ended, route tracking stopped")
            
    except json.JSONDecodeError as e:
        print(f"   ✗ Failed to parse message: {e}")


def request_route():
    """Request initial route via REST API"""
    global current_route, session_id, mqtt_topic
    
    print(f"\n🚀 Requesting route from {FROM_NODE} to {TO_NODE}")
    print(f"   Ticket ID: {TICKET_ID}")
    
    try:
        response = requests.post(
            f"{API_URL}/api/route",
            json={
                "from_node": FROM_NODE,
                "to_node": TO_NODE,
                "ticket_id": TICKET_ID
            },
            timeout=10
        )
        response.raise_for_status()
        
        current_route = response.json()
        session_id = current_route.get('session_id')
        mqtt_topic = current_route.get('mqtt_topic')
        
        print(f"✓ Route received")
        print(f"   Distance: {current_route.get('distance')} m")
        print(f"   Duration: {current_route.get('duration')} s")
        print(f"   Session ID: {session_id}")
        print(f"   MQTT Topic: {mqtt_topic}")
        print(f"   Waypoints: {current_route.get('waypoints')}")
        
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"✗ Failed to request route: {e}")
        return False


def send_heartbeat(client):
    """Send heartbeat to routing service"""
    topic = f"stadium/clients/{TICKET_ID}/heartbeat"
    payload = {
        "ticket_id": TICKET_ID,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    client.publish(topic, json.dumps(payload))
    print(f"💓 Sent heartbeat")


def send_waypoint(client, node_id):
    """Send waypoint confirmation to routing service"""
    topic = f"stadium/clients/{TICKET_ID}/waypoint"
    payload = {
        "ticket_id": TICKET_ID,
        "node_id": node_id,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    client.publish(topic, json.dumps(payload))
    print(f"📍 Sent waypoint confirmation: {node_id}")


def cancel_route(client):
    """Cancel active route"""
    topic = f"stadium/clients/{TICKET_ID}/route/cancel"
    payload = {
        "ticket_id": TICKET_ID,
        "session_id": session_id,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    client.publish(topic, json.dumps(payload))
    print(f"❌ Sent route cancellation")


def main():
    global waypoint_index
    
    print("=" * 60)
    print("MQTT Routing Test Client")
    print("=" * 60)
    
    # Request initial route
    if not request_route():
        print("Failed to get initial route. Exiting.")
        return
    
    # Setup MQTT client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        
        # Wait for connection
        time.sleep(2)
        
        # Subscribe to route updates
        if mqtt_topic:
            client.subscribe(mqtt_topic)
            print(f"✓ Subscribed to {mqtt_topic}")
        
        waypoints = current_route.get('waypoints', [])
        
        print("\n" + "=" * 60)
        print("Simulation started - sending heartbeats every 10s")
        print("Waypoints will be sent every 30s")
        print("Press Ctrl+C to cancel route and exit")
        print("=" * 60)
        
        # Simulate journey
        iteration = 0
        while True:
            time.sleep(10)
            iteration += 1
            
            # Send heartbeat every 10s (in production: 45s)
            send_heartbeat(client)
            
            # Send waypoint every 30s (in production: when actually reaching it)
            if iteration % 3 == 0 and waypoint_index < len(waypoints):
                send_waypoint(client, waypoints[waypoint_index])
                waypoint_index += 1
                
                if waypoint_index >= len(waypoints):
                    print("\n✓ All waypoints reached - route completed")
                    break
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        cancel_route(client)
        time.sleep(1)
        
    finally:
        client.loop_stop()
        client.disconnect()
        print("\n✓ Disconnected from MQTT broker")


if __name__ == "__main__":
    main()
