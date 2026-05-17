import pytest
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import paho.mqtt.client as mqtt
from mqtt_handler import MQTTRoutingHandler

class MockMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

def test_mqtt_handler_lifecycle():
    with patch("paho.mqtt.client.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        
        handler = MQTTRoutingHandler("broker.fake", 1883)
        assert handler.client_broker == "broker.fake"
        assert handler.client_port == 1883
        
        # Test connection callback
        handler._on_client_connect(mock_client, None, None, 0)
        assert mock_client.subscribe.called
        
        # Test connection callback failure
        mock_client.reset_mock()
        handler._on_client_connect(mock_client, None, None, 1)
        assert not mock_client.subscribe.called
        
        # Test disconnect callback
        handler._on_client_disconnect(mock_client, None, 1)
        handler._on_client_disconnect(mock_client, None, 0)
        
        # Test start
        handler.start()
        assert mock_client.connect.called
        assert mock_client.loop_start.called
        
        # Test start failure
        mock_client.connect.side_effect = Exception("failed")
        with pytest.raises(Exception):
            handler.start()
            
        # Test stop
        mock_client.reset_mock()
        handler.stop()
        assert mock_client.loop_stop.called
        assert mock_client.disconnect.called

def test_mqtt_message_handling():
    with patch("paho.mqtt.client.Client"):
        handler = MQTTRoutingHandler("broker.fake", 1883)
        
        # Mock callbacks
        handler.on_waittime_update = MagicMock()
        handler.on_congestion_update = MagicMock()
        handler.on_alert = MagicMock()
        handler.on_heartbeat = MagicMock()
        handler.on_waypoint = MagicMock()
        handler.on_route_cancel = MagicMock()
        
        # 1. Waittime
        msg_wait = MockMessage("stadium/services/waittime/POI-1", {"minutes": 5.0})
        handler._on_client_message(None, None, msg_wait)
        handler.on_waittime_update.assert_called_once_with("POI-1", {"minutes": 5.0})
        
        # 2. Congestion
        msg_cong = MockMessage("stadium/services/congestion", {"cells": []})
        handler._on_client_message(None, None, msg_cong)
        handler.on_congestion_update.assert_called_once_with({"cells": []})
        
        # 3. Alert
        msg_alert = MockMessage("alerts/broadcast", {"alert_type": "FIRE", "message": "Run"})
        handler._on_client_message(None, None, msg_alert)
        handler.on_alert.assert_called_once_with({"alert_type": "FIRE", "message": "Run"})
        
        # 4. Heartbeat (verified ticket)
        handler.verify_ticket_callback = MagicMock(return_value=True)
        msg_hb = MockMessage("stadium/clients/t123/heartbeat", {"timestamp": 1234})
        handler._on_client_message(None, None, msg_hb)
        handler.on_heartbeat.assert_called_once_with("t123", {"timestamp": 1234})
        
        # 5. Heartbeat (unverified ticket)
        handler.verify_ticket_callback = MagicMock(return_value=False)
        handler.on_heartbeat.reset_mock()
        handler._on_client_message(None, None, msg_hb)
        assert not handler.on_heartbeat.called
        
        # 6. Waypoint (verified)
        handler.verify_ticket_callback = MagicMock(return_value=True)
        msg_wp = MockMessage("stadium/clients/t123/waypoint", {"node_id": "N1"})
        handler._on_client_message(None, None, msg_wp)
        handler.on_waypoint.assert_called_once_with("t123", {"node_id": "N1"})
        
        # 7. Route Cancel (verified)
        msg_cancel = MockMessage("stadium/clients/t123/route/cancel", {})
        handler._on_client_message(None, None, msg_cancel)
        handler.on_route_cancel.assert_called_once_with("t123", {})

def test_mqtt_message_expiry():
    with patch("paho.mqtt.client.Client"):
        handler = MQTTRoutingHandler("broker.fake", 1883)
        handler.on_waittime_update = MagicMock()
        
        # Expired message (10s ago)
        exp_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace('+00:00', 'Z')
        msg = MockMessage("stadium/services/waittime/POI-1", {"minutes": 5.0, "expiry_time": exp_time})
        handler._on_client_message(None, None, msg)
        assert not handler.on_waittime_update.called
        
        # Non-expired message (10s in future)
        fut_time = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat().replace('+00:00', 'Z')
        msg_fut = MockMessage("stadium/services/waittime/POI-1", {"minutes": 5.0, "expiry_time": fut_time})
        handler._on_client_message(None, None, msg_fut)
        assert handler.on_waittime_update.called

def test_mqtt_publish_route_update():
    with patch("paho.mqtt.client.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        handler = MQTTRoutingHandler("broker.fake", 1883)
        
        # Publish success
        mock_client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        handler.publish_route_update("sess-1", {"route": []})
        assert mock_client.publish.called
        
        # Publish fail
        mock_client.publish.return_value.rc = mqtt.MQTT_ERR_NO_CONN
        handler.publish_route_update("sess-1", {"route": []})
