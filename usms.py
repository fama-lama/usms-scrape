import os
import time
import json
import logging
from datetime import datetime
import paho.mqtt.client as mqtt
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.remote_connection import RemoteConnection

# ENV
USERNAME = os.getenv("USMS_USERNAME")
PASSWORD = os.getenv("USMS_PASSWORD")
SELENIUM_HOST = os.getenv("SELENIUM_HOST", "localhost")
SELENIUM_PORT = os.getenv("SELENIUM_PORT", "4444")
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "600"))

MQTT_HOST = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "usms/data")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def get_data():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")

    remote_url = f"http://{SELENIUM_HOST}:{SELENIUM_PORT}/wd/hub"
    try:
        driver = webdriver.Remote(command_executor=remote_url, options=options)
        logging.info("Connected to Selenium")
        driver.get("https://www.usms.com.my/")
        # Stubbed logic
        logging.info("Stub scrape successful.")
        return {"timestamp": datetime.now().isoformat(), "value": "example"}
    except Exception as e:
        logging.error(f"Scraping error: {e}")
        return None
    finally:
        try:
            driver.quit()
        except:
            pass


# MQTT Callbacks
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logging.info("MQTT connected")
    else:
        logging.error(f"MQTT connect failed: {reason_code}")

def on_disconnect(client, userdata, reason_code, properties=None):
    logging.warning(f"MQTT disconnected: {reason_code}")

def on_publish(client, userdata, mid):
    logging.info(f"Published payload id={mid}")

# Setup MQTT client
client = mqtt.Client(mqtt.CallbackAPIVersion.VERS
