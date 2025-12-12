import paho.mqtt.client as mqtt
import json
import logging
import os
from typing import Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class MQTTRoutingHandler:
    """
    Handles MQTT communication for the Routing Service.
    - Subscribes to service updates (waittime, congestion, alerts)
    - Publishes routing updates and receives client heartbeats/waypoints
    """
    
    def __init__(
        self,
        client_broker: str,
        client_port: int,
        stadium_broker: Optional[str] = None,
        stadium_port: int = 1883
    ):
        # Client broker (for publishing route updates and receiving heartbeats)
        self.client_broker = client_broker
        self.client_port = client_port
        
        # Stadium broker (for receiving service updates - optional)
        self.stadium_broker = stadium_broker
        self.stadium_port = stadium_port
        
        # MQTT clients
        self.client_mqtt = mqtt.Client(client_id="routing_service_client")
        self.client_mqtt.on_connect = self._on_client_connect
        self.client_mqtt.on_message = self._on_client_message
        self.client_mqtt.on_disconnect = self._on_client_disconnect
        
        if stadium_broker:
            self.stadium_mqtt = mqtt.Client(client_id="routing_service_stadium")
            self.stadium_mqtt.on_connect = self._on_stadium_connect
            self.stadium_mqtt.on_message = self._on_stadium_message
            self.stadium_mqtt.on_disconnect = self._on_stadium_disconnect
        else:
            self.stadium_mqtt = None
        
        # Callbacks for handling events
        self.on_waittime_update: Optional[Callable] = None
        self.on_congestion_update: Optional[Callable] = None
        self.on_alert: Optional[Callable] = None
        self.on_heartbeat: Optional[Callable] = None
        self.on_waypoint: Optional[Callable] = None
        self.on_route_cancel: Optional[Callable] = None
    
    def _on_client_connect(self, client, userdata, flags, rc):
        """Handler for connection to client broker"""
        if rc == 0:
            logger.info(f"[CLIENT] Connected to broker at {self.client_broker}:{self.client_port}")
            # Subscribe to client messages (heartbeats, waypoints, cancellations)
            client.subscribe("stadium/clients/+/heartbeat")
            logger.info("[CLIENT] Subscribed to topic: stadium/clients/+/heartbeat")
            client.subscribe("stadium/clients/+/waypoint")
            logger.info("[CLIENT] Subscribed to topic: stadium/clients/+/waypoint")
            client.subscribe("stadium/clients/+/route/cancel")
            logger.info("[CLIENT] Subscribed to topic: stadium/clients/+/route/cancel")
        else:
            logger.error(f"[CLIENT] Connection failed with code {rc}")
    
    def _on_stadium_connect(self, client, userdata, flags, rc):
        """Handler for connection to stadium broker (service updates)"""
        if rc == 0:
            logger.info(f"[STADIUM] Connected to broker at {self.stadium_broker}:{self.stadium_port}")
            # Subscribe to service updates
            client.subscribe("stadium/services/waittime/#")
            logger.info("[STADIUM] Subscribed to topic: stadium/services/waittime/#")
            client.subscribe("stadium/services/congestion")
            logger.info("[STADIUM] Subscribed to topic: stadium/services/congestion")
            client.subscribe("stadium/services/alerts/#")
            logger.info("[STADIUM] Subscribed to topic: stadium/services/alerts/#")
        else:
            logger.error(f"[STADIUM] Connection failed with code {rc}")
    
    def _on_client_disconnect(self, client, userdata, rc):
        """Handler for disconnection from client broker"""
        if rc != 0:
            logger.warning(f"[CLIENT] Unexpected disconnection. Code: {rc}")
    
    def _on_stadium_disconnect(self, client, userdata, rc):
        """Handler for disconnection from stadium broker"""
        if rc != 0:
            logger.warning(f"[STADIUM] Unexpected disconnection. Code: {rc}")
    
    def _on_client_message(self, client, userdata, msg):
        """Process incoming messages from clients"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            if "/heartbeat" in topic:
                # Extract ticket_id from topic
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[CLIENT] Received heartbeat from ticket: {ticket_id}")
                if self.on_heartbeat and ticket_id:
                    self.on_heartbeat(ticket_id, payload)
            
            elif "/waypoint" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[CLIENT] Received waypoint from ticket: {ticket_id}")
                if self.on_waypoint and ticket_id:
                    self.on_waypoint(ticket_id, payload)
            
            elif "/cancel" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[CLIENT] Received route cancellation from ticket: {ticket_id}")
                if self.on_route_cancel and ticket_id:
                    self.on_route_cancel(ticket_id, payload)
        
        except json.JSONDecodeError as e:
            logger.error(f"[CLIENT] JSON decode error: {e}")
        except Exception as e:
            logger.error(f"[CLIENT] Error processing message: {e}")
    
    def _on_stadium_message(self, client, userdata, msg):
        """Process incoming messages from services"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            if "waittime" in topic:
                poi_id = topic.split("/")[-1]
                logger.info(f"[STADIUM] Received waittime update for POI: {poi_id}")
                if self.on_waittime_update:
                    self.on_waittime_update(poi_id, payload)
            
            elif "congestion" in topic:
                logger.info(f"[STADIUM] Received congestion update")
                if self.on_congestion_update:
                    self.on_congestion_update(payload)
            
            elif "alerts" in topic:
                logger.info(f"[STADIUM] Received alert")
                if self.on_alert:
                    self.on_alert(payload)
        
        except json.JSONDecodeError as e:
            logger.error(f"[STADIUM] JSON decode error: {e}")
        except Exception as e:
            logger.error(f"[STADIUM] Error processing message: {e}")
    
    def publish_route_update(self, session_id: str, update_data: dict):
        """Publish route update to specific session"""
        topic = f"stadium/services/routing/{session_id}"
        payload = json.dumps(update_data)
        result = self.client_mqtt.publish(topic, payload, qos=1)
        
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[CLIENT] Published route update to topic: {topic}")
        else:
            logger.error(f"[CLIENT] Failed to publish route update to {session_id}")
    
    def start(self):
        """Start MQTT clients and connect to brokers"""
        try:
            # Connect to client broker
            self.client_mqtt.connect(self.client_broker, self.client_port, keepalive=60)
            self.client_mqtt.loop_start()
            
            # Connect to stadium broker if configured
            if self.stadium_mqtt:
                self.stadium_mqtt.connect(self.stadium_broker, self.stadium_port, keepalive=60)
                self.stadium_mqtt.loop_start()
            
            logger.info("Routing Service MQTT Handler started")
        except Exception as e:
            logger.error(f"Failed to start MQTT handlers: {e}")
            raise
    
    def stop(self):
        """Stop MQTT clients"""
        self.client_mqtt.loop_stop()
        self.client_mqtt.disconnect()
        
        if self.stadium_mqtt:
            self.stadium_mqtt.loop_stop()
            self.stadium_mqtt.disconnect()
        
        logger.info("Routing Service MQTT Handler stopped")
