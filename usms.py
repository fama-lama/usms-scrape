import os
import time
import logging
from datetime import datetime, timedelta, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

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

# Dashboard page and XPaths for the data table rows
DASHBOARD_URL = "https://www.usms.com.bn/SmartMeter/Home"
LOGIN_URL = "https://www.usms.com.bn/SmartMeter/ResLogin"

# XPaths for the three data cells in the dashboard table
XPATH_UNIT = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[9]/td/table/tbody/tr/td[2]"
XPATH_BALANCE = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[10]/td/table/tbody/tr/td[2]"
XPATH_POLLED = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[11]/td/table/tbody/tr/td[2]"
# The outer container div (used for login detection and as anchor for waiting)
XPATH_DASHBOARD_DIV = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div"

# Configure Selenium options — fresh options each session to avoid shared state
def make_chrome_options():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return opts


def create_driver():
    """Create a new remote WebDriver session, retrying up to 5 times."""
    last_exc = None
    for attempt in range(5):
        try:
            return webdriver.Remote(
                command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
                options=make_chrome_options()
            )
        except WebDriverException as e:
            last_exc = e
            log.warning(f"Selenium not ready yet (attempt {attempt + 1}/5): {e}")
            time.sleep(3)
    raise RuntimeError(f"Selenium host not reachable after retries: {last_exc}")


def _log_page_source(driver, label=""):
    """Log what USMS actually returned — invaluable for debugging."""
    try:
        title = driver.title
        url = driver.current_url
        body_snippet = driver.find_element(By.TAG_NAME, "body").text[:500]
        log.warning("%s — Page title: %s | URL: %s | Body preview: %s",
                     label or "Page state", title, url, body_snippet)
    except Exception as e:
        log.warning("Could not capture page source for %s: %s", label, e)


def is_logged_in(driver):
    driver.get(DASHBOARD_URL)
    try:
        driver.find_element(By.XPATH, XPATH_DASHBOARD_DIV)
        log.info("Already logged in.")
        return True
    except Exception:
        log.info("Not logged in.")
        return False


def login(driver):
    log.info("Logging in…")
    driver.get(LOGIN_URL)
    driver.find_element(By.ID, "ASPxRoundPanel1_txtUsername_I").send_keys(usms_username)
    driver.find_element(By.ID, "ASPxRoundPanel1_txtPassword_I").send_keys(usms_password)
    driver.find_element(By.ID, "ASPxRoundPanel1_btnLogin").click()

    # Wait up to 20s for the dashboard container to appear after login
    # This replaces the blind time.sleep(3) — we wait for the actual page we need.
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, XPATH_DASHBOARD_DIV))
        )
        log.info("Login successful — dashboard loaded.")
    except TimeoutException:
        log.warning("Dashboard did not appear after login — dumping page state.")
        _log_page_source(driver, "After login timeout")


def safe_get_text(driver, xpath, timeout=10):
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        return el.text.strip()
    except TimeoutException:
        log.warning(f"Element not found within {timeout}s: {xpath}")
        return "N/A"
    except Exception as e:
        log.warning(f"Failed to find element {xpath}: {e}")
        return "N/A"


def scrape_data(driver):
    log.info("Scraping dashboard…")
    driver.get(DASHBOARD_URL)

    unit = safe_get_text(driver, XPATH_UNIT)
    balance = safe_get_text(driver, XPATH_BALANCE)
    polled = safe_get_text(driver, XPATH_POLLED)
    run_time = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

    log.info(f"Unit: {unit}, Balance: {balance}, Last Polled: {polled}, Time: {run_time}")

    # If all three values are N/A, the page structure is likely wrong — log a debug dump
    if unit == "N/A" and balance == "N/A" and polled == "N/A":
        _log_page_source(driver, "All values N/A")
        # Retry once: refresh and wait a bit longer
        log.info("Retrying scrape after refresh…")
        time.sleep(5)
        driver.get(DASHBOARD_URL)
        unit = safe_get_text(driver, XPATH_UNIT, timeout=15)
        balance = safe_get_text(driver, XPATH_BALANCE, timeout=15)
        polled = safe_get_text(driver, XPATH_POLLED, timeout=15)
        run_time = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"Retry result — Unit: {unit}, Balance: {balance}, Last Polled: {polled}")

    return unit, balance, polled, run_time


def publish_mqtt(unit, balance, polled, run_time):
    if not mqtt_enabled:
        log.info("MQTT not configured, skipping publish.")
        return

    # Don't overwrite good sensor data with N/A — skip publish on failure
    if unit == "N/A" and balance == "N/A" and polled == "N/A":
        log.warning("All values N/A — skipping MQTT publish to preserve last good sensor data.")
        return

    try:
        client = mqtt.Client(  # type: ignore[union-attr] — guarded by mqtt_enabled above
            client_id="",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[union-attr]
            protocol=mqtt.MQTTv5,  # type: ignore[union-attr]
        )
        client.username_pw_set(mqtt_username, mqtt_password)
        client.connect(mqtt_broker, mqtt_port, 60)
        client.publish(topic_unit, unit)
        client.publish(topic_balance, balance)
        client.publish(topic_polled, polled)
        client.publish(topic_lastrun, run_time)
        log.info("Published to MQTT.")
        client.disconnect()
    except Exception as e:
        log.error(f"MQTT error: {e}")


def print_summary(unit, balance, polled, run_time):
    print("\n=== USMS Data Summary ===")
    print(f"Remaining Unit    : {unit}")
    print(f"Remaining Balance : {balance}")
    print(f"Meter Last Polled : {polled}")
    print(f"Last Run Time     : {run_time}")
    print("========================\n")


# === MAIN LOOP ===
log.info("USMS Scrape starting up.")
failure_count = 0

while True:
    driver = None
    try:
        # Create a fresh WebDriver session for this cycle
        driver = create_driver()

        if not is_logged_in(driver):
            login(driver)

        unit, balance, polled, run_time = scrape_data(driver)

        # Track consecutive failures vs successes
        if unit == "N/A" and balance == "N/A" and polled == "N/A":
            failure_count += 1
        else:
            failure_count = 0

        publish_mqtt(unit, balance, polled, run_time)
        print_summary(unit, balance, polled, run_time)

        # Auto-heal: if 3+ consecutive cycles returned N/A, force an exit
        # so Docker's restart policy (unless-stopped) recycles us cleanly.
        if failure_count >= 3:
            log.error(
                "%d consecutive scrape failures — triggering container restart "
                "to recover clean Selenium connection.",
                failure_count,
            )
            raise RuntimeError("Too many consecutive failures — restarting.")

    except Exception as e:
        log.error(f"Error during scrape cycle: {e}")
    finally:
        if driver:
            try:
                driver.quit()
                log.debug("WebDriver session cleaned up.")
            except Exception as e:
                # Log cleanup errors instead of swallowing them silently
                log.warning("WebDriver cleanup warning (non-fatal): %s", e)

    log.info(f"Sleeping {scrape_interval}s…")
    time.sleep(scrape_interval)
