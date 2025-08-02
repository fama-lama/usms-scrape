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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("usms")

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

topic_unit = "home/usms/remaining_unit"
topic_balance = "home/usms/remaining_balance"
topic_polled = "home/usms/meter_last_polled"
topic_lastrun = "home/usms/last_run"

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
        return driver.find_element(By.XPATH, xpath).text.strip()
    except Exception:
        return None

def scrape(driver):
    if not is_logged_in(driver):
        login(driver)
    unit = safe_get_text(driver, "/html/body/form/div[5]/table/tbody/tr/td/table[3]/tbody/tr[1]/td[2]/span")
    balance = safe_get_text(driver, "/html/body/form/div[5]/table/tbody/tr/td/table[3]/tbody/tr[2]/td[2]/span")
    last_polled = safe_get_text(driver, "/html/body/form/div[5]/table/tbody/tr/td/table[4]/tbody/tr[1]/td[2]/span")
    return unit, balance, last_polled

def mqtt_publish(client, unit, balance, last_polled):
    client.publish(topic_unit, unit)
    client.publish(topic_balance, balance)
    client.publish(topic_polled, last_polled)
    client.publish(topic_lastrun, datetime.now(timezone(timedelta(hours=8))).isoformat())

def main():
    if mqtt_enabled:
        client = mqtt.Client()
        client.username_pw_set(mqtt_username, mqtt_password)
        client.connect(mqtt_broker, mqtt_port)
        client.loop_start()
    else:
        client = None

    driver = create_driver()

    while True:
        try:
            # Check for dead session
            if not driver.session_id:
                raise Exception("Driver session missing, recreating")

            unit, balance, last_polled = scrape(driver)
            log.info(f"Unit: {unit}, Balance: {balance}, Last Polled: {last_polled}")
            if client:
                mqtt_publish(client, unit, balance, last_polled)
        except Exception as e:
            # Handle Selenium session expiry by recreating the driver
            if "NoSuchSessionException" in str(e) or "Unable to find session" in str(e) or "session missing" in str(e):
                log.warning("Selenium session expired. Recreating driver...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver()
            else:
                log.error(f"Error during scraping: {e}")

        log.info(f"Sleeping {scrape_interval}s…")
        time.sleep(scrape_interval)

if __name__ == "__main__":
    main()
