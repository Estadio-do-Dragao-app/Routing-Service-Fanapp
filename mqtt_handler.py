import paho.mqtt.client as mqtt
import json
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class MQTTRoutingHandler:
    """
    Handles MQTT communication for the Routing Service.
    - Subscribes to service updates (waittime, congestion) on client broker
    - Publishes routing updates and receives client heartbeats/waypoints
    """
    
    def __init__(self, client_broker: str, client_port: int):
        # Client broker (for all MQTT communication)
        self.client_broker = client_broker
        self.client_port = client_port
        
        # MQTT client
        self.client_mqtt = mqtt.Client(client_id="routing_service_client")
        self.client_mqtt.on_connect = self._on_client_connect
        self.client_mqtt.on_message = self._on_client_message
        self.client_mqtt.on_disconnect = self._on_client_disconnect
        
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
            logger.info(f"[MQTT] Connected to broker at {self.client_broker}:{self.client_port}")
            # Subscribe to client messages (heartbeats, waypoints, cancellations)
            client.subscribe("stadium/clients/+/heartbeat")
            logger.info("[MQTT] Subscribed to topic: stadium/clients/+/heartbeat")
            client.subscribe("stadium/clients/+/waypoint")
            logger.info("[MQTT] Subscribed to topic: stadium/clients/+/waypoint")
            client.subscribe("stadium/clients/+/route/cancel")
            logger.info("[MQTT] Subscribed to topic: stadium/clients/+/route/cancel")
            # Subscribe to waittime from WaitTime-Service
            client.subscribe("stadium/waittime/#")
            logger.info("[MQTT] Subscribed to topic: stadium/waittime/#")
            # Subscribe to congestion from Congestion-Service
            client.subscribe("stadium/services/congestion")
            logger.info("[MQTT] Subscribed to topic: stadium/services/congestion")
        else:
            logger.error(f"[MQTT] Connection failed with code {rc}")
    
    def _on_client_disconnect(self, client, userdata, rc):
        """Handler for disconnection from client broker"""
        if rc != 0:
            logger.warning(f"[MQTT] Unexpected disconnection. Code: {rc}")
    
    def _on_client_message(self, client, userdata, msg):
        """Process incoming messages from clients and services"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())
            
            if "waittime" in topic:
                # Wait time update from WaitTime-Service
                poi_id = topic.split("/")[-1]
                logger.info(f"[MQTT] Received waittime update for POI: {poi_id}")
                if self.on_waittime_update:
                    self.on_waittime_update(poi_id, payload)
            
            elif "/heartbeat" in topic:
                # Extract ticket_id from topic
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[MQTT] Received heartbeat from ticket: {ticket_id}")
                if self.on_heartbeat and ticket_id:
                    self.on_heartbeat(ticket_id, payload)
            
            elif "/waypoint" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[MQTT] Received waypoint from ticket: {ticket_id}")
                if self.on_waypoint and ticket_id:
                    self.on_waypoint(ticket_id, payload)
            
            elif "/cancel" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[MQTT] Received route cancellation from ticket: {ticket_id}")
                if self.on_route_cancel and ticket_id:
                    self.on_route_cancel(ticket_id, payload)
            
            elif "congestion" in topic:
                # Congestion update from Congestion-Service
                logger.debug(f"[MQTT] Received congestion update")
                if self.on_congestion_update:
                    self.on_congestion_update(payload)
        
        except json.JSONDecodeError as e:
            logger.error(f"[MQTT] JSON decode error: {e}")
        except Exception as e:
            logger.error(f"[MQTT] Error processing message: {e}")
    
    def publish_route_update(self, session_id: str, update_data: dict):
        """Publish route update to specific session"""
        topic = f"stadium/services/routing/{session_id}"
        payload = json.dumps(update_data)
        result = self.client_mqtt.publish(topic, payload, qos=1)
        
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[MQTT] Published route update to topic: {topic}")
        else:
            logger.error(f"[MQTT] Failed to publish route update to {session_id}")
    
    def start(self):
        """Start MQTT client and connect to broker"""
        try:
            self.client_mqtt.connect(self.client_broker, self.client_port, keepalive=60)
            self.client_mqtt.loop_start()
            logger.info("Routing Service MQTT Handler started")
        except Exception as e:
            logger.error(f"Failed to start MQTT handler: {e}")
            raise
    
    def stop(self):
        """Stop MQTT client"""
        self.client_mqtt.loop_stop()
        self.client_mqtt.disconnect()
        logger.info("Routing Service MQTT Handler stopped")
