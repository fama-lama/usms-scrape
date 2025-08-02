import os
import time
import logging
from datetime import datetime, timedelta, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("usms")

# Read config from environment variables
usms_username = os.getenv("USMS_USERNAME")
usms_password = os.getenv("USMS_PASSWORD")
selenium_host = os.getenv("SELENIUM_HOST", "localhost")
selenium_port = os.getenv("SELENIUM_PORT", "4444")

mqtt_broker = os.getenv("MQTT_BROKER")
mqtt_port_str = os.getenv("MQTT_PORT")
mqtt_username = os.getenv("MQTT_USERNAME")
mqtt_password = os.getenv("MQTT_PASSWORD")

try:
    mqtt_port = int(mqtt_port_str) if mqtt_port_str and mqtt_port_str.isdigit() else 1883
except ValueError:
    mqtt_port = 1883

scrape_interval = int(os.getenv("SCRAPE_INTERVAL", "1800"))

mqtt_enabled = all([mqtt_broker, mqtt_username, mqtt_password]) and mqtt is not None

# MQTT topics
topic_unit = "home/usms/remaining_unit"
topic_balance = "home/usms/remaining_balance"
topic_polled = "home/usms/meter_last_polled"
topic_lastrun = "home/usms/last_run"

# Configure Selenium options
chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

def create_driver():
    for _ in range(5):
        try:
            return webdriver.Remote(
                command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
                options=chrome_options
            )
        except WebDriverException as e:
            log.warning(f"Selenium not ready yet: {e}")
            time.sleep(3)
    raise RuntimeError("Selenium host not reachable after retries.")

def is_logged_in(driver):
    driver.get("https://www.usms.com.bn/SmartMeter/Home")
    try:
        driver.find_element(By.XPATH, "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div")
        log.info("Already logged in.")
        return True
    except Exception:
        log.info("Not logged in.")
        return False

def login(driver):
    log.info("Logging in…")
    driver.get("https://www.usms.com.bn/SmartMeter/ResLogin")
    driver.find_element(By.ID, "ASPxRoundPanel1_txtUsername_I").send_keys(usms_username)
    driver.find_element(By.ID, "ASPxRoundPanel1_txtPassword_I").send_keys(usms_password)
    driver.find_element(By.ID, "ASPxRoundPanel1_btnLogin").click()
    time.sleep(3)

def safe_get_text(driver, xpath):
    try:
        return drive
