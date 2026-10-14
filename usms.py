import os
import io
import time
import json
import base64
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from PIL import Image
import ddddocr
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


# ---------------------------------------------------------------------------
# Captcha solving
#
# The USMS portal now protects the login with a DevExpress ASPxCaptcha. The
# captcha lives on the SECOND stage of the login (see login() below), not on the
# first. Two properties of that image drive the design here:
#   1. Its URL is single-use — fetching it a second time returns 404 — so the
#      original bytes have to be read out of the browser's own HTTP cache.
#   2. The codes are random 5-character lowercase alphanumeric strings.
# ddddocr is a CPU-only ONNX model, so there is no browser/GPU dependency. Every
# image is read five different ways (two model generations x three preprocessings)
# and only an answer the models AGREE on is submitted. A disagreement costs a
# free re-roll ("Show another code"), never a login attempt.
# ---------------------------------------------------------------------------

CAPTCHA_MIN_VOTES = 4      # of 5 readings
CAPTCHA_MAX_ROLLS = 10     # re-roll budget per login attempt
CAPTCHA_CODE_LENGTH = 5
_captcha_engines = None


def _engines():
    """Load ddddocr lazily so a model problem can never stop the container booting."""
    global _captcha_engines
    if _captcha_engines is None:
        _captcha_engines = (
            ddddocr.DdddOcr(show_ad=False),
            ddddocr.DdddOcr(show_ad=False, beta=True),
        )
    return _captcha_engines


def _read_captcha_variants(png_bytes):
    """Five independent readings of the same captcha image."""
    eng_old, eng_beta = _engines()
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    canvas = Image.new("RGBA", im.size, (255, 255, 255, 255))  # flatten transparency to white
    canvas.alpha_composite(im)
    rgb = canvas.convert("RGB")

    buf = io.BytesIO(); rgb.save(buf, format="PNG"); raw = buf.getvalue()
    upscaled = rgb.resize((rgb.width * 2, rgb.height * 2), Image.LANCZOS)
    buf = io.BytesIO(); upscaled.save(buf, format="PNG"); x2 = buf.getvalue()
    binarised = upscaled.convert("L").point(lambda p: 0 if p < 180 else 255).convert("RGB")
    buf = io.BytesIO(); binarised.save(buf, format="PNG"); x2bin = buf.getvalue()

    return {
        "raw": eng_old.classification(raw).strip().lower(),
        "x2": eng_old.classification(x2).strip().lower(),
        "x2bin": eng_old.classification(x2bin).strip().lower(),
        "beta_raw": eng_beta.classification(raw).strip().lower(),
        "beta_x2bin": eng_beta.classification(x2bin).strip().lower(),
    }


def _valid_code(code):
    return len(code) == CAPTCHA_CODE_LENGTH and all(
        c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in code)


# The captcha image URL 404s once the browser has rendered it, so pull the bytes
# back out of Chrome's HTTP cache instead of asking the server again.
_STEAL_CAPTCHA_JS = """
const cb = arguments[arguments.length - 1];
const img = document.getElementById('ASPxRoundPanel1_ASPxCaptcha1_IMG');
if (!img) { cb([null, 'captcha image element not present']); }
else {
  fetch(img.src, {cache: 'force-cache'}).then(async r => {
    const bytes = new Uint8Array(await r.arrayBuffer());
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    cb([img.src, r.status, btoa(s), bytes.length]);
  }).catch(e => cb([img.src, 'fetch failed: ' + e]));
}
"""


def _captcha_png(driver):
    res = driver.execute_async_script(_STEAL_CAPTCHA_JS)
    if not res or not res[0] or not isinstance(res[2], str):
        log.debug("Captcha image unavailable: %s", res)
        return None
    data = base64.b64decode(res[2])
    if not data.startswith(b"\x89PNG"):
        log.debug("Captcha payload was not a PNG (%d bytes)", len(data))
        return None
    return data


def solve_captcha(driver):
    """Read the stage-2 captcha, re-rolling until the OCR models agree."""
    for attempt in range(CAPTCHA_MAX_ROLLS):
        png = _captcha_png(driver)
        if png:
            readings = _read_captcha_variants(png)
            code, votes = Counter(readings.values()).most_common(1)[0]
            log.info("Captcha attempt %d/%d: %r (%d/5 models agree) %s",
                     attempt + 1, CAPTCHA_MAX_ROLLS, code, votes, json.dumps(readings))
            if _valid_code(code) and votes >= CAPTCHA_MIN_VOTES:
                return code
        # Disagreement or unreadable: ask for a new challenge instead of guessing.
        refresh = driver.find_elements(By.CSS_SELECTOR, "[id$='ASPxCaptcha1_RB']")
        if refresh:
            driver.execute_script("arguments[0].click();", refresh[0])
        time.sleep(2)
    log.warning("Gave up reading the captcha after %d attempts.", CAPTCHA_MAX_ROLLS)
    return None


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
            driver = webdriver.Remote(
                command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
                options=make_chrome_options()
            )
            driver.set_script_timeout(30)  # needed by the captcha cache read
            return driver
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


def _click(driver, css_selector):
    """DevExpress renders buttons as <a> wrappers whose inner span can refuse a native click."""
    el = driver.find_element(By.CSS_SELECTOR, css_selector)
    driver.execute_script("arguments[0].click();", el)


def _type(driver, css_selector, text):
    el = driver.find_element(By.CSS_SELECTOR, css_selector)
    driver.execute_script("arguments[0].focus();", el)
    el.clear()
    el.send_keys(text)


def login(driver):
    """Two-stage login.

    Stage 1 is the plain form (IC + password) — it carries no captcha for a human.
    Submitting it returns ResLogin?sc=…, a second form that asks for the IC and
    password AGAIN plus a captcha. The captcha is solved by OCR (see above) and the
    second form is submitted, which lands on /MainPage.
    """
    log.info("Logging in…")
    driver.get(LOGIN_URL)

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.ID, "ASPxRoundPanel1_txtUsername_I")))
    _type(driver, "#ASPxRoundPanel1_txtUsername_I", usms_username)
    _type(driver, "#ASPxRoundPanel1_txtPassword_I", usms_password)
    _click(driver, "#ASPxRoundPanel1_btnLogin")

    # Either the captcha stage shows up (normal) or we went straight in.
    try:
        WebDriverWait(driver, 30).until(
            lambda d: "sc=" in d.current_url or "ResLogin" not in d.current_url)
    except TimeoutException:
        log.warning("No response to the stage-1 submit — dumping page state.")
        _log_page_source(driver, "After stage 1 timeout")
        return False

    if "sc=" not in driver.current_url:
        log.info("Login succeeded without a captcha stage — landed on %s", driver.current_url)
        return True

    log.info("Stage 1 accepted; solving the stage-2 captcha on %s", driver.current_url)
    code = solve_captcha(driver)
    if not code:
        _log_page_source(driver, "Captcha unreadable")
        return False

    # Fill stage 2: same credentials again, plus the solved captcha.
    tb = driver.find_elements(By.CSS_SELECTOR, "[name$='ASPxCaptcha1$TB']")
    if not tb:
        log.warning("Stage-2 captcha input field not found.")
        return False
    driver.execute_script("arguments[0].focus();", tb[0])
    tb[0].send_keys(code)

    username_field = driver.find_element(By.CSS_SELECTOR, "#ASPxRoundPanel1_txtUsername_I")
    password_field = driver.find_element(By.CSS_SELECTOR, "#ASPxRoundPanel1_txtPassword_I")
    if not username_field.get_attribute("value"):
        username_field.send_keys(usms_username)
    if not password_field.get_attribute("value"):
        password_field.send_keys(usms_password)

    _click(driver, "#ASPxRoundPanel1_btnLogin")
    try:
        WebDriverWait(driver, 30).until(lambda d: "ResLogin" not in d.current_url)
    except TimeoutException:
        log.warning("Dashboard did not appear after the stage-2 submit — dumping page state.")
        _log_page_source(driver, "After stage 2 timeout")
        return False

    log.info("Login successful — dashboard loaded at %s", driver.current_url)
    return True


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
