"""Proof harness for the v0.15.10 audit fixes in usms.py.

Runs INSIDE the published image (selenium / paho / ddddocr present):

    docker exec usms-scrape python3 /tmp/test-audit.py        # USMS_SRC=/tmp/usms.py

Nothing here talks to the USMS portal: every driver and MQTT client is a fake.
The two lifecycle tests execute main() itself and prove the container now really
exits, which is what the restart policy needs.

Exit code 0 = all checks passed.
"""
import base64
import importlib.util
import io
import logging
import os
import re
import signal
import sys
import time

SRC = os.environ.get("USMS_SRC", "/app/usms.py")
BASE_ENV = dict(os.environ)
fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


def load(name="usms_ut", env=None):
    """Fresh module instance, with the process environment overridden for that load."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(BASE_ENV)
    for key, value in (env or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        spec = importlib.util.spec_from_file_location(name, SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # __main__ guard must keep this side-effect free
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage(), record.exc_info))

    def start(self, mod):
        self.logger = logging.getLogger("usms")
        self._level, self._prop = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(self)
        return self

    def stop(self):
        self.logger.removeHandler(self)
        self.logger.setLevel(self._level)
        self.logger.propagate = self._prop

    def text(self):
        return "\n".join(msg for _, msg, _ in self.records)

    def count(self, needle):
        return sum(1 for _, msg, _ in self.records if needle in msg)

    def exc_count(self):
        return sum(1 for _, _, exc in self.records if exc)

    def find(self, pattern):
        rx = re.compile(pattern)
        for level, msg, _ in self.records:
            if rx.search(msg):
                return msg
        return None


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeElement:
    def __init__(self, text="", attrs=None):
        self.text = text
        self.attrs = attrs or {}

    def get_attribute(self, name):
        return self.attrs.get(name)

    def send_keys(self, *args):
        pass

    def clear(self):
        pass


class FakeDriver:
    """Minimal stand-in: XPath cells, the page body, the captcha image."""

    def __init__(self):
        self.cells = {}                 # xpath -> list of str/None (last value repeats)
        self.body = ""
        self.title = "USMS Smart Meter"
        self.current_url = "https://www.usms.com.bn/SmartMeter/MainPage"
        self.page_source = "<html><body>dashboard</body></html>"
        self.async_result = None
        self.async_exc = None
        self.captcha_src = "captcha-1.png"
        self.refresh_present = True
        self.stage2_field = True
        self.quit_called = False
        self.gets = []
        self.scripts = []

    def get(self, url):
        self.gets.append(url)
        self.current_url = url

    def quit(self):
        self.quit_called = True

    def find_element(self, by, selector):
        if by == "xpath" and selector in self.cells:
            queue = self.cells[selector]
            value = queue.pop(0) if len(queue) > 1 else queue[0]
            if value is None:
                raise LookupError(f"no such element: {selector}")
            return FakeElement(value)
        if by == "tag name" and selector == "body":
            return FakeElement(self.body)
        if by == "id":
            return FakeElement(attrs={"src": self.captcha_src})
        raise LookupError(f"no such element: {by}={selector}")

    def find_elements(self, by, selector):
        if by == "css selector" and "ASPxCaptcha1_RB" in selector:
            return [FakeElement()] if self.refresh_present else []
        if by == "css selector" and "ASPxCaptcha1$TB" in selector:
            return [FakeElement()] if self.stage2_field else []
        return []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "click" in script and args:
            # "Show another code" swaps the image URL, like DevExpress does.
            self.captcha_src = f"captcha-{len(self.scripts) + 1}.png"
        return None

    def execute_async_script(self, script):
        if self.async_exc:
            raise self.async_exc
        return self.async_result


class FakeWait:
    """WebDriverWait stand-in: either resolves immediately or times out at once."""

    mode = "timeout"

    def __init__(self, driver, timeout):
        pass

    def until(self, condition):
        if FakeWait.mode == "ok":
            return True
        raise TimeoutError("simulated wait timeout")


class FakeInfo:
    def __init__(self, rc=0):
        self.rc = rc
        self._published = rc == 0

    def is_published(self):
        return self._published

    def wait_for_publish(self, timeout=None):
        return None


class FakeClient:
    def __init__(self, rc=0, connect_rc=0):
        self.published = []
        self.will = None
        self.rc = rc
        self.connect_rc = connect_rc
        self.connected = False
        self.disconnected = False

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect(self, host, port, keepalive=60):
        self.connected = True
        return self.connect_rc

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        if isinstance(topic, str) and topic:
            self.published.append({"topic": topic, "payload": payload,
                                   "qos": qos, "retain": retain})
        return FakeInfo(self.rc)


def patch_client(mod, **kwargs):
    client = FakeClient(**kwargs)
    mod.mqtt.Client = lambda *a, **kw: client
    return client


def make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 40), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ===========================================================================
# A. Import hygiene + config validation
# ===========================================================================
print("\n--- A. import hygiene + config validation")

mod = load("a1")
check("A1 import does not start the scrape loop (__main__ guard)",
      callable(getattr(mod, "main", None)) and not hasattr(mod, "failure_count"),
      "module-level loop variables leaked into the namespace")

mod = load("a2", {"SCRAPE_INTERVAL": "30m"})
cap = Capture().start(mod)
try:
    mod.validate_config()
    code = None
except SystemExit as e:
    code = e.code
cap.stop()
check("A2 a non-numeric SCRAPE_INTERVAL exits 2 instead of ValueError at import",
      code == 2 and "SCRAPE_INTERVAL must be a whole number" in cap.text(), f"code={code}")

mod = load("a3", {"SCRAPE_INTERVAL": "0"})
cap = Capture().start(mod)
try:
    mod.validate_config()
    code = None
except SystemExit as e:
    code = e.code
cap.stop()
check("A3 SCRAPE_INTERVAL=0 is refused (portal hammering)",
      code == 2 and "below the" in cap.text(), f"code={code}")

mod = load("a4", {"USMS_PASSWORD": ""})
cap = Capture().start(mod)
try:
    mod.validate_config()
    code = None
except SystemExit as e:
    code = e.code
cap.stop()
check("A4 an empty password fails fast and names the variable",
      code == 2 and "USMS_PASSWORD is empty" in cap.text(), f"code={code}")

mod = load("a5", {"MQTT_PORT": "eight"})
cap = Capture().start(mod)
try:
    mod.validate_config()
    code = None
except SystemExit as e:
    code = e.code
cap.stop()
check("A5 a non-numeric MQTT_PORT is reported, not defaulted silently",
      code == 2 and "MQTT_PORT" in cap.text(), f"code={code}")

mod = load("a6", {"LOG_LEVEL": "debug"})
check("A6 LOG_LEVEL=debug is honoured", mod.LOG_LEVEL == "DEBUG", mod.LOG_LEVEL)
check("A6b LOG_LEVEL=debug does NOT enable selenium/urllib3 HTTP debug "
      "(those records carry the password and IC number)",
      not logging.getLogger("urllib3").isEnabledFor(logging.DEBUG)
      and not logging.getLogger("selenium").isEnabledFor(logging.DEBUG),
      (logging.getLogger("urllib3").getEffectiveLevel(),
       logging.getLogger("selenium").getEffectiveLevel()))
check("A6c the application's own debug records still reach a handler",
      logging.getLogger("usms").isEnabledFor(logging.DEBUG))
mod = load("a7", {"LOG_LEVEL": "loud"})
check("A7 a nonsense LOG_LEVEL falls back to INFO", mod.LOG_LEVEL == "INFO", mod.LOG_LEVEL)

mod = load("a8", {"USMS_USERNAME": "IC123456", "USMS_PASSWORD": "hunter2"})
cap = Capture().start(mod)
mod.log_startup_config()
cap.stop()
line = cap.find(r"^config: ")
check("A8 startup echo reports presence, never the credential values",
      line is not None and "IC123456" not in cap.text() and "hunter2" not in cap.text()
      and "username=set" in (line or ""), line)

# ===========================================================================
# B. create_driver retries when Selenium is down (was dead code)
# ===========================================================================
print("\n--- B. Selenium retry loop")

mod = load("b1")
cap = Capture().start(mod)
real_sleep = mod.time.sleep
mod.time.sleep = lambda *_: None
real_remote = mod.webdriver.Remote


def boom(*args, **kwargs):
    raise ConnectionRefusedError("[Errno 111] Connection refused")


mod.webdriver.Remote = boom
try:
    try:
        mod.create_driver()
        raised = None
    except RuntimeError as e:
        raised = str(e)
finally:
    mod.webdriver.Remote = real_remote
    mod.time.sleep = real_sleep
    cap.stop()

check("B1 a refused connection (not WebDriverException) is retried 5 times",
      cap.count("Selenium not ready yet") == 5, f"retry lines={cap.count('Selenium not ready yet')}")
check("B2 the last error is surfaced in the RuntimeError",
      raised is not None and "ConnectionRefusedError" in raised, raised)

# ===========================================================================
# C. Auto-heal: main() actually exits after 3 failed cycles
# ===========================================================================
print("\n--- C. auto-heal / failure accounting")

mod = load("c1")
cap = Capture().start(mod)
real_sleep = mod.time.sleep
mod.time.sleep = lambda *_: None
calls = {"n": 0}


def dead_selenium():
    calls["n"] += 1
    raise RuntimeError("Selenium host not reachable after 5 attempts")


mod.create_driver = dead_selenium
try:
    try:
        mod.main()
        exit_code = None
    except SystemExit as e:
        exit_code = e.code
finally:
    mod.time.sleep = real_sleep
    cap.stop()

check("C1 three failed cycles exit(1) so the restart policy recycles us",
      exit_code == 1, f"exit={exit_code}")
check("C2 it exited after exactly 3 attempts (not 55 silent cycles)",
      calls["n"] == 3, f"attempts={calls['n']}")
check("C3 the old swallowed RuntimeError text is gone",
      cap.count("Error during scrape cycle") == 0,
      cap.find(r"Error during scrape cycle"))
check("C4 the failure funnel logs a traceback (exc_info)",
      cap.exc_count() >= 3, f"records with exc_info={cap.exc_count()}")

# ===========================================================================
# D. login failure is fatal, scrape is never attempted
# ===========================================================================
print("\n--- D. login failure handling")

mod = load("d1")
driver = FakeDriver()
scraped = {"called": False}
mod.create_driver = lambda: driver
mod.is_logged_in = lambda d, cycle_id="none": False
mod.login = lambda d, cycle_id="none": False


def must_not_run(*a, **kw):
    scraped["called"] = True
    raise AssertionError("scrape_data() ran after a failed login")


mod.scrape_data = must_not_run
cap = Capture().start(mod)
try:
    try:
        mod.run_cycle("d001")
        raised = None
    except RuntimeError as e:
        raised = str(e)
    cap.stop()
finally:
    pass

check("D1 a login() that returns False raises instead of scraping",
      raised == "login failed", raised)
check("D2 scrape_data was not called after the failed login", not scraped["called"])
check("D3 the WebDriver is still quit when the cycle fails", driver.quit_called)

# ===========================================================================
# E. safe_get_text waits for text, not presence
# ===========================================================================
print("\n--- E. empty-cell handling")

mod = load("e1")
real_sleep = mod.time.sleep
mod.time.sleep = lambda *_: None
driver = FakeDriver()
driver.cells[mod.XPATH_UNIT] = ["", "   ", "1991.264 kWh"]
value = mod.safe_get_text(driver, mod.XPATH_UNIT, timeout=1)
mod.time.sleep = real_sleep
check("E1 a blank cell is waited on until text appears", value == "1991.264 kWh", repr(value))

mod = load("e2")
real_sleep = mod.time.sleep
mod.time.sleep = lambda *_: None
driver = FakeDriver()
driver.cells[mod.XPATH_UNIT] = [None]
cap = Capture().start(mod)
value = mod.safe_get_text(driver, mod.XPATH_UNIT, timeout=0.3)
cap.stop()
mod.time.sleep = real_sleep
check("E2 a missing cell returns None, never the string 'N/A'", value is None, repr(value))

# ===========================================================================
# F. value-shape validation on absolute XPaths
# ===========================================================================
print("\n--- F. value shapes")

mod = load("f1")
cap = Capture().start(mod)
cases = [
    ("unit", "1991.264 kWh", True),
    ("unit", "2,003.508 kWh", True),
    ("unit", "Meter No.", False),
    ("unit", "220.1", False),
    ("balance", "$67.16", True),
    ("balance", "67.16", True),
    ("balance", "RM 12.30", True),
    ("balance", "N/A", False),
    ("polled", "24/11/2026", True),
    ("polled", "24-11-2026", False),
    ("polled", "Last Polled", False),
]
bad = [(f, v) for f, v, ok in cases if (mod.validate_value(f, v) is not None) != ok]
cap.stop()
check("F1 every field shape is accepted/rejected as specified", not bad, bad)
check("F2 a rejected value warns with the raw text",
      "does not look like" in cap.text() and "Meter No." in cap.text())

# ===========================================================================
# G. partial scrape never publishes N/A over good data
# ===========================================================================
print("\n--- G. MQTT publish semantics")

mod = load("g1")
client = patch_client(mod)
ok = mod.publish_mqtt("1991.264 kWh", None, "24/11/2026", "2026-11-25 00:30:00", "g001")
topics = {entry["topic"]: entry["payload"] for entry in client.published}
check("G1 a partial cycle publishes only the fields that validated",
      ok is False and set(topics) == {mod.topic_unit, mod.topic_polled, mod.topic_status},
      topics)
check("G2 the status topic says 'partial'", topics.get(mod.topic_status) == "partial", topics)
check("G3 last_run is NOT advanced on a partial cycle", mod.topic_lastrun not in topics, topics)
check("G4 no literal 'N/A' is ever published",
      all(payload != "N/A" for payload in topics.values()), topics)
check("G5 the last will marks the scraper offline", client.will is not None
      and client.will[0] == mod.topic_status and client.will[1] == "offline", client.will)

mod = load("g2")
client = patch_client(mod)
ok = mod.publish_mqtt("1991.264 kWh", "$67.16", "24/11/2026", "2026-11-25 00:30:00", "g002")
topics = {entry["topic"]: entry["payload"] for entry in client.published}
check("G6 a complete cycle publishes all five topics",
      ok is True and len(topics) == 5 and topics.get(mod.topic_status) == "ok", topics)
check("G7 every publish is QoS 1 and retained",
      all(entry["qos"] == 1 and entry["retain"] for entry in client.published),
      client.published)

mod = load("g3")
client = patch_client(mod, rc=4)
cap = Capture().start(mod)
ok = mod.publish_mqtt("1991.264 kWh", "$67.16", "24/11/2026", "2026-11-25 00:30:00", "g003")
cap.stop()
check("G8 a broker that rejects the publish is reported, not logged as success",
      ok is False and "NOT published" in cap.text() and client.disconnected, cap.text()[-300:])

# ===========================================================================
# H. captcha failure shapes and re-roll
# ===========================================================================
print("\n--- H. captcha robustness")

mod = load("h1")
driver = FakeDriver()
driver.async_result = ["https://x/captcha.png", "fetch failed: TypeError: failed to fetch"]
cap = Capture().start(mod)
result = mod._captcha_png(driver, 1)
cap.stop()
check("H1 the 2-element JS error shape returns None instead of IndexError",
      result is None, repr(result))
check("H2 the failure is logged at WARNING with the attempt number",
      "Captcha attempt 1" in cap.text(), cap.text())

driver = FakeDriver()
driver.async_result = [None, "captcha image element not present"]
check("H3 a null src returns None", mod._captcha_png(driver, 2) is None)

driver = FakeDriver()
driver.async_result = ["src", 200, base64.b64encode(b"not a png at all").decode(), 16]
check("H4 a non-PNG payload returns None", mod._captcha_png(driver, 3) is None)

png = make_png()
driver = FakeDriver()
driver.async_result = ["src", 200, base64.b64encode(png).decode(), len(png)]
check("H5 a real PNG comes back as bytes", mod._captcha_png(driver, 4) == png)

driver = FakeDriver()
driver.async_exc = RuntimeError("script timeout")
check("H6 a script timeout returns None instead of raising",
      mod._captcha_png(driver, 5) is None)

driver = FakeDriver()
driver.async_result = 42          # not a JS callback tuple at all
check("H6b a non-sequence callback value returns None instead of raising",
      mod._captcha_png(driver, 6) is None)

driver = FakeDriver()
driver.refresh_present = False
cap = Capture().start(mod)
refreshed = mod._refresh_captcha(driver, "captcha-1.png")
cap.stop()
check("H7 a missing refresh control reports it and does not pretend to re-roll",
      refreshed is False and "refresh control" in cap.text(), refreshed)

driver = FakeDriver()
check("H8 a re-roll waits for the image URL to change",
      mod._refresh_captcha(driver, "captcha-1.png") is True and driver.captcha_src != "captcha-1.png",
      driver.captcha_src)

mod = load("h2")
driver = FakeDriver()
driver.refresh_present = False
driver.async_result = ["src", 200, base64.b64encode(b"junk").decode(), 4]
cap = Capture().start(mod)
code = mod.solve_captcha(driver)
cap.stop()
check("H9 an impossible re-roll stops after one attempt instead of re-reading 10x",
      code is None and mod._cycle_state["captcha_rolls"] == 1,
      mod._cycle_state["captcha_rolls"])

# ===========================================================================
# I. page dump: digits masked in the log, full DOM kept on disk
# ===========================================================================
print("\n--- I. failure page dump")

mod = load("i1")
driver = FakeDriver()
driver.body = "IC No 123456789 balance $67.16 meter 2003.508"
cap = Capture().start(mod)
mod.log_page_source(driver, "All values N/A", "i001")
cap.stop()
text = cap.text()
path_match = re.search(r"full page saved to (\S+) \(", text)
saved = path_match.group(1) if path_match else None
check("I1 digit runs are masked in the logged body preview",
      "IC No #########" in text and "123456789" not in text, text)
check("I2 the full DOM is written to /tmp and its path is logged",
      saved is not None and os.path.exists(saved), saved)
if saved and os.path.exists(saved):
    os.unlink(saved)

# ===========================================================================
# J. structured per-cycle summary + timings
# ===========================================================================
print("\n--- J. observability")

mod = load("j1", {"MQTT_BROKER": ""})
driver = FakeDriver()
driver.cells = {
    mod.XPATH_UNIT: ["1991.264 kWh"],
    mod.XPATH_BALANCE: ["$67.16"],
    mod.XPATH_POLLED: ["24/11/2026"],
    mod.XPATH_DASHBOARD_DIV: ["dashboard"],
}
mod.create_driver = lambda: driver
mod.is_logged_in = lambda d, cycle_id="none": True
cap = Capture().start(mod)
outcome = mod.run_cycle("j001")
cap.stop()
summary = cap.find(r"^cycle=j001 result=ok ")
check("J1 a healthy cycle reports result=ok", outcome["status"] == "ok" and outcome["published"] is True,
      outcome)
check("J2 one structured summary line carries values, captcha rolls and phase timings",
      summary is not None
      and "unit='1991.264 kWh'" in summary
      and re.search(r"driver=\d\.\ds login=\d\.\ds scrape=\d\.\ds publish=\d\.\ds total=\d\.\ds", summary),
      summary)
check("J3 the cycle id appears on every line of the cycle", cap.count("cycle=j001") >= 3,
      cap.count("cycle=j001"))

# ===========================================================================
# K. SIGTERM releases the WebDriver
# ===========================================================================
print("\n--- K. shutdown handling")

mod = load("k1")
old = signal.getsignal(signal.SIGTERM)
mod.install_signal_handlers()
handler = signal.getsignal(signal.SIGTERM)
try:
    raised = None
    try:
        handler(signal.SIGTERM, None)
    except SystemExit:
        raised = "SystemExit"
finally:
    signal.signal(signal.SIGTERM, old)

check("K1 SIGTERM is turned into SystemExit so the finally block runs",
      raised == "SystemExit", raised)

# ===========================================================================
# L. source-level guarantees that are cheap to assert
# ===========================================================================
print("\n--- L. source checks")

src = open(SRC, encoding="utf-8").read()
check("L1 no hardcoded +8 offset remains", "timedelta(hours=8)).strftime" not in src)
check("L2 the scheduler accumulates next_run instead of sleeping a full interval",
      "next_run += scrape_interval" in src)
check("L3 the page-load timeout is set on the driver", "set_page_load_timeout" in src)
check("L4 the cycle handler logs tracebacks", "log.exception(" in src)

print("\n" + ("ALL CHECKS PASSED" if not fails else f"FAILURES ({len(fails)}): {fails}"))
sys.exit(0 if not fails else 1)
