import paho.mqtt.client as mqtt
import json
import logging
import os
import ssl
from typing import Optional, Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MQTT_USER = os.getenv("MQTT_USER", "services")
_MQTT_PASS = os.getenv("MQTT_PASS", "dragao_mqtt_2026")
_MQTT_CA_CERT = os.getenv("MQTT_CA_CERT", "")


def _configure_mqtt_tls(client: mqtt.Client) -> None:
    """Apply credentials and optional TLS to a paho Client."""
    client.username_pw_set(_MQTT_USER, _MQTT_PASS)
    if _MQTT_CA_CERT:
        try:
            client.tls_set(ca_certs=_MQTT_CA_CERT, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.tls_insecure_set(False)
        except Exception as exc:  # pragma: no cover
            logger.warning("[MQTT][TLS] Could not configure TLS: %s", exc)


class MQTTRoutingHandler:
    """
    Handles MQTT communication for the Routing Service.
    - Subscribes to service updates (waittime, congestion)
    - Publishes routing updates and receives client heartbeats/waypoints
    """
    
    def __init__(self, client_broker: str, client_port: int):
        # Client broker (for all MQTT communication)
        self.client_broker = client_broker
        self.client_port = client_port
        
        # MQTT client
        self.client_mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="routing_service_client")
        self.client_mqtt.on_connect = self._on_client_connect
        self.client_mqtt.on_message = self._on_client_message
        self.client_mqtt.on_disconnect = self._on_client_disconnect
        _configure_mqtt_tls(self.client_mqtt)

        
        # Callbacks for handling events
        self.on_waittime_update: Optional[Callable] = None
        self.on_congestion_update: Optional[Callable] = None
        self.on_alert: Optional[Callable] = None
        self.on_heartbeat: Optional[Callable] = None
        self.on_waypoint: Optional[Callable] = None
        self.on_route_cancel: Optional[Callable] = None
        self.verify_ticket_callback: Optional[Callable] = None
    
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
            client.subscribe("stadium/services/waittime/#")
            logger.info("[MQTT] Subscribed to topic: stadium/services/waittime/#")
            # Subscribe to congestion from Congestion-Service
            client.subscribe("stadium/services/congestion")
            logger.info("[MQTT] Subscribed to topic: stadium/services/congestion")
            # Subscribe to emergency alerts from Alert-Service
            client.subscribe("alerts/broadcast")
            logger.info("[MQTT] Subscribed to topic: alerts/broadcast")
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
            
            # Check for expiry_time
            if "expiry_time" in payload and payload["expiry_time"]:
                try:
                    expiry_str = payload["expiry_time"]
                    # Handle RFC3339 Z suffix by replacing with +00:00
                    if expiry_str.endswith('Z'):
                        expiry_str = expiry_str[:-1] + '+00:00'
                    expiry = datetime.fromisoformat(expiry_str)
                    # Ensure expiry is timezone-aware UTC
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    else:
                        expiry = expiry.astimezone(timezone.utc)
                    if datetime.now(timezone.utc) > expiry:
                        logger.warning(f"[MQTT] Ignored expired message on {topic}")
                        return
                except (ValueError, TypeError) as e:
                    logger.warning(f"[MQTT] Invalid expiry_time format on {topic}: {e}")
            
            
            if "waittime" in topic:
                # Wait time update from WaitTime-Service
                poi_id = topic.split("/")[-1]
                logger.info(f"[MQTT] Received waittime update for POI {poi_id}: {str(payload)[:50]}...")
                if self.on_waittime_update:
                    self.on_waittime_update(poi_id, payload)
            
            elif "/heartbeat" in topic:
                # Extract ticket_id from topic
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.debug(f"[MQTT] Heartbeat from {ticket_id}")
                
                # Validation: Check if ticket is active
                if self.verify_ticket_callback and not self.verify_ticket_callback(ticket_id):
                    return

                if self.on_heartbeat and ticket_id:
                    self.on_heartbeat(ticket_id, payload)
            
            elif "/waypoint" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[MQTT] Waypoint from {ticket_id}: node={payload.get('node_id')}")
                
                # Validation: Check if ticket is active
                if self.verify_ticket_callback and not self.verify_ticket_callback(ticket_id):
                    return

                if self.on_waypoint and ticket_id:
                    self.on_waypoint(ticket_id, payload)
            
            elif "/cancel" in topic:
                parts = topic.split("/")
                ticket_id = parts[2] if len(parts) > 2 else None
                logger.info(f"[MQTT] Route cancellation from ticket: {ticket_id}")
                
                # Validation: Check if ticket is active
                if self.verify_ticket_callback and not self.verify_ticket_callback(ticket_id):
                    return

                if self.on_route_cancel and ticket_id:
                    self.on_route_cancel(ticket_id, payload)
            
            elif "congestion" in topic:
                # Congestion update from Congestion-Service
                logger.debug(f"[MQTT] Congestion update: {len(payload.get('cells', []))} cells")
                if self.on_congestion_update:
                    self.on_congestion_update(payload)
            
            elif topic == "alerts/broadcast":
                # Emergency alert from Alert-Service
                logger.warning(f"[MQTT] EMERGENCY ALERT: {payload.get('alert_type', 'unknown')} - {payload.get('message', '')}")
                if self.on_alert:
                    self.on_alert(payload)
        
        except json.JSONDecodeError as e:
            logger.exception("[MQTT] JSON decode error")
        except Exception as e:
            logger.exception("[MQTT] Error processing message")
    
    def publish_route_update(self, session_id: str, update_data: dict, priority: str = "HIGH", qos: int = 1):
        """Publish route update to specific session"""
        topic = f"stadium/services/routing/{session_id}"
        if "priority" not in update_data:
            update_data["priority"] = priority
        payload = json.dumps(update_data)
        result = self.client_mqtt.publish(topic, payload, qos=qos)
        
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[MQTT] Published route update to topic: {topic}")
        else:
            logger.error(f"[MQTT] Failed to publish route update to {session_id}")
    
    def start(self):
        """Start MQTT client and connect to broker"""
        try:
            self.client_mqtt.connect(self.client_broker, self.client_port, keepalive=60)
            self.client_mqtt.loop_start()
            logger.info("Event Routing Service MQTT Handler started")
        except Exception as e:
            logger.exception("Failed to start MQTT handler")
            raise
    
    def stop(self):
        """Stop MQTT client"""
        self.client_mqtt.loop_stop()
        self.client_mqtt.disconnect()
        logger.info("Event Routing Service MQTT Handler stopped")
