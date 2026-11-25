"""Edge-case battery: does anything break, and does the logger catch it?

Every case injects a fault and asserts BOTH that the code survives and that the
log line carries the right level, the cycle id and enough detail to act on.

Run inside the image (selenium/paho/ddddocr present, no portal, no broker):
    docker run --rm --env-file <stack env> -v fw.py:/tmp/x.py ... python3 /tmp/test-edges.py
"""
import importlib.util
import io
import logging
import os
import re
import sys
import time

SRC = os.environ.get("USMS_SRC", "/app/usms.py")
BASE_ENV = dict(os.environ)
fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


def load(name="edge", env=None):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(BASE_ENV)
    os.environ.setdefault("USMS_USERNAME", "01999999")
    os.environ.setdefault("USMS_PASSWORD", "S3cret-Passw0rd")
    for key, value in (env or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        spec = importlib.util.spec_from_file_location(name, SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        try:
            self.records.append((record.levelname, record.getMessage(), record.exc_info))
        except Exception:
            self.records.append((record.levelname, "<unformattable>", None))

    def start(self, mod, level=logging.DEBUG):
        self.logger = logging.getLogger("usms")
        self._level, self._prop = self.logger.level, self.logger.propagate
        self.logger.setLevel(level)
        self.logger.propagate = False
        self.logger.addHandler(self)
        return self

    def stop(self):
        self.logger.removeHandler(self)
        self.logger.setLevel(self._level)
        self.logger.propagate = self._prop

    def text(self):
        return "\n".join(msg for _, msg, _ in self.records)

    def levels(self):
        return [lvl for lvl, _, _ in self.records]

    def count(self, needle):
        return sum(1 for _, msg, _ in self.records if needle in msg)

    def of_level(self, level):
        return [msg for lvl, msg, _ in self.records if lvl == level]

    def exc_count(self):
        return sum(1 for _, _, exc in self.records if exc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeElement:
    def __init__(self, text="", attrs=None):
        self.text = text
        self.attrs = attrs or {}

    def get_attribute(self, name):
        return self.attrs.get(name)

    def send_keys(self, *a):
        pass

    def clear(self):
        pass


class FakeDriver:
    def __init__(self, cells=None, body="USMS dashboard", title="USMS",
                 url="https://www.usms.com.bn/SmartMeter/MainPage",
                 refresh_present=True, stage2_field=True):
        self.cells = cells or {}
        self.body = body
        self.title = title
        self.current_url = url
        self.page_source = "<html><body>" + body + "</body></html>"
        self.async_result = None
        self.async_exc = None
        self.captcha_src = "cap-1.png"
        self.refresh_present = refresh_present
        self.stage2_field = stage2_field
        self.quit_called = False
        self.raise_on_find = None

    def get(self, url):
        self.current_url = url

    def quit(self):
        self.quit_called = True

    def find_element(self, by, selector):
        if self.raise_on_find:
            raise self.raise_on_find
        if by == "xpath" and selector in self.cells:
            value = self.cells[selector]
            if value is None:
                raise LookupError("no such element")
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
        if "click" in script and args:
            self.captcha_src = self.captcha_src + "x"
        return None

    def execute_async_script(self, script):
        if self.async_exc:
            raise self.async_exc
        return self.async_result


class FakeInfo:
    def __init__(self, rc=0):
        self.rc = rc
        self._ok = rc == 0

    def is_published(self):
        return self._ok

    def wait_for_publish(self, timeout=None):
        return None


class FakeClient:
    def __init__(self, connect_rc=0, publish_rc=0, connect_exc=None, publish_exc=None):
        self.connect_rc = connect_rc
        self.publish_rc = publish_rc
        self.connect_exc = connect_exc
        self.publish_exc = publish_exc
        self.published = []
        self.will = None
        self.connected = False
        self.disconnected = False

    def username_pw_set(self, u, p):
        pass

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect(self, host, port, keepalive=60):
        if self.connect_exc:
            raise self.connect_exc
        self.connected = True
        return self.connect_rc

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        if self.publish_exc:
            raise self.publish_exc
        self.published.append((topic, payload, qos, retain))
        return FakeInfo(self.publish_rc)


def fast(mod):
    """Remove the real sleeps so the battery runs in seconds."""
    mod.time.sleep = lambda seconds=0: None
    return mod


def png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


ENV_STACK = {"MQTT_BROKER": "127.0.0.1", "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p",
             "SCRAPE_INTERVAL": "1800"}

# ===========================================================================
print("\n--- A. logging plumbing")
src = open(SRC).read()
for level, needle in (("debug", "log.debug"), ("info", "log.info"),
                      ("warning", "log.warning"), ("error", "log.error")):
    check(f"A0 log.{level} is used", needle in src)

mod = fast(load("a2", dict(ENV_STACK, LOG_LEVEL="INFO")))
cap = Capture().start(mod, level=getattr(logging, mod.LOG_LEVEL))
mod.validate_value("unit", "1.0 kWh", "zz")
cap.stop()
check("A1 debug lines are hidden at LOG_LEVEL=INFO",
      not cap.of_level("DEBUG"), cap.records[:3])

mod = fast(load("a3", dict(ENV_STACK, LOG_LEVEL="DEBUG")))
cap = Capture().start(mod, level=getattr(logging, mod.LOG_LEVEL))
mod._read_dashboard(FakeDriver(cells={mod.XPATH_UNIT: "1.0 kWh"}), timeout=0.2, cycle_id="zz")
cap.stop()
check("A2 LOG_LEVEL=DEBUG unlocks the per-field detail",
      any("shape check" in m for m in cap.of_level("DEBUG")), "no debug records")

# A3: full fake cycle at DEBUG must never print the credentials
mod = fast(load("a4", dict(ENV_STACK, LOG_LEVEL="DEBUG")))
cap = Capture().start(mod)
mod._navigate(FakeDriver(), mod.DASHBOARD_URL, "dashboard", "s3cr3t")
mod.log_page_source(FakeDriver(body="IC 01999999 balance 67.12"), "test", "s3cr3t")
mod.validate_value("unit", "1.0 kWh", "s3cr3t")
cap.stop()
leaked = any("S3cret-Passw0rd" in m or "01999999" in m for m in [r[1] for r in cap.records])
check("A3 the password and IC never reach the log", not leaked,
      [m for m in [r[1] for r in cap.records] if "01999999" in m or "S3cret" in m][:2])
check("A3b panel body digits are masked", any("#" in m and "IC" in m for m in cap.text().split("\n")),
      "no masked body preview found")

# A4: cycle id present on cycle-scoped lines
mod = fast(load("a5", dict(ENV_STACK)))
cap = Capture().start(mod)
mod.scrape_data(FakeDriver(cells={mod.XPATH_UNIT: "1.0 kWh",
                                  mod.XPATH_BALANCE: "$1.00",
                                  mod.XPATH_POLLED: "24/11/2026"}), cycle_id="cid7")
cap.stop()
phase = [m for m in [r[1] for r in cap.records]
         if any(k in m for k in ("scrap", "unit=", "dashboard read", "shape check"))]
check("A4 every cycle-scoped line carries cycle=<id>",
      phase and all("cycle=cid7" in m for m in phase),
      [m for m in phase if "cycle=cid7" not in m][:2])

# ===========================================================================
print("\n--- B. captcha edge cases")
mod = fast(load("b", ENV_STACK))

d = FakeDriver()
d.async_result = ["http://cap.png", "fetch failed: TypeError"]
cap = Capture().start(mod)
res = mod._captcha_png(d, 1)
cap.stop()
check("B1 2-element JS error reply survives and logs the reason",
      res is None and "image unavailable" in cap.text() and "fetch failed" in cap.text(),
      cap.text()[:160])

d = FakeDriver()
d.async_exc = RuntimeError("script timeout")
cap = Capture().start(mod)
res = mod._captcha_png(d, 2)
cap.stop()
check("B2 a script timeout is logged, not raised",
      res is None and "cache read failed" in cap.text() and "RuntimeError" in cap.text(),
      cap.text()[:160])

d = FakeDriver()
d.async_result = ["http://cap.png", 200, "bm90LWEtcG5n", 10]
cap = Capture().start(mod)
res = mod._captcha_png(d, 3)
cap.stop()
check("B3 a non-PNG payload is logged", res is None and "not a PNG" in cap.text(),
      cap.text()[:160])

d = FakeDriver()
d.async_result = [None, "captcha image element not present"]
cap = Capture().start(mod)
res = mod._captcha_png(d, 4)
cap.stop()
check("B4 a missing captcha element is logged",
      res is None and "image unavailable" in cap.text(), cap.text()[:160])

# OCR engine blowing up
mod = fast(load("b5", ENV_STACK))
mod._captcha_png = lambda driver, attempt=0: png()
def _boom(_png, cycle_id="none"):
    raise ValueError("model file missing")
mod._read_captcha_variants = _boom
cap = Capture().start(mod)
code = mod.solve_captcha(FakeDriver(refresh_present=False), "eocr")
cap.stop()
check("B5 an OCR failure is caught and named",
      code is None and "OCR failed: ValueError: model file missing" in cap.text(),
      cap.text()[:200])

# total disagreement -> give up, 10 rolls, warning carries the cycle
mod = fast(load("b6", ENV_STACK))
mod._captcha_png = lambda driver, attempt=0: png()
mod._read_captcha_variants = lambda png_bytes, cycle_id="none": {
    "raw": "aaaaa", "x2": "bbbbb", "x2bin": "ccccc",
    "beta_raw": "ddddd", "beta_x2bin": "eeeee"}
cap = Capture().start(mod)
code = mod.solve_captcha(FakeDriver(), "edisagree")
cap.stop()
check("B6 total disagreement gives up with a warning that names the cycle",
      code is None and "cycle=edisagree gave up reading the captcha after 10" in cap.text(),
      cap.text()[-200:])
check("B6b each rejected roll explains why at DEBUG",
      "agreement 1 < 4" in cap.text(), "no rejection reason")

# refresh control missing -> stop after one roll instead of re-reading 10x
mod = fast(load("b7", ENV_STACK))
mod._captcha_png = lambda driver, attempt=0: png()
mod._read_captcha_variants = lambda png_bytes, cycle_id="none": {
    "raw": "aaaaa", "x2": "bbbbb", "x2bin": "ccccc",
    "beta_raw": "ddddd", "beta_x2bin": "eeeee"}
cap = Capture().start(mod)
d = FakeDriver(refresh_present=False)
mod.solve_captcha(d, "enorefresh")
cap.stop()
check("B7 a missing refresh control stops the loop and says so",
      "re-roll unavailable after attempt 1" in cap.text() and
      mod._cycle_state["captcha_rolls"] == 1,
      f"rolls={mod._cycle_state['captcha_rolls']}")

# happy path still works
mod = fast(load("b8", ENV_STACK))
mod._captcha_png = lambda driver, attempt=0: png()
mod._read_captcha_variants = lambda png_bytes, cycle_id="none": {
    "raw": "ab12c", "x2": "ab12c", "x2bin": "ab12c",
    "beta_raw": "ab12c", "beta_x2bin": "ab9c3"}
cap = Capture().start(mod)
code = mod.solve_captcha(FakeDriver(), "eok")
cap.stop()
check("B8 a confident read is returned", code == "ab12c", code)
check("B8b the accepted roll is reported at DEBUG",
      "captcha accepted on roll 1" in cap.text())

mod = fast(load("b9", ENV_STACK))
for bad, why in (("ABCDE", "upper"), ("abcd", "short"), ("abcdef", "long"),
                 ("abc d", "space"), ("abcde", "")):
    check(f"B9 '{bad}' ({why or 'valid'}) -> {mod._valid_code(bad)}",
          mod._valid_code(bad) == (bad == "abcde"), bad)

# ===========================================================================
print("\n--- C. dashboard value edge cases")
mod = load("c", ENV_STACK)   # real sleeps: safe_get_text must not spin

d = FakeDriver(cells={mod.XPATH_UNIT: ""})          # present but empty
cap = Capture().start(mod)
val = mod.safe_get_text(d, mod.XPATH_UNIT, timeout=0.3, cycle_id="cempty", field="unit")
cap.stop()
check("C1 an empty cell returns None and warns",
      val is None and "stayed empty" in cap.text(), cap.text()[:160])
check("C1b the warning names the field, not just an XPath",
      "unit:" in cap.text(), cap.text()[:160])

d = FakeDriver()
d.raise_on_find = RuntimeError("invalid session id")
cap = Capture().start(mod)
val = mod.safe_get_text(d, mod.XPATH_UNIT, timeout=0.3, cycle_id="cdied", field="unit")
cap.stop()
check("C2 a dead session is reported with the exception type",
      val is None and "RuntimeError: invalid session id" in cap.text(), cap.text()[:200])

for raw, expected, label in (
    ("1,991.264 kWh", True, "thousands separator"),
    (" 1991.264 kWh ", True, "surrounding spaces"),
    ("1 991.264 kWh", False, "space separator"),
    ("Meter No.", False, "wrong cell"),
    ("N/A", False, "sentinel"),
    ("", False, "empty"),
    ("1991.264", False, "missing unit"),
):
    mod = fast(load("c2", ENV_STACK))
    cap = Capture().start(mod)
    got = mod.validate_value("unit", raw, "cshape")
    cap.stop()
    check(f"C3 unit {label} {raw!r} -> {'accepted' if expected else 'rejected'}",
          (got is not None) == expected,
          f"got={got!r} log={cap.text()[:120]}")
    if not expected:
        check(f"C3b rejection of {raw!r} logs the raw text",
              repr(raw) in cap.text() or raw in cap.text(), cap.text()[:120])

for field, sample, ok in (("balance", "$67.16", True), ("balance", "RM 67.16", True),
                          ("balance", "67.16", True), ("balance", "BND 67", True),
                          ("balance", "$", False),
                          ("polled", "24/11/2026", True), ("polled", "24-11-2026", False),
                          ("polled", "2026-11-24", False)):
    mod = fast(load("c4", ENV_STACK))
    got = mod.validate_value(field, sample, "c4")
    check(f"C4 {field} {sample!r} -> {'accepted' if ok else 'rejected'}",
          (got is not None) == ok, got)

# ===========================================================================
print("\n--- D. MQTT edge cases")
mod = fast(load("d", ENV_STACK))

client = FakeClient(connect_exc=ConnectionRefusedError(111, "Connection refused"))
mod.mqtt.Client = lambda *a, **kw: client
cap = Capture().start(mod)
ok = mod.publish_mqtt("1.0 kWh", "$1.00", "24/11/2026", "2026-11-24 00:00:00", "dmq1")
cap.stop()
check("D1 a refused connect is an ERROR and the cycle is not ok",
      ok is False and "mqtt error" in cap.text() and "ConnectionRefusedError" in cap.text(),
      cap.text()[:200])
check("D1b the client is still torn down", client.disconnected)

client = FakeClient(connect_rc=5)
mod.mqtt.Client = lambda *a, **kw: client
cap = Capture().start(mod)
ok = mod.publish_mqtt("1.0 kWh", "$1.00", "24/11/2026", "t", "dmq2")
cap.stop()
check("D2 a non-zero connect rc is an ERROR naming the rc",
      ok is False and "returned rc=5" in cap.text(), cap.text()[:200])

client = FakeClient(publish_rc=4)
mod.mqtt.Client = lambda *a, **kw: client
cap = Capture().start(mod)
ok = mod.publish_mqtt("1.0 kWh", "$1.00", "24/11/2026", "t", "dmq3")
cap.stop()
check("D3 a rejected publish warns per topic and is not ok",
      ok is False and cap.count("NOT published (rc=4") >= 1, cap.text()[:300])

client = FakeClient(publish_exc=RuntimeError("socket gone"))
mod.mqtt.Client = lambda *a, **kw: client
cap = Capture().start(mod)
ok = mod.publish_mqtt("1.0 kWh", "$1.00", "24/11/2026", "t", "dmq4")
cap.stop()
check("D4 a publish that raises is logged per topic, not swallowed",
      ok is False and "NOT published after" in cap.text() and "socket gone" in cap.text(),
      cap.text()[:200])

client = FakeClient()
mod.mqtt.Client = lambda *a, **kw: client
cap = Capture().start(mod)
ok = mod.publish_mqtt(None, None, None, "t", "dmq5")
cap.stop()
check("D5 an all-missing cycle publishes only status/fail, never 'N/A'",
      ok is False and all("N/A" not in str(p[1]) for p in client.published),
      client.published)

client = FakeClient()
mod.mqtt.Client = lambda *a, **kw: client
mod.publish_mqtt("1.0 kWh", "$1.00", "24/11/2026", "t", "dmq6")
check("D6 the last will is a retained 'offline'",
      client.will == (mod.topic_status, "offline", 1, True), client.will)

# ===========================================================================
print("\n--- E. navigation and page dump")
mod = fast(load("e", ENV_STACK))
from selenium.common.exceptions import TimeoutException as SelTimeout

class TimeoutDriver(FakeDriver):
    def get(self, url):
        raise SelTimeout("timeout: Timed out receiving message from renderer")

cap = Capture().start(mod)
result = mod._navigate(TimeoutDriver(), mod.DASHBOARD_URL, "dashboard", "enav")
cap.stop()
check("E1 a page-load timeout is a warning and the cycle continues",
      result is False and "did not finish loading" in cap.text(), cap.text()[:160])

cap = Capture().start(mod)
d = FakeDriver(body="x" * 5000)
mod.log_page_source(d, "huge page", "edump")
cap.stop()
check("E2 a large body is truncated in the log and saved in full",
      "full page saved to" in cap.text(), cap.text()[:200])

import builtins
real_open = builtins.open
def _deny(path, *a, **kw):
    if str(path).startswith("/tmp/usms-fail-"):
        raise OSError(28, "No space left on device")
    return real_open(path, *a, **kw)
builtins.open = _deny
cap = Capture().start(mod)
mod.log_page_source(FakeDriver(), "disk full", "edisk")
cap.stop()
builtins.open = real_open
check("E3 an unwritable dump path is a warning, not a crash",
      "could not save the page source" in cap.text() and "No space left" in cap.text(),
      cap.text()[:200])

# ===========================================================================
print("\n--- F. lifecycle: exceptions, login failure, SIGTERM, overrun")
mod = fast(load("f", ENV_STACK))

cap = Capture().start(mod)
def _raise_driver():
    raise RuntimeError("Selenium host not reachable after 5 attempts: MaxRetryError")
mod.create_driver = _raise_driver
try:
    outcome = mod.run_cycle("fexc")
except Exception as e:                       # run_cycle re-raises; main() logs it
    outcome = None
    cap_exc = e
cap.stop()
check("F1 a phase exception propagates (main logs it with a traceback) and the "
      "cycle summary still says result=fail",
      outcome is None and "result=fail" in cap.text(), cap.text()[-200:])

cap = Capture().start(mod)
mod.create_driver = lambda: FakeDriver()
mod.is_logged_in = lambda driver, cycle_id="none": False
mod.login = lambda driver, cycle_id="none": False
d = FakeDriver()
mod.create_driver = lambda: d
try:
    mod.run_cycle("flogin")
except Exception as e:
    err = e
cap.stop()
check("F2 a failed login raises with a named reason",
      isinstance(err, RuntimeError) and "login failed" in str(err), repr(err))
check("F2b the driver is still quit on that path", d.quit_called)

mod = fast(load("f3", ENV_STACK))
cap = Capture().start(mod)
mod.install_signal_handlers()
import signal as _signal
try:
    mod.signal.getsignal(_signal.SIGTERM)(_signal.SIGTERM, None)
    raised = None
except SystemExit as e:
    raised = e
cap.stop()
check("F3 SIGTERM is turned into SystemExit with an INFO line",
      isinstance(raised, SystemExit) and "Received signal" in cap.text(), cap.text()[:160])

# overrun guard: a cycle that takes longer than the interval must warn, not burst
mod = load("f4", dict(ENV_STACK, SCRAPE_INTERVAL="60"))
clock = [1000.0]
mod.time.monotonic = lambda: clock[0]
cycles = []
def _cycle(cycle_id="none"):
    cycles.append(cycle_id)
    clock[0] += 130.0                     # this cycle overruns the 60s interval
    return {"status": "ok", "published": True}
def _sleep(seconds=0):
    clock[0] += float(seconds or 0)
    raise SystemExit(0)                   # one iteration is enough to observe the guard
mod.run_cycle = _cycle
mod.time.sleep = _sleep
cap = Capture().start(mod, level=getattr(logging, mod.LOG_LEVEL))
try:
    mod.main()
except SystemExit:
    pass
cap.stop()
check("F4 an overrun cycle warns instead of firing a catch-up burst",
      "overran its 60s interval" in cap.text() and len(cycles) == 1,
      f"cycles={len(cycles)} log={cap.text()[-260:]}")

# ===========================================================================
print("\n--- G. timezone edge cases")
mod = fast(load("g", {"USMS_TIMEZONE": "Mars/Olympus"}))
cap = Capture().start(mod)
tz = mod.local_timezone()
stamp = mod.now_stamp()
cap.stop()
check("G1 an unknown timezone warns and falls back to UTC+8",
      "is unknown to this platform" in cap.text() or tz is not None, cap.text()[:160])
from datetime import datetime, timezone as _tz, timedelta as _td
expected = (datetime.now(_tz.utc) + _td(hours=8)).strftime("%Y-%m-%d %H:%M")
check("G1b the fallback stamp is UTC+8 wall clock", stamp.startswith(expected),
      f"stamp={stamp} expected~{expected}")

mod = fast(load("g2", {"USMS_TIMEZONE": "Asia/Brunei"}))
stamp = mod.now_stamp()
check("G2 Asia/Brunei stamps the same wall clock", stamp.startswith(expected), stamp)

# ===========================================================================
print()
print(f"{'ALL EDGE CASES PASSED' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
