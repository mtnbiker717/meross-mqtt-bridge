#!/usr/bin/env python3
"""
Meross Garage Door to MQTT Bridge
Reads config from config.yaml, watches for changes, and bridges Meross cloud API to local MQTT.
"""

import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
import paho.mqtt.client as mqtt
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH_PRIMARY = Path("/app/config/config.yaml")
CONFIG_PATH_FALLBACK = Path("./config/config.yaml")
LOG_DIR_PRIMARY = Path("/app/logs")
LOG_DIR_FALLBACK = Path("./logs")

def _resolve_config_path() -> Path:
    if CONFIG_PATH_PRIMARY.exists():
        return CONFIG_PATH_PRIMARY
    return CONFIG_PATH_FALLBACK

def _resolve_log_dir() -> Path:
    d = LOG_DIR_PRIMARY if CONFIG_PATH_PRIMARY.exists() else LOG_DIR_FALLBACK
    d.mkdir(parents=True, exist_ok=True)
    return d

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _redact_email(email: str) -> str:
    if not email or len(email) < 3:
        return "***"
    return email[:3] + "***"


def _setup_logging(level_name: str = "INFO") -> logging.Logger:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logger = logging.getLogger("MerossBridge")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # rotating file handler
    log_dir = _resolve_log_dir()
    fh = RotatingFileHandler(
        log_dir / "bridge.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    path = _resolve_config_path()
    logger.info("Loading config from %s", path)
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def config_is_ready(cfg: dict) -> bool:
    """Return True if minimum required fields are populated."""
    meross = cfg.get("meross", {})
    mqtt_cfg = cfg.get("mqtt", {})
    return bool(meross.get("email")) and bool(meross.get("password")) and bool(mqtt_cfg.get("host"))

# ---------------------------------------------------------------------------
# Config file watcher
# ---------------------------------------------------------------------------

class ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._debounce = 0

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith("config.yaml"):
            now = time.time()
            if now - self._debounce < 2:
                return
            self._debounce = now
            logger.info("Config file change detected")
            self._callback()

# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class MerossMQTTBridge:
    def __init__(self):
        self.cfg: dict = {}
        self.mqtt_client: mqtt.Client | None = None
        self.meross_client: MerossHttpClient | None = None
        self.meross_manager: MerossManager | None = None
        self.garage_device = None

        self.door_states: dict[int, str] = {}
        self.pending_operations: dict[int, bool] = {}
        self.running = False
        self.mqtt_connected = False

        self.loop: asyncio.AbstractEventLoop | None = None
        self._poll_task: asyncio.Task | None = None
        self._config_reload_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()

        # Watchdog observer
        self._observer: Observer | None = None

    # ---- config watcher ---------------------------------------------------

    def _start_config_watcher(self):
        config_path = _resolve_config_path()
        watch_dir = str(config_path.parent)
        handler = ConfigChangeHandler(self._on_config_change)
        self._observer = Observer()
        self._observer.schedule(handler, watch_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for config changes", watch_dir)

    def _on_config_change(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._config_reload_event.set)

    # ---- MQTT callbacks ---------------------------------------------------

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self.mqtt_connected = True
            self._subscribe_doors()
            # Re-publish last known states on reconnect
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._publish_states(), self.loop)
        else:
            logger.error("MQTT connection failed with code %s", rc)

    def _on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning("Disconnected from MQTT broker (rc=%s)", rc)
        self.mqtt_connected = False

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8").strip().lower()
            logger.info("MQTT message: topic=%s payload=%s", topic, payload)

            if not self.garage_device:
                logger.warning("No garage device available, ignoring command")
                return

            # Determine channel from topic
            channel = None
            for door in self.cfg.get("doors", []):
                if door.get("enabled") and door.get("command_topic") == topic:
                    channel = door["channel"]
                    break

            if channel is None:
                logger.warning("No enabled door matched topic %s", topic)
                return

            if payload == "query":
                asyncio.run_coroutine_threadsafe(self._update_and_publish(), self.loop)
                return

            if payload not in ("open", "close"):
                logger.warning("Invalid command: %s", payload)
                return

            asyncio.run_coroutine_threadsafe(
                self._handle_command(channel, payload), self.loop
            )
        except Exception:
            logger.exception("Error processing MQTT message")

    def _subscribe_doors(self):
        for door in self.cfg.get("doors", []):
            if door.get("enabled"):
                topic = door["command_topic"]
                self.mqtt_client.subscribe(topic)
                logger.info("Subscribed to %s", topic)

    # ---- commands ---------------------------------------------------------

    async def _handle_command(self, channel: int, command: str):
        if self.pending_operations.get(channel):
            logger.warning("Operation already pending on channel %s, skipping", channel)
            return

        self.pending_operations[channel] = True
        try:
            await self.garage_device.async_update()
            is_open = self.garage_device.get_is_open(channel)
            current = "open" if is_open else "closed"

            if command == "open" and is_open:
                logger.info("Door channel %s already open, skipping", channel)
                return
            if command == "close" and not is_open:
                logger.info("Door channel %s already closed, skipping", channel)
                return

            logger.info("Executing %s on channel %s (was %s)", command, channel, current)
            if command == "open":
                await asyncio.wait_for(
                    self.garage_device.async_open(channel=channel), timeout=10
                )
            else:
                await asyncio.wait_for(
                    self.garage_device.async_close(channel=channel), timeout=10
                )

            await asyncio.sleep(2)
            await self._update_and_publish()
        except asyncio.TimeoutError:
            logger.error("Timeout executing %s on channel %s", command, channel)
        except Exception:
            logger.exception("Error executing command on channel %s", channel)
        finally:
            self.pending_operations[channel] = False

    # ---- state ------------------------------------------------------------

    async def _update_and_publish(self):
        try:
            await self.garage_device.async_update()
            for door in self.cfg.get("doors", []):
                if not door.get("enabled"):
                    continue
                ch = door["channel"]
                try:
                    is_open = self.garage_device.get_is_open(ch)
                    self.door_states[ch] = "open" if is_open else "closed"
                except Exception:
                    logger.exception("Error reading state for channel %s", ch)
                    self.door_states[ch] = "unknown"

            await self._publish_states()
            self._write_door_states_file()
        except Exception:
            logger.exception("Error updating device state")

    async def _publish_states(self):
        if not self.mqtt_connected or not self.mqtt_client:
            logger.warning("Cannot publish states: MQTT not connected")
            return

        for door in self.cfg.get("doors", []):
            if not door.get("enabled"):
                continue
            ch = door["channel"]
            state = self.door_states.get(ch, "unknown")
            self.mqtt_client.publish(door["state_topic"], state, retain=True)
            logger.info("Published %s = %s", door["state_topic"], state)

        # Combined state
        combined: dict = {}
        for door in self.cfg.get("doors", []):
            if door.get("enabled"):
                name = door.get("name") or f"door_{door['channel']}"
                combined[name] = self.door_states.get(door["channel"], "unknown")
        combined["timestamp"] = datetime.now().isoformat()

        combined_topic = self.cfg.get("mqtt", {}).get("combined_state_topic", "garage/state")
        self.mqtt_client.publish(combined_topic, json.dumps(combined), retain=True)
        logger.info("Published combined state to %s", combined_topic)

    def _write_door_states_file(self):
        try:
            log_dir = _resolve_log_dir()
            states_file = log_dir / "door_states.json"
            data: dict = {}
            for door in self.cfg.get("doors", []):
                if door.get("enabled"):
                    name = door.get("name") or f"door_{door['channel']}"
                    data[name] = {
                        "channel": door["channel"],
                        "state": self.door_states.get(door["channel"], "unknown"),
                        "state_topic": door.get("state_topic", ""),
                        "command_topic": door.get("command_topic", ""),
                    }
            data["timestamp"] = datetime.now().isoformat()
            states_file.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.exception("Error writing door_states.json")

    # ---- Meross connection ------------------------------------------------

    async def _connect_meross(self):
        meross_cfg = self.cfg.get("meross", {})
        email = meross_cfg["email"]
        password = meross_cfg["password"]
        api_url = meross_cfg.get("api_url", "https://iot.meross.com")

        logger.info("Connecting to Meross cloud as %s", _redact_email(email))
        self.meross_client = await MerossHttpClient.async_from_user_password(
            email=email, password=password, api_base_url=api_url
        )
        logger.info("Meross cloud login successful")

        self.meross_manager = MerossManager(http_client=self.meross_client)
        await self.meross_manager.async_init()

        logger.info("Discovering Meross devices...")
        await self.meross_manager.async_device_discovery()

        devices = self.meross_manager.find_devices(device_type="msg200")
        if not devices:
            raise RuntimeError("No Meross MSG200 garage door opener found")

        self.garage_device = devices[0]
        logger.info("Found garage device: %s (UUID: %s)", self.garage_device.name, self.garage_device.uuid)

    async def _teardown_meross(self):
        if self.meross_manager:
            try:
                self.meross_manager.close()
            except Exception:
                pass
            self.meross_manager = None
        if self.meross_client:
            try:
                await self.meross_client.async_logout()
            except Exception:
                pass
            self.meross_client = None
        self.garage_device = None

    # ---- MQTT connection --------------------------------------------------

    def _connect_mqtt(self):
        mqtt_cfg = self.cfg.get("mqtt", {})
        host = mqtt_cfg["host"]
        port = mqtt_cfg.get("port", 1883)
        user = mqtt_cfg.get("user", "")
        password = mqtt_cfg.get("pass", "")

        client_id = f"meross_bridge_{int(time.time())}_{random.randint(1000, 9999)}"
        self.mqtt_client = mqtt.Client(client_id)
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        if user:
            self.mqtt_client.username_pw_set(user, password)

        logger.info("Connecting to MQTT broker at %s:%s", host, port)
        self.mqtt_client.connect_async(host, port, 60)
        self.mqtt_client.loop_start()

    def _teardown_mqtt(self):
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                pass
            self.mqtt_client = None
        self.mqtt_connected = False

    # ---- poll loop --------------------------------------------------------

    async def _poll_loop(self):
        interval = self.cfg.get("bridge", {}).get("poll_interval", 300)
        while self.running:
            await asyncio.sleep(interval)
            if self.garage_device and self.running:
                try:
                    await self._update_and_publish()
                except Exception:
                    logger.exception("Error during poll")

    # ---- main run loop with reconnect ------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.running = True

        # Signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self._signal_handler)

        # Start config watcher
        self._start_config_watcher()

        while self.running:
            try:
                self.cfg = load_config()
            except Exception:
                logger.exception("Failed to load config, retrying in 10s")
                await asyncio.sleep(10)
                continue

            # Update log level from config
            level_name = self.cfg.get("bridge", {}).get("log_level", "INFO")
            level = getattr(logging, level_name.upper(), logging.INFO)
            logger.setLevel(level)
            for h in logger.handlers:
                h.setLevel(level)

            if not config_is_ready(self.cfg):
                logger.warning("Config incomplete (email/password/host empty). Waiting for config update...")
                self._config_reload_event.clear()
                # Wait for config change or shutdown
                await self._wait_for_event(self._config_reload_event, self._shutdown_event)
                if self._shutdown_event.is_set():
                    break
                continue

            # Connect
            try:
                self._connect_mqtt()
                await asyncio.sleep(2)  # let MQTT connect
                await self._connect_meross()
                await self._update_and_publish()
            except Exception:
                logger.exception("Error during startup connection")
                self._teardown_mqtt()
                await self._teardown_meross()
                delay = self._backoff_delay()
                logger.info("Retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue

            # Start polling
            self._poll_task = asyncio.create_task(self._poll_loop())

            # Wait for config reload or shutdown
            self._config_reload_event.clear()
            await self._wait_for_event(self._config_reload_event, self._shutdown_event)

            # Teardown before reconnect / exit
            logger.info("Tearing down connections (config change or shutdown)")
            if self._poll_task:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass

            self._teardown_mqtt()
            await self._teardown_meross()

            if self._shutdown_event.is_set():
                break

        # Final cleanup
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("Bridge stopped")

    # ---- helpers ----------------------------------------------------------

    _backoff_attempts = 0

    def _backoff_delay(self) -> float:
        base = self.cfg.get("bridge", {}).get("reconnect_base_delay", 30)
        cap = self.cfg.get("bridge", {}).get("reconnect_max_delay", 300)
        self._backoff_attempts += 1
        delay = min(base * (2 ** (self._backoff_attempts - 1)), cap)
        jitter = delay * 0.1 * random.random()
        return delay + jitter

    def _reset_backoff(self):
        self._backoff_attempts = 0

    async def _wait_for_event(self, *events: asyncio.Event):
        """Wait until any of the given events is set."""
        tasks = [asyncio.create_task(e.wait()) for e in events]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    def _signal_handler(self):
        logger.info("Received shutdown signal")
        self.running = False
        self._shutdown_event.set()

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    bridge = MerossMQTTBridge()
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")

if __name__ == "__main__":
    main()
