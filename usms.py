import os
import glob
import io
import re
import sys
import json
import time
import uuid
import base64
import signal
import logging
import threading
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
    from zoneinfo import ZoneInfo
except ImportError:      # Python < 3.9
    ZoneInfo = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # Optional

# ---------------------------------------------------------------------------
# Logging
#
# LOG_LEVEL makes the existing debug detail reachable without a rebuild. An
# invalid value is reported and ignored rather than crashing the container.
# ---------------------------------------------------------------------------

_VALID_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")
_log_level_raw = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
LOG_LEVEL = _log_level_raw if _log_level_raw in _VALID_LEVELS else "INFO"

# The ROOT logger stays at INFO on purpose. selenium/urllib3 emit one debug record
# per WebDriver HTTP request, and those bodies contain the account password and,
# on the login page, the IC number. LOG_LEVEL raises only this application's
# logger, so its own debug detail becomes readable without shipping credentials
# into the container log (verified live: urllib3 debug leaked both).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("usms")
log.setLevel(getattr(logging, LOG_LEVEL))

if LOG_LEVEL != _log_level_raw:
    log.warning("LOG_LEVEL=%r is not a logging level — using INFO.", _log_level_raw)


# ---------------------------------------------------------------------------
# Configuration
#
# Parsing is deliberately side-effect free: a bad value is carried through as a
# sentinel and reported by validate_config() with one actionable line, instead
# of raising ValueError at import time and crash-looping with a traceback.
# ---------------------------------------------------------------------------

_INVALID = object()


def _env_text(name, default=None):
    """Env value, stripped; empty is the same as unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


# A credential value may be written `b64:<base64>` to survive Docker Compose's `$`
# interpolation while staying inline in the compose file. The prefix makes the
# encoding explicit — there is no guessing whether a value "looks like" base64.
B64_PREFIX = "b64:"

# Credential-failure state: the portal said the username/password are wrong. This is
# NOT a transient failure — retrying only burns the portal's attempt budget (it
# allows 5, then locks the account), so the loop stops and waits for a human.
class CredentialsRejected(RuntimeError):
    pass


PLAIN_PREFIX = "plain:"


AUTODETECT_MIN_LEN = 16   # below this the false-positive rate on real passwords hurts


def _looks_like_base64(raw):
    """The decoded text when `raw` is unambiguously base64-of-something, else None.

    Why guess at all: a user who pastes a base64 value from any encoder, or the
    Base64 of their password, should not also have to remember a prefix. The test is
    deliberately conservative — the whole value must be in the base64 alphabet, its
    length a multiple of 4, at least 12 characters (so a short password can never
    collide), and it must decode to printable UTF-8 of at least 6 characters. Anything
    decoding to control characters is treated as a literal password, not base64.
    """
    compact = re.sub(r"\s+", "", raw or "")
    # 16, not 12: at 12 characters ~1 in 1300 ordinary passwords is valid base64 and was
    # silently decoded into a different secret. Padding is the exception — a value ending
    # in '=' is base64 by construction (a 9-character password encodes to 12 characters
    # with one '='), and a human password ending in '=' is vanishingly rare.
    if len(compact) % 4 != 0:
        return None
    if len(compact) < AUTODETECT_MIN_LEN and not compact.endswith("="):
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return None
    try:
        text = base64.b64decode(compact, validate=True).decode("utf-8")
    except Exception:
        return None
    if len(text) < 6 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    return text


def _decode_prefixed(raw, name="credential"):
    """(value, problem, kind) for a credential written in any accepted form.

    kind: 'b64' (explicit prefix), 'auto' (recognised as base64), 'plain' (forced
    literal) or None (used exactly as given).

    Quotes are removed FIRST. Strip them after the decode and `"b64:AAAA"` (the quotes
    compose keeps literally) fails the prefix test, is handed on as the literal password
    `b64:AAAA`, and the portal rejects a credential that was correct in the file.
    """
    if raw is None:
        return None, None, None
    stripped = _unquote_secret(raw.strip(), name)

    if stripped.lower().startswith(PLAIN_PREFIX):
        return stripped[len(PLAIN_PREFIX):], None, "plain"

    if stripped.lower().startswith(B64_PREFIX):
        payload = re.sub(r"\s+", "", stripped[len(B64_PREFIX):])
        if not payload:
            return None, "is prefixed b64: but has no payload after it", None
        try:
            decoded = base64.b64decode(payload, validate=True).decode("utf-8")
        except Exception as e:
            return None, ("is prefixed b64: but does not decode (%s: %s) — re-encode with "
                          "printf '%%s' 'the-value' | base64 -w0"
                          % (type(e).__name__, e)), None
        if not decoded.strip():
            return None, "decodes to an empty value", None
        return decoded, None, "b64"

    auto = _looks_like_base64(stripped)
    if auto is not None:
        log.warning("the credential looks like base64 and was decoded automatically to "
                    "%d characters. If that is literally your password and not an encoding "
                    "of it, write it as plain:<value> to switch the decoding off.", len(auto))
        return auto, None, "auto"

    return raw, None, None

# Where each credential actually came from, for the startup log line.
_CREDENTIAL_SOURCE = {}

# Credentials that could not be decoded — reported by validate_config().
_CREDENTIAL_ERRORS = []


def _env_secret(name, default=None):
    """One variable, three accepted forms, no extra variables to remember.

    Docker Compose interpolates `$` in every value it reads, so a password
    containing `$` arrives shortened — or empty if it starts with one. Base64 has no
    `$` in its alphabet, so an encoded value survives untouched. Rather than expose
    separate `_B64` / `_FILE` variables for that, the same variable accepts:

      MyPassword123      used as written (fine when there is no `$`)
      b64:<base64>       decoded (explicit)
      <base64>           decoded automatically when it is unambiguously base64
      plain:<anything>   forced literal, for a password that merely looks like base64

    There is no `_FILE` route. It cost a mount and three more variable names to solve
    a problem base64 already solves inline, and two ways to say the same thing is how
    users end up setting both and wondering which one won.
    """
    raw = _env_text(name)
    if raw is None:
        return default

    decoded, problem, kind = _decode_prefixed(raw, name)
    if problem:
        log.warning("%s %s", name, problem)
        _CREDENTIAL_ERRORS.append(name)
        return default

    _CREDENTIAL_SOURCE[name] = {
        "b64": "b64", "auto": "b64 (auto-detected)", "plain": "plain (forced)",
    }.get(kind or "", "plain")

    # A short password's base64 is 12 characters, which is below the auto-detect floor and
    # would otherwise be submitted as literal text with no explanation. Say it out loud
    # rather than failing at the portal.
    compact = re.sub(r"\s+", "", raw)
    if (kind is None and len(compact) >= 12 and len(compact) % 4 == 0
            and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact)):
        log.warning("%s is used literally, but it is shaped like base64 (%d characters). "
                    "If you pasted an encoded value, write it as b64:%s to have it decoded.",
                    name, len(compact), "<value>")
    return decoded


def _unquote_secret(value, name):
    """Drop ONE matching pair of surrounding quotes, loudly.

    `- USMS_PASSWORD="***"` in a compose list is not YAML quoting: the quotes are
    part of the value, the container receives them, and the portal rejects the
    login with no useful error. A real password never starts and ends with the
    same quote character, so stripping the pair is safe — but a warning is logged
    so the file gets fixed rather than silently compensated for.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        log.warning("%s is wrapped in %s quotes — those are literal inside a compose "
                    "environment entry, so the portal would have seen them too. "
                    "Using the value without them.", name, value[0])
        return value[1:-1]
    return value


def _env_int(name, default):
    """Env value as int, or the _INVALID sentinel — never raises."""
    raw = _env_text(name)
    if raw is None:
        return default
    # int() also accepts '1_800' and non-ASCII digits, which hides a typo instead of
    # reporting one. Accept only an optional sign and ASCII digits.
    if not re.fullmatch(r"-?\d+", raw.strip()):
        return _INVALID
    return int(raw.strip())


usms_username = _env_secret("USMS_USERNAME") or ""
usms_password = _env_secret("USMS_PASSWORD") or ""
selenium_host = _env_text("SELENIUM_HOST", "localhost")
selenium_port = _env_int("SELENIUM_PORT", 4444)

mqtt_broker = _env_text("MQTT_BROKER")
mqtt_port = _env_int("MQTT_PORT", 1883)
# Same handling as the portal credential: compose interpolates '$' in every value, so a
# broker password containing one arrives truncated and the broker refuses the CONNACK.
mqtt_username = _unquote_secret(_env_text("MQTT_USERNAME") or "", "MQTT_USERNAME")
mqtt_password = _unquote_secret(_env_text("MQTT_PASSWORD") or "", "MQTT_PASSWORD")

scrape_interval = _env_int("SCRAPE_INTERVAL", 1800)

# A broker URL is the only thing MQTT needs: anonymous brokers are legitimate.
mqtt_enabled = bool(mqtt_broker) and mqtt is not None

# MQTT topics
topic_lastrun = "home/usms/last_run"
topic_status = "home/usms/status"

# Dashboard page and XPaths for the data table rows
DASHBOARD_URL = "https://www.usms.com.bn/SmartMeter/Home"
LOGIN_URL = "https://www.usms.com.bn/SmartMeter/ResLogin"

# XPaths for the three data cells in the dashboard table
XPATH_UNIT = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[9]/td/table/tbody/tr/td[2]"
XPATH_BALANCE = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[10]/td/table/tbody/tr/td[2]"
XPATH_POLLED = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div/table/tbody/tr[11]/td/table/tbody/tr/td[2]"
# The outer container div (used for login detection and as anchor for waiting)
XPATH_DASHBOARD_DIV = "/html/body/form/div[5]/table/tbody/tr/td/table[1]/tbody/tr/td[1]/div"

# --- Operational limits -----------------------------------------------------
MIN_SCRAPE_INTERVAL = 60      # seconds; below this the portal is being hammered
MAX_CONSECUTIVE_FAILURES = 3  # then exit(1) and let the restart policy recycle us
PAGE_LOAD_TIMEOUT = 45        # seconds per navigation (chromedriver defaults to 300)
HTTP_TIMEOUT = 60             # seconds for any WebDriver command (default is NO limit)
ELEMENT_TIMEOUT = 10          # seconds per dashboard cell
SCRAPE_RETRY_SLEEP = 5        # seconds before the single retry pass
SELENIUM_ATTEMPTS = 5
SELENIUM_RETRY_SLEEP = 3
CAPTCHA_REFRESH_TIMEOUT = 5.0  # seconds to wait for a re-rolled captcha image


# ---------------------------------------------------------------------------
# Timezone
#
# The portal's timestamps are local (Asia/Brunei, UTC+8). ZoneInfo is used when
# the platform has tzdata; the fixed offset is the documented fallback so a slim
# image without tzdata still stamps the same wall-clock time.
# ---------------------------------------------------------------------------

USMS_TIMEZONE = _env_text("USMS_TIMEZONE", "Asia/Brunei")


def local_timezone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(USMS_TIMEZONE)
        except Exception as e:
            log.warning("USMS_TIMEZONE=%r is unknown to this platform (%s) — "
                        "falling back to UTC+8.", USMS_TIMEZONE, e)
    return timezone(timedelta(hours=8))


def now_stamp():
    return datetime.now(local_timezone()).strftime("%Y-%m-%d %H:%M:%S")


# The portal's username field takes the IC number; accounts registered with an
# email address exist too. Anything else is a typo worth flagging before it becomes
# a failed login.
_USERNAME_IC = re.compile(r"^\d{6,12}$")
_USERNAME_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def username_shape(value):
    """('ic' | 'email' | 'unknown' | 'missing', human hint)."""
    if not value:
        return "missing", ""
    if _USERNAME_IC.match(value):
        return "ic", ""
    if _USERNAME_EMAIL.match(value):
        return "email", ""
    return "unknown", ("USMS_USERNAME=%s is neither a plain IC number (digits) nor an "
                       "email address — the portal will reject it. IC numbers are entered "
                       "without dashes or spaces." % _mask_digits(value))


def validate_config():
    """Fail fast, in one log line per problem, when the environment is unusable."""
    problems = []

    if scrape_interval is _INVALID:
        problems.append("SCRAPE_INTERVAL must be a whole number of seconds, got %r"
                        % os.getenv("SCRAPE_INTERVAL"))
    elif scrape_interval < MIN_SCRAPE_INTERVAL:
        problems.append("SCRAPE_INTERVAL=%ds is below the %ds floor — refusing to "
                        "hammer the portal" % (scrape_interval, MIN_SCRAPE_INTERVAL))

    ports = [("SELENIUM_PORT", selenium_port)]
    if mqtt_broker:
        # A junk MQTT_PORT in a stack that never publishes used to exit(2) — a restart
        # loop for a variable nothing reads.
        ports.append(("MQTT_PORT", mqtt_port))
    for label, value in ports:
        if value is _INVALID:
            problems.append("%s must be a whole number, got %r" % (label, os.getenv(label)))
        elif not 0 < value < 65536:
            problems.append("%s=%s is not a valid TCP port" % (label, value))

    if not usms_username and "USMS_USERNAME" not in _CREDENTIAL_ERRORS:
        problems.append("USMS_USERNAME is empty — set it in the stack's environment")
    if not usms_password and "USMS_PASSWORD" not in _CREDENTIAL_ERRORS:
        problems.append("USMS_PASSWORD is empty — set it in the stack's environment")
    for name in _CREDENTIAL_ERRORS:
        problems.append("%s could not be decoded — re-encode it: "
                        "printf '%%s' 'the-value' | base64 -w0" % name)

    # A `$` in an environment-supplied password is the single most common cause
    # of "Incorrect user name and password" on a password that works in a
    # browser: Compose substituted it away before we ever saw it. We cannot tell
    # a truncated password from a genuine one, so say the whole story once.
    if (usms_password and "$" in usms_password
            and not str(_CREDENTIAL_SOURCE.get("USMS_PASSWORD", "")).startswith("b64")):
        log.warning("USMS_PASSWORD contains '$' — Docker Compose interpolates that, so a "
                    "password like MyP@ss$w0rd reaches this container as MyP@ss and the "
                    "portal rejects it. Escape it as '$$', or paste the base64 of the "
                    "password instead — both are accepted in this same variable.")

    if mqtt_password and "$" in mqtt_password:
        log.warning("MQTT_PASSWORD contains '$' — Docker Compose interpolates that, so the "
                    "broker receives a shortened password and will refuse the connection. "
                    "Escape it as '$$', or paste its base64 instead (both accepted here).")

    if mqtt_broker and mqtt is None:
        problems.append("MQTT_BROKER is set but paho-mqtt is not installed")

    if problems:
        for problem in problems:
            log.critical("config: %s", problem)
        log.critical("Refusing to start with an unusable configuration.")
        sys.exit(2)


def mqtt_disabled_reason():
    if mqtt is None:
        return "paho-mqtt not installed"
    if not mqtt_broker:
        return "MQTT_BROKER not set"
    return ""


def log_startup_config():
    """Echo the RESOLVED configuration — never the credential values.

    The password LENGTH is printed (never the value): Compose silently truncates a
    password at the first `$`, and the only tell is that a 12-character password
    arrives as 5 characters. Seeing `(11 chars)` next to a password known to be
    longer is the fastest diagnosis there is.
    """
    src_user = _CREDENTIAL_SOURCE.get("USMS_USERNAME", "unset")
    src_pass = _CREDENTIAL_SOURCE.get("USMS_PASSWORD", "unset")
    shape, shape_hint = username_shape(usms_username)
    if shape_hint:
        log.warning("%s", shape_hint)
    log.info(
        "config: interval=%ds selenium=%s:%s mqtt=%s (%s) credentials "
        "username=%s (%s) password=%s log_level=%s timezone=%s ort_threads=%d",
        scrape_interval, selenium_host, selenium_port,
        "on" if mqtt_enabled else "off",
        f"{mqtt_broker}:{mqtt_port}" if mqtt_broker else mqtt_disabled_reason(),
        "set" if usms_username else "MISSING", shape,
        "set (%d chars, %s)" % (len(usms_password), src_pass) if usms_password else "MISSING",
        LOG_LEVEL, USMS_TIMEZONE,
        max(1, min(ORT_MAX_THREADS, _available_cpus())),
    )


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

# Per-cycle counters, so the summary line can report them.
_cycle_state = {"captcha_rolls": 0}

# ---------------------------------------------------------------------------
# ONNX Runtime thread affinity
#
# ddddocr runs ONNX Runtime (ORT) sessions under the hood. Unless an explicit
# thread count is supplied, ORT pins each worker thread with sched_setaffinity()
# using CPU ids taken from the HOST topology. Inside anything with a restricted
# or scattered cpuset — an LXC coreset, `docker --cpuset-cpus`, a Kubernetes
# cpuset, WSL2 — those ids are not permitted, the syscall fails with EINVAL and
# ORT prints one "pthread_setaffinity_np failed … error code: 22" line per
# thread on every session creation. Supplying the thread counts is ORT's own
# documented remedy: it then skips affinity entirely.
#
# The numbers are chosen to be safe on any machine: the job is a ~6 ms
# inference on a 200x80 captcha, so more than a handful of threads buys nothing,
# and the count respects the cpuset we are actually allowed to run on.
# ---------------------------------------------------------------------------

ORT_MAX_THREADS = 4


def _available_cpus():
    """CPUs this process may run on — cgroup/cpuset/taskset aware."""
    try:
        return max(1, len(os.sched_getaffinity(0)))   # Linux
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)            # macOS / Windows fallback


def _harden_onnxruntime():
    """Give ORT explicit thread counts so it never sets CPU affinity.

    Idempotent and best-effort: if ONNX Runtime is missing or a future version
    changes this API, the captcha reader still works exactly as before — the
    only loss is the stderr noise this exists to remove.
    """
    try:
        import onnxruntime as ort
    except Exception:
        return

    if getattr(ort, "_usms_affinity_hardened", False):
        return

    workers = max(1, min(ORT_MAX_THREADS, _available_cpus()))
    original_session = ort.InferenceSession

    def _apply(options):
        try:
            options.intra_op_num_threads = workers
            options.inter_op_num_threads = 1
        except Exception:
            pass
        return options

    def hardened_session(*args, **kwargs):
        untouched_args, untouched_kwargs = args, dict(kwargs)
        options = kwargs.get("sess_options")
        positional = len(args) > 1
        if options is None and positional and isinstance(args[1], ort.SessionOptions):
            options = args[1]
        # Only add or mutate options when that slot is free or already options-shaped.
        # An argument we do not recognise is passed straight through untouched.
        if options is None and positional and args[1] is not None:
            return original_session(*args, **kwargs)
        options = _apply(options if options is not None else ort.SessionOptions())
        if positional:
            args = (args[0], options) + args[2:]   # replace the positional slot
        else:
            kwargs["sess_options"] = options
        try:
            return original_session(*args, **kwargs)
        except TypeError:
            # A future ONNX Runtime that rejects the options we inject must never
            # cost us captcha solving: retry the caller's exact call instead.
            return original_session(*untouched_args, **untouched_kwargs)

    ort.InferenceSession = hardened_session  # type: ignore[assignment]
    setattr(ort, "_usms_affinity_hardened", True)
    log.debug("ONNX Runtime: %d intra-op / 1 inter-op thread, CPU affinity off.", workers)


def _engines():
    """Load ddddocr lazily so a model problem can never stop the container booting."""
    global _captcha_engines
    if _captcha_engines is None:
        _harden_onnxruntime()
        _captcha_engines = (
            ddddocr.DdddOcr(show_ad=False),
            ddddocr.DdddOcr(show_ad=False, beta=True),
        )
    return _captcha_engines


def _read_captcha_variants(png_bytes, cycle_id="none"):
    """Five independent readings of the same captcha image.

    Every variant logs its answer and its inference time at DEBUG, so a slow host
    or a model that is starting to disagree is visible without guessing.
    """
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

    log.debug("cycle=%s captcha image %dx%d, %d bytes from the fetch",
              cycle_id, rgb.width, rgb.height, len(png_bytes))

    readings = {}
    for variant, engine, payload in (
        ("raw", eng_old, raw),
        ("x2", eng_old, x2),
        ("x2bin", eng_old, x2bin),
        ("beta_raw", eng_beta, raw),
        ("beta_x2bin", eng_beta, x2bin),
    ):
        started = time.monotonic()
        readings[variant] = engine.classification(payload).strip().lower()
        log.debug("cycle=%s captcha %s -> %r in %.1fms",
                  cycle_id, variant, readings[variant], (time.monotonic() - started) * 1000)
    return readings


def _valid_code(code):
    return len(code) == CAPTCHA_CODE_LENGTH and all(
        c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in code)


# The captcha image URL 404s once the browser has rendered it, so pull the bytes
# back out of Chrome's HTTP cache instead of asking the server again.
STEAL_CAPTCHA_JS = """
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

CAPTCHA_IMG_ID = "ASPxRoundPanel1_ASPxCaptcha1_IMG"
CAPTCHA_REFRESH_SELECTOR = "[id$='ASPxCaptcha1_RB']"


def _captcha_src(driver):
    """Current captcha image URL, or None. The URL changes on every re-roll."""
    try:
        return driver.find_element(By.ID, CAPTCHA_IMG_ID).get_attribute("src")
    except Exception:
        return None


def _captcha_png(driver, attempt=0):
    """Captcha bytes out of Chrome's HTTP cache, or None.

    The page script has three failure shapes: a 2-element callback
    ([src, 'reason']), a null src, or a non-PNG payload. All of them must be
    survivable — a rejected captcha fetch once cost the whole login because the
    tuple was indexed blind.
    """
    try:
        res = driver.execute_async_script(STEAL_CAPTCHA_JS)
    except Exception as e:
        log.warning("Captcha attempt %d: cache read failed: %s: %s",
                    attempt, type(e).__name__, e)
        return None

    # A JS callback should be a tuple, but nothing guarantees the shape: index
    # only after proving it is a sequence of at least three entries.
    if (not isinstance(res, (list, tuple)) or len(res) < 3
            or not res[0] or not isinstance(res[2], str)):
        detail = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else res
        log.warning("Captcha attempt %d: image unavailable (%s)", attempt, detail)
        return None

    try:
        data = base64.b64decode(res[2])
    except Exception as e:
        log.warning("Captcha attempt %d: payload was not base64 (%s)", attempt, e)
        return None

    if not data.startswith(b"\x89PNG"):
        log.warning("Captcha attempt %d: payload was not a PNG (%d bytes)", attempt, len(data))
        return None
    return data


def _refresh_captcha(driver, previous_src):
    """Click "Show another code" and wait for the image URL to actually change.

    Without the src comparison the loop could re-read the same cached image ten
    times (refresh control missing) or read it before DevExpress swapped it
    (fixed sleep). Both looked like "the OCR is bad" in the logs.
    """
    refresh = driver.find_elements(By.CSS_SELECTOR, CAPTCHA_REFRESH_SELECTOR)
    if not refresh:
        log.warning("Captcha refresh control %s not found — a re-roll cannot change "
                    "the image, stopping here.", CAPTCHA_REFRESH_SELECTOR)
        return False

    driver.execute_script("arguments[0].click();", refresh[0])

    if not previous_src:
        # No previous src means the image element could not be read at all. Sleeping and
        # claiming success re-rolled the captcha ten times against the portal instead of
        # stopping, and the log blamed the OCR.
        time.sleep(1)
        if _captcha_src(driver):
            return True
        log.warning("Captcha image is still unreadable after the refresh click — stopping "
                    "instead of re-rolling blind.")
        return False

    deadline = time.monotonic() + CAPTCHA_REFRESH_TIMEOUT
    while time.monotonic() < deadline:
        if _captcha_src(driver) != previous_src:
            return True
        time.sleep(0.2)

    log.warning("Captcha image did not change within %.1fs of the refresh click",
                CAPTCHA_REFRESH_TIMEOUT)
    return False


def solve_captcha(driver, cycle_id="none"):
    """Read the stage-2 captcha, re-rolling until the OCR models agree."""
    for attempt in range(1, CAPTCHA_MAX_ROLLS + 1):
        _cycle_state["captcha_rolls"] = attempt
        src_before = _captcha_src(driver)
        log.debug("cycle=%s captcha roll %d/%d: image src=%s",
                  cycle_id, attempt, CAPTCHA_MAX_ROLLS, src_before)
        png = _captcha_png(driver, attempt)
        if png:
            try:
                readings = _read_captcha_variants(png, cycle_id)
            except Exception as e:
                log.warning("cycle=%s captcha attempt %d: OCR failed: %s: %s",
                            cycle_id, attempt, type(e).__name__, e)
                readings = {}
            if readings:
                code, votes = Counter(readings.values()).most_common(1)[0]
                log.info("cycle=%s captcha attempt %d/%d: %r (%d/5 models agree) %s",
                         cycle_id, attempt, CAPTCHA_MAX_ROLLS, code, votes, json.dumps(readings))
                if _valid_code(code) and votes >= CAPTCHA_MIN_VOTES:
                    log.debug("cycle=%s captcha accepted on roll %d: %r (%d votes, %d chars)",
                              cycle_id, attempt, code, votes, len(code))
                    return code
                reasons = []
                if not _valid_code(code):
                    reasons.append(f"shape: {len(code)} chars, expected {CAPTCHA_CODE_LENGTH}")
                if votes < CAPTCHA_MIN_VOTES:
                    reasons.append(f"agreement {votes} < {CAPTCHA_MIN_VOTES}")
                log.debug("cycle=%s captcha roll %d rejected (%s) — re-rolling",
                          cycle_id, attempt, "; ".join(reasons))

        # Disagreement or unreadable: ask for a new challenge instead of guessing.
        if attempt == CAPTCHA_MAX_ROLLS:
            break
        if not _refresh_captcha(driver, src_before):
            log.warning("cycle=%s captcha re-roll unavailable after attempt %d — stopping early.",
                        cycle_id, attempt)
            break

    log.warning("cycle=%s gave up reading the captcha after %d attempts.",
                cycle_id, _cycle_state["captcha_rolls"])
    return None


# ---------------------------------------------------------------------------
# WebDriver
# ---------------------------------------------------------------------------

# Configure Selenium options — fresh options each session to avoid shared state
def make_chrome_options():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return opts


def create_driver():
    """Create a new remote WebDriver session, retrying up to SELENIUM_ATTEMPTS.

    The catch is deliberately broad: a refused connection to the Selenium
    container raises urllib3 MaxRetryError/NewConnectionError, which is NOT a
    WebDriverException. Catching only WebDriverException made the retry loop dead
    code in exactly the case it exists for (Selenium down).
    """
    last_exc = None
    for attempt in range(1, SELENIUM_ATTEMPTS + 1):
        driver = None
        try:
            driver = webdriver.Remote(
                command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
                options=make_chrome_options(),
            )
            driver.set_script_timeout(30)               # needed by the captcha cache read
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            # The page-load timeout covers navigation only. Without this the HTTP client
            # has no timeout at all, so one wedged chromedriver makes find_element/send_keys
            # block forever: no log line, no failure counted, no auto-heal, no restart.
            try:
                driver.command_executor.set_timeout(HTTP_TIMEOUT)
            except Exception as e:
                log.debug("could not set the WebDriver HTTP timeout: %s", e)
            log.info("WebDriver session ready (attempt %d/%d)", attempt, SELENIUM_ATTEMPTS)
            log.debug("WebDriver session %s: page-load timeout %ss, script timeout 30s, "
                      "headless, session id %s", attempt, PAGE_LOAD_TIMEOUT,
                      getattr(driver, "session_id", "?"))
            return driver
        except Exception as e:
            last_exc = e
            # A session that was created but never configured still holds a Chrome in
            # the Selenium container; leaking it per attempt exhausts SE_NODE_MAX_SESSIONS
            # and the next start fails for a reason that has nothing to do with the host.
            if driver is not None:
                try:
                    driver.quit()
                    log.debug("Selenium attempt %d: half-built session released.", attempt)
                except Exception as cleanup_error:
                    log.debug("Selenium attempt %d: releasing the session failed: %s",
                              attempt, cleanup_error)
            log.warning("Selenium not ready yet (attempt %d/%d): %s: %s",
                        attempt, SELENIUM_ATTEMPTS, type(e).__name__, e)
            if attempt < SELENIUM_ATTEMPTS:
                time.sleep(SELENIUM_RETRY_SLEEP)
    raise RuntimeError(
        f"Selenium host not reachable after {SELENIUM_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}")


def _navigate(driver, url, what, cycle_id="none"):
    """driver.get() with the page-load timeout turned into a log line, not a hang."""
    started = time.monotonic()
    try:
        driver.get(url)
    except TimeoutException:
        log.warning("cycle=%s %s did not finish loading within %ss — using whatever "
                    "rendered (%.1fs).", cycle_id, what, PAGE_LOAD_TIMEOUT,
                    time.monotonic() - started)
        return False
    try:
        log.debug("cycle=%s %s loaded in %.1fs: title=%r url=%s",
                  cycle_id, what, time.monotonic() - started, driver.title, driver.current_url)
    except Exception:
        log.debug("cycle=%s %s loaded in %.1fs",
                  cycle_id, what, time.monotonic() - started)
    return True


_DIGITS = re.compile(r"\d")


def _mask_digits(text):
    """Blank out digit runs — the login body carries the account holder's IC number."""
    return _DIGITS.sub("#", text or "")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "page").lower()).strip("-")[:40] or "page"


PAGE_DUMP_KEEP = 5


def _rotate_page_dumps(directory="/tmp"):
    """Keep at most PAGE_DUMP_KEEP dumps.

    A crash loop writes one full DOM per cycle and nothing ever deleted them; a full /tmp
    then stops Chrome starting, and every cycle fails for a reason unrelated to the portal.
    """
    try:
        dumps = sorted(glob.glob(os.path.join(directory, "usms-fail-*.html")),
                       key=os.path.getmtime, reverse=True)
        for stale in dumps[PAGE_DUMP_KEEP:]:
            try:
                os.unlink(stale)
            except OSError:
                pass
    except Exception as e:
        log.debug("page-dump rotation skipped: %s", e)


def log_page_source(driver, label="", cycle_id="none"):
    """Log what USMS actually returned and keep the full DOM for a layout change.

    The 500-char preview is enough to spot a redirect, not enough to fix a
    shifted table; the whole page is written next to it. Digits are masked in the
    log line only — the file keeps real values so XPath work stays possible.
    """
    try:
        title = driver.title
        url = driver.current_url
    except Exception as e:
        log.warning("cycle=%s %s — could not read title/url: %s", cycle_id, label, e)
        return

    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
        log.warning("cycle=%s %s — title: %s | url: %s | body (digits masked): %s",
                    cycle_id, label or "Page state", title, url, _mask_digits(body[:500]))
    except Exception as e:
        log.warning("cycle=%s %s — could not read the body: %s", cycle_id, label, e)

    _rotate_page_dumps()
    try:
        path = f"/tmp/usms-fail-{cycle_id}-{_slug(label)}.html"
        html = driver.page_source or ""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        log.warning("cycle=%s %s — full page saved to %s (%d bytes)",
                    cycle_id, label, path, len(html))
    except Exception as e:
        log.warning("cycle=%s %s — could not save the page source: %s", cycle_id, label, e)


def is_logged_in(driver, cycle_id="none"):
    """True when the dashboard is reachable without a login.

    A WebDriver-level failure is NOT "not logged in" — it propagates, so the cycle
    handler logs a traceback instead of quietly attempting a login on a dead
    session.
    """
    _navigate(driver, DASHBOARD_URL, "dashboard (session probe)", cycle_id)
    try:
        WebDriverWait(driver, ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, XPATH_DASHBOARD_DIV)))
        log.info("cycle=%s session still valid — already logged in.", cycle_id)
        return True
    except TimeoutException:
        log.info("cycle=%s dashboard not present — needs a login.", cycle_id)
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


def _portal_rejected_credentials(driver):
    """True when the page we are looking at is the portal's 'wrong credentials' state.

    Verified against the live portal: a fresh login page contains no such text, and
    after a rejected submit the visible body becomes
    '… | Incorrect user name and password. | Ok'. So seeing it means the SERVER said
    the username/password are wrong — not a timeout, not a captcha problem.
    """
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        return False
    return "incorrect user name" in body or "incorrect username" in body


def login(driver, cycle_id="none"):
    """Two-stage login.

    Stage 1 is the plain form (IC + password) — it carries no captcha for a human.
    Submitting it returns ResLogin?sc=…, a second form that asks for the IC and
    password AGAIN plus a captcha. The captcha is solved by OCR (see above) and the
    second form is submitted, which lands on /MainPage.
    """
    log.info("cycle=%s logging in…", cycle_id)
    _navigate(driver, LOGIN_URL, "login page", cycle_id)

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "ASPxRoundPanel1_txtUsername_I")))
    except TimeoutException:
        # This was the one failure path that produced no page dump at all.
        log.warning("cycle=%s stage-1 login form never appeared — dumping page state.",
                    cycle_id)
        log_page_source(driver, "Stage 1 form missing", cycle_id)
        return False

    try:
        _type(driver, "#ASPxRoundPanel1_txtUsername_I", usms_username)
        _type(driver, "#ASPxRoundPanel1_txtPassword_I", usms_password)
        log.debug("cycle=%s stage-1 credentials filled (%d-char username, %d-char password)",
                  cycle_id, len(usms_username or ""), len(usms_password or ""))
        _click(driver, "#ASPxRoundPanel1_btnLogin")
    except WebDriverException as e:
        # A renamed field is a layout change: the dump is the only thing that fixes it, and
        # this path used to raise a bare traceback instead of producing one.
        log.warning("cycle=%s the stage-1 form changed — a field or button is gone: %s: %s",
                    cycle_id, type(e).__name__, e)
        log_page_source(driver, "Stage 1 form changed", cycle_id)
        return False
    log.debug("cycle=%s stage-1 submitted, waiting for the redirect", cycle_id)

    # Either the captcha stage shows up (normal) or we went straight in.
    try:
        WebDriverWait(driver, 30).until(
            lambda d: "sc=" in d.current_url or "ResLogin" not in d.current_url)
    except TimeoutException:
        if _portal_rejected_credentials(driver):
            log_page_source(driver, "Portal rejected the credentials", cycle_id)
            raise CredentialsRejected("stage 1: the portal answered 'Incorrect user "
                                      "name and password'")
        log.warning("cycle=%s no response to the stage-1 submit — dumping page state.",
                    cycle_id)
        log_page_source(driver, "After stage 1 timeout", cycle_id)
        return False

    if "sc=" not in driver.current_url:
        # No captcha stage — but do not take the URL as proof: the portal can answer the
        # submit with a page that still is not the dashboard. Verify, or say so.
        try:
            WebDriverWait(driver, ELEMENT_TIMEOUT).until(
                EC.presence_of_element_located((By.XPATH, XPATH_DASHBOARD_DIV)))
        except Exception:
            log.warning("cycle=%s stage 1 left the captcha path but the dashboard is not "
                        "present — treating the login as failed.", cycle_id)
            log_page_source(driver, "No dashboard after stage 1", cycle_id)
            return False
        log.info("cycle=%s login succeeded without a captcha stage — landed on %s",
                 cycle_id, driver.current_url)
        return True

    log.info("cycle=%s stage 1 accepted; solving the stage-2 captcha on %s",
             cycle_id, driver.current_url)
    code = solve_captcha(driver, cycle_id)
    if not code:
        log_page_source(driver, "Captcha unreadable", cycle_id)
        return False

    # Fill stage 2: same credentials again, plus the solved captcha.
    tb = driver.find_elements(By.CSS_SELECTOR, "[name$='ASPxCaptcha1$TB']")
    if not tb:
        log.warning("cycle=%s stage-2 captcha input field not found.", cycle_id)
        log_page_source(driver, "Stage 2 captcha input missing", cycle_id)
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
        # The portal can reject the SECOND submit too. Reporting that as a transient
        # failure makes main() retry the whole login, burning the attempt budget that
        # CredentialsRejected and the parking path exist to protect.
        if _portal_rejected_credentials(driver):
            log_page_source(driver, "Portal rejected the credentials (stage 2)", cycle_id)
            raise CredentialsRejected("stage 2: the portal answered 'Incorrect user "
                                      "name and password'")
        log.warning("cycle=%s dashboard did not appear after the stage-2 submit — "
                    "dumping page state.", cycle_id)
        log_page_source(driver, "After stage 2 timeout", cycle_id)
        return False

    log.info("cycle=%s login successful — dashboard loaded at %s", cycle_id, driver.current_url)
    return True


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def safe_get_text(driver, xpath, timeout=ELEMENT_TIMEOUT, cycle_id="none", field="cell"):
    """Text of one element, or None.

    Waits for NON-EMPTY text, not merely for the element to exist: a present but
    empty cell used to return '' which every N/A guard then let through.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_error = None
    reads = 0
    while True:
        reads += 1
        try:
            text = (driver.find_element(By.XPATH, xpath).text or "").strip()
            if text:
                log.debug("cycle=%s %s read %r after %.0fms (%d read%s)",
                          cycle_id, field, text, (time.monotonic() - started) * 1000,
                          reads, "" if reads == 1 else "s")
                return text
        except Exception as e:
            # A dead session or a broken XPath is not "the cell is empty": propagating it
            # lets the cycle fail loudly instead of burning the full timeout per field.
            if type(e).__name__ in ("InvalidSessionIdException", "InvalidSelectorException",
                                    "NoSuchWindowException", "InvalidArgumentException"):
                raise
            last_error = e
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    if last_error is not None:
        log.warning("cycle=%s %s: no text within %ss (%d reads, last error: %s: %s)",
                    cycle_id, field, timeout, reads,
                    type(last_error).__name__, last_error)
    else:
        log.warning("cycle=%s %s: element stayed empty for %ss (%d reads)",
                    cycle_id, field, timeout, reads)
    return None


# Value shapes. The dashboard cells are read by absolute XPath, so inserting one
# table row resolves every offset to the WRONG cell and would publish it happily
# forever. A shape check turns that silent corruption into a warning.
#
# The unit is deliberately not tied to kWh: an account can hold more than one
# meter (electricity in kWh, water in m³), and refusing anything but kWh threw
# away every water reading as "not a kWh reading".
_FIELD_SHAPES = {
    "unit": (re.compile(r"^\d[\d,]*(?:\.\d+)?\s*(?:kWh|MWh|Wh|m\u00b3|m3|L|litres?|liters?|units?)$", re.I),
             "a meter reading with a unit, e.g. 1991.264 kWh or 62.320 m\u00b3"),
    # A negative (arrears) or parenthesised balance is a real reading here; it used to
    # be discarded as unreadable, freezing the sensor exactly when the account was in debit.
    "balance": (re.compile(r"^(?:RM|BND|\$)?\s*(?:\(\s*-?\d[\d,]*(?:\.\d+)?\s*\)"
                           r"|-?\d[\d,]*(?:\.\d+)?)$", re.I),
                "an amount, e.g. $67.16 or -12.30"),
    "polled": (re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"), "a date, e.g. 24/11/2026"),
}

# Unit suffix -> what kind of meter it is. Used only to name MQTT topics.
_UNIT_KINDS = (
    (re.compile(r"kwh$", re.I), "electricity"),
    (re.compile(r"mwh$", re.I), "electricity"),
    (re.compile(r"wh$", re.I), "electricity"),        # sub-kWh reading, still electricity
    (re.compile(r"(?:m\u00b3|m3)$", re.I), "water"),
    (re.compile(r"(?:l|litres?|liters?)$", re.I), "water"),
)


def unit_kind(reading):
    """'electricity' / 'water' from a validated reading such as '62.320 m³'."""
    if not reading:
        return "meter"
    tail = reading.strip().rsplit(" ", 1)[-1]
    for pattern, kind in _UNIT_KINDS:
        if pattern.search(tail):
            return kind
    return "meter"


def validate_value(field, value, cycle_id="none"):
    """value when it matches the field's shape, otherwise None (treated as N/A)."""
    if value is None:
        return None
    value = value.strip()          # a stray space must not fail the shape check
    pattern, hint = _FIELD_SHAPES[field]
    if pattern.match(value):
        log.debug("cycle=%s %s=%r passed the %s shape check", cycle_id, field, value, field)
        return value
    log.warning("cycle=%s %s = %r does not look like %s — treating this field as unavailable.",
                cycle_id, field, value, hint)
    return None


# --- Meter discovery --------------------------------------------------------
#
# The dashboard renders ONE CARD PER METER (a DevExpress ASPxCardView), side by
# side, each with the same label/value row pairs: Meter No, Full Name, Meter
# Status, Address, ..., Remaining Unit, Remaining Balance, Last Updated.
#
# The old code read three fixed ABSOLUTE XPaths, which can only ever address the
# FIRST card — so on an account holding both an electricity and a water meter
# every water reading was invisible, and the water card's "62.320 m³" would have
# failed the kWh shape check even if it had been read.
#
# Locating the label cells BY THEIR TEXT finds every card and stops caring about
# row indexes, which is what broke whenever the portal inserted a row.
METER_LABELS = (
    ("meter_no", "Meter No:"),
    ("unit", "Remaining Unit:"),
    ("balance", "Remaining Balance:"),
    ("polled", "Last Updated:"),
)


def _xpath_literal(text):
    """Quote text for an XPath expression, whatever quotes it contains."""
    if "'" not in text:
        return "'%s'" % text
    if '"' not in text:
        return '"%s"' % text
    parts = ["'%s'" % chunk for chunk in text.split("'")]
    return "concat(%s)" % ", \"'\", ".join(parts)


def _sibling_value(cell):
    """The value cell that sits next to a label cell, or None."""
    try:
        text = (cell.find_element(By.XPATH, "./following-sibling::td[1]").text or "").strip()
    except WebDriverException:
        return None
    return text or None


def _card_for(cell, labels, max_levels=8):
    """The smallest ancestor of `cell` that holds two or more meter labels.

    That ancestor is one meter card. Used only when the label lists have uneven
    lengths, where pairing them by index would attach one card's values to another
    card's meter number — silently, and forever.
    """
    node = cell
    for _ in range(max_levels):
        try:
            node = node.find_element(By.XPATH, "..")
        except WebDriverException:
            return None
        found = 0
        for label in labels:
            try:
                if node.find_elements(By.XPATH, ".//td[normalize-space(.)=%s]"
                                      % _xpath_literal(label)):
                    found += 1
            except WebDriverException:
                return None
            if found >= 2:
                return node
    return None


def _card_meter(card, cycle_id):
    """Read one card's fields, each from the FIRST matching label INSIDE that card."""
    meter = {}
    for field, label in METER_LABELS:
        try:
            cells = card.find_elements(By.XPATH, ".//td[normalize-space(.)=%s]"
                                       % _xpath_literal(label))
        except WebDriverException as e:
            log.warning("cycle=%s could not read '%s' inside a card: %s", cycle_id, label, e)
            cells = []
        raw = _sibling_value(cells[0]) if cells else None
        meter[field] = raw if field == "meter_no" else validate_value(field, raw, cycle_id)
    meter["kind"] = unit_kind(meter.get("unit"))
    return meter


def read_meters(driver, timeout=ELEMENT_TIMEOUT, cycle_id="none"):
    """Every meter card on the dashboard, in page order, with values validated."""
    started = time.monotonic()
    # The cards are rendered by an ASP.NET postback; enumerating the instant the page
    # "loads" can capture one card of two, and a partial set looks like a complete scrape.
    deadline = time.monotonic() + timeout
    while True:
        try:
            if driver.find_elements(By.XPATH,
                                    "//td[normalize-space(.)=%s]"
                                    % _xpath_literal(dict(METER_LABELS)["unit"])):
                break
        except WebDriverException:
            pass
        if time.monotonic() >= deadline:
            log.warning("cycle=%s no meter card appeared within %ss.", cycle_id, timeout)
            return []
        time.sleep(0.5)

    columns = {}
    for field, label in METER_LABELS:
        # normalize-space(.) not text(): the caption sits in a <span> inside the
        # cell, so the cell has no direct text node of its own and
        # normalize-space(text()) matches nothing at all.
        xpath = "//td[normalize-space(.)=%s]" % _xpath_literal(label)
        try:
            columns[field] = driver.find_elements(By.XPATH, xpath)
        except WebDriverException as e:
            log.warning("cycle=%s could not enumerate '%s' cells: %s", cycle_id, label, e)
            columns[field] = []

    count = len(columns["unit"])
    if count == 0:
        return []

    counts = {field: len(cells) for field, cells in columns.items()}
    meters = []

    if len(set(counts.values())) == 1:
        # Fast path — every label appears exactly once per card, so index N of one list
        # is the same card as index N of every other. This is the normal case.
        for index in range(count):
            meter = {}
            for field, _label in METER_LABELS:
                raw = _sibling_value(columns[field][index])
                meter[field] = raw if field == "meter_no" else validate_value(field, raw, cycle_id)
            meter["kind"] = unit_kind(meter.get("unit"))
            meters.append(meter)
    elif len([m for m in columns["meter_no"]]) and len(set(
            (_sibling_value(c) or "").strip() for c in columns["meter_no"])) < len(columns["meter_no"]):
        # Same counts, but two cards report the same meter number — the signature of a
        # stray label outside a card shifting every list by one. Card scoping is the only
        # safe reading here.
        log.warning("cycle=%s two cards report the same meter number — reading each card "
                    "in its own scope rather than pairing the lists.", cycle_id)
        labels = [label for _f, label in METER_LABELS]
        seen = set()
        for cell in columns["unit"]:
            card = _card_for(cell, labels)
            if card is None:
                continue
            key = getattr(card, "id", None) or id(card)
            if key in seen:
                continue
            seen.add(key)
            meters.append(_card_meter(card, cycle_id))
    else:
        # Uneven counts: a card missing a label, or a label rendered twice. Index N of one
        # list is then NOT the same card as index N of another, and pairing them publishes
        # one meter's reading under another meter's number. Scope every reading to its own
        # card instead.
        log.warning("cycle=%s meter cards have uneven label counts %s — reading each card "
                    "in its own scope instead of pairing the lists.", cycle_id, counts)
        labels = [label for _f, label in METER_LABELS]
        seen = set()
        for cell in columns["unit"]:
            card = _card_for(cell, labels)
            if card is None:
                log.warning("cycle=%s a meter card could not be isolated — skipping it "
                            "rather than guessing which values belong to it.", cycle_id)
                continue
            key = getattr(card, "id", None) or id(card)
            if key in seen:
                continue          # two labels in the same card map to one meter
            seen.add(key)
            meters.append(_card_meter(card, cycle_id))

    # A label match with nothing beside it is not a meter: publishing it would fail every
    # field, fail the cycle, and after three cycles exit the container.
    kept = []
    for meter in meters:
        if all(meter.get(field) is None for field in ("meter_no", "unit", "balance", "polled")):
            log.warning("cycle=%s ignoring a card with no readable values at all.", cycle_id)
            continue
        kept.append(meter)
    log.debug("cycle=%s %d meter card(s) read in %.1fs: %s", cycle_id, len(kept),
              time.monotonic() - started,
              [{k: m.get(k) for k in ("meter_no", "kind", "unit", "balance", "polled")}
               for m in kept])
    return kept


def _read_dashboard_legacy(driver, timeout=ELEMENT_TIMEOUT, cycle_id="none"):
    """Fallback read for a page the label probe could not find cards on."""
    values = {}
    for field, xpath in (("unit", XPATH_UNIT), ("balance", XPATH_BALANCE),
                         ("polled", XPATH_POLLED)):
        values[field] = validate_value(
            field, safe_get_text(driver, xpath, timeout, cycle_id, field), cycle_id)
    values["meter_no"] = None
    values["kind"] = unit_kind(values.get("unit"))
    log.debug("cycle=%s legacy absolute-XPath read: %s", cycle_id, values)
    return [values]


def _missing_fields(meter):
    return [name for name in ("unit", "balance", "polled") if meter.get(name) is None]


def scrape_data(driver, cycle_id="none"):
    """Reads the dashboard and retries any meter that came back incomplete."""
    meters = read_meters(driver, timeout=ELEMENT_TIMEOUT + 5, cycle_id=cycle_id)
    if not meters:
        log.warning("cycle=%s no meter card found — falling back to the legacy XPaths.", cycle_id)
        meters = _read_dashboard_legacy(driver, cycle_id=cycle_id)

    if any(_missing_fields(meter) for meter in meters):
        incomplete = [m for m in meters if _missing_fields(m)]
        log.warning("cycle=%s %d meter(s) incomplete (%s) — retrying once.",
                    cycle_id, len(incomplete),
                    "; ".join("meter %s missing %s" % (m.get("meter_no") or "n/a",
                                                       ",".join(_missing_fields(m)))
                              for m in incomplete))
        log_page_source(driver, "Incomplete scrape", cycle_id)
        time.sleep(SCRAPE_RETRY_SLEEP)
        retry = read_meters(driver, timeout=ELEMENT_TIMEOUT + 5, cycle_id=cycle_id)
        if not retry:
            retry = _read_dashboard_legacy(driver, cycle_id=cycle_id)

        # Pair by meter number, never by position: the retry is a fresh page whose card
        # order/length is not guaranteed, so merging by index can graft meter B's reading
        # onto meter A. No identity, no merge.
        by_number = {r.get("meter_no"): r for r in retry if r.get("meter_no")}
        for meter in meters:
            number = meter.get("meter_no")
            source = by_number.get(number) if number else None
            if source is None:
                continue
            for field in ("unit", "balance", "polled"):
                if meter.get(field) is None and source.get(field) is not None:
                    meter[field] = source[field]
                    log.info("cycle=%s meter %s: recovered %s on retry (%r)",
                             cycle_id, number or "n/a", field, source[field])

    run_time = now_stamp()
    log.debug("cycle=%s scraped %d meter(s): %s", cycle_id, len(meters),
              [{k: m.get(k) for k in ("meter_no", "kind", "unit", "balance", "polled")}
               for m in meters])
    return meters, run_time


def cycle_status(meters):
    """'ok' only when every meter came back complete."""
    statuses = [result_status(meter.get("unit"), meter.get("balance"), meter.get("polled"))
                for meter in meters]
    if not statuses:
        return "fail"
    if all(status == "ok" for status in statuses):
        return "ok"
    if all(status == "fail" for status in statuses):
        return "fail"
    return "partial"


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def result_status(unit, balance, polled):
    present = sum(1 for value in (unit, balance, polled) if value)
    if present == 3:
        return "ok"
    if present:
        return "partial"
    return "fail"


def _publish_topic(client, topic, value, cycle_id):
    """Publish one retained QoS-1 value and report whether the broker took it."""
    started = time.monotonic()
    try:
        info = client.publish(topic, value, qos=1, retain=True)
        info.wait_for_publish(timeout=5)
    except Exception as e:
        log.warning("cycle=%s mqtt %s NOT published after %.1fs: %s: %s",
                    cycle_id, topic, time.monotonic() - started, type(e).__name__, e)
        return False

    rc = getattr(info, "rc", None)
    is_published = getattr(info, "is_published", None)
    confirmed = is_published() if callable(is_published) else True
    if rc == mqtt.MQTT_ERR_SUCCESS and confirmed:
        log.info("cycle=%s mqtt %s = %r (qos=1 retain, acked in %.0fms)",
                 cycle_id, topic, value, (time.monotonic() - started) * 1000)
        return True

    log.warning("cycle=%s mqtt %s NOT published (rc=%s published=%s)",
                cycle_id, topic, rc, confirmed)
    return False


def _meter_topic_slugs(meters):
    """A stable topic segment per meter: the kind, or kind + meter number."""
    kinds = Counter(meter.get("kind") or "meter" for meter in meters)
    slugs = []
    for position, meter in enumerate(meters):
        kind = meter.get("kind") or "meter"
        if kinds[kind] > 1:
            slugs.append("%s_%s" % (kind, re.sub(r"\W+", "", meter.get("meter_no") or str(position))))
        else:
            slugs.append(kind)

    # Two meters whose numbers are equal, differ only in punctuation, or are both unreadable
    # collapse onto one branch: the later publish overwrites the earlier and HA refuses the
    # duplicate unique_id, so one meter silently loses its entities.
    seen = {}
    for position, slug in enumerate(slugs):
        if slug in seen:
            seen[slug] += 1
            slugs[position] = "%s_%d" % (slug, seen[slug])
            log.warning("meter branch collision on %r — using %r for the duplicate.",
                        slug, slugs[position])
        else:
            seen[slug] = 1
    return slugs


def primary_meter_index(meters):
    """The meter the LEGACY topics follow: the electricity one when there is one."""
    for index, meter in enumerate(meters):
        if meter.get("kind") == "electricity":
            return index
    return 0


def _publish_meter(client, meter, cycle_id, prefix="home/usms"):
    """Publish one meter's validated fields under `prefix`; returns per-field results."""
    got = {}
    for field, suffix in (("unit", "remaining_unit"), ("balance", "remaining_balance"),
                          ("polled", "meter_last_polled"), ("meter_no", "meter_no")):
        value = meter.get(field)
        got[field] = bool(value) and _publish_topic(
            client, "%s/%s" % (prefix, suffix), value, cycle_id)
    return got


# ---------------------------------------------------------------------------
# Home Assistant MQTT discovery
#
# The per-meter topic branches are new, so nothing in HA knows about them. Rather
# than requiring hand-written YAML on every Home Assistant that consumes them,
# the scraper publishes retained discovery configs — HA's MQTT integration then
# creates the entities itself, for exactly the meters this account has.
#
# Skipped for a SINGLE electricity meter on purpose: those setups already have
# hand-written sensors bound to the legacy `home/usms/*` topics, and discovery
# would add a duplicate set beside them.
# ---------------------------------------------------------------------------

MQTT_CONNECT_TIMEOUT = 10.0   # seconds to wait for the broker's CONNACK
DISCOVERY_PREFIX = "homeassistant"

# (topic suffix, display label, icon, extra config)
_DISCOVERY_FIELDS = (
    ("remaining_unit", "Remaining unit", "mdi:counter", {}),
    ("remaining_balance", "Remaining balance", "mdi:cash", {}),
    ("meter_last_polled", "Meter last polled", "mdi:calendar-clock", {}),
    ("meter_no", "Meter number", "mdi:information-outline", {"entity_category": "diagnostic"}),
    ("status", "Status", "mdi:cloud-check-outline", {"entity_category": "diagnostic"}),
)


def discovery_wanted(meters):
    """True when this account needs self-installing entities."""
    if not meters:
        return False
    if len(meters) > 1:
        return True
    return (meters[0].get("kind") or "meter") != "electricity"


def _discovery_payload(meter, slug, suffix, label, icon, extra):
    kind = (meter.get("kind") or "meter").capitalize()
    payload = {
        "name": "USMS %s %s" % (kind, label),
        "unique_id": "usms_%s_%s" % (slug, suffix),
        "state_topic": "home/usms/%s/%s" % (slug, suffix),
        "qos": 1,
        "icon": icon,
        "device": {
            "identifiers": ["usms_meter_%s" % slug],
            "name": "USMS %s meter" % kind,
            "manufacturer": "USMS Brunei",
            "model": "Smart meter",
        },
    }
    if meter.get("meter_no"):
        payload["device"]["serial_number"] = meter["meter_no"]
    payload.update(extra)
    return payload


# Slugs seen on the previous cycle. Anything that disappears is explicitly removed from
# the broker: a retained topic nobody updates keeps showing its last reading to Home
# Assistant forever, and a retained discovery config keeps the entity alive.
_known_slugs = set()


def _clear_topic(client, topic, cycle_id):
    """Remove one retained topic by publishing an empty retained payload."""
    try:
        info = client.publish(topic, b"", qos=1, retain=True)
        info.wait_for_publish(timeout=5)
        if getattr(info, "rc", 1) == mqtt.MQTT_ERR_SUCCESS:
            log.info("cycle=%s mqtt cleared stale topic %s", cycle_id, topic)
            return True
    except Exception as e:
        log.warning("cycle=%s mqtt could not clear %s: %s: %s",
                    cycle_id, topic, type(e).__name__, e)
    return False


def _clear_stale_branches(client, slugs, cycle_id):
    """Retire the branches of meters that are no longer on the dashboard."""
    global _known_slugs
    current = set(slugs)
    gone = _known_slugs - current
    for slug in sorted(gone):
        for suffix, _label, _icon, _extra in _DISCOVERY_FIELDS:
            _clear_topic(client, "%s/sensor/usms_%s_%s/config" % (DISCOVERY_PREFIX, slug, suffix),
                         cycle_id)
        for suffix in ("remaining_unit", "remaining_balance", "meter_last_polled",
                       "meter_no", "status"):
            _clear_topic(client, "home/usms/%s/%s" % (slug, suffix), cycle_id)
        log.warning("cycle=%s meter branch %r was on the last cycle but is gone now — its "
                    "topics and Home Assistant entities have been removed.", cycle_id, slug)
    _known_slugs = current


def publish_discovery(client, meters, cycle_id="none"):
    """Retained discovery configs for every per-meter branch. Counts successes."""
    if not discovery_wanted(meters):
        log.debug("cycle=%s discovery not needed for this account", cycle_id)
        return 0

    published = 0
    for meter, slug in zip(meters, _meter_topic_slugs(meters)):
        for suffix, label, icon, extra in _DISCOVERY_FIELDS:
            if suffix == "meter_no" and not meter.get("meter_no"):
                # The state topic is only published when the number was read; advertising
                # it anyway leaves a permanently "unknown" entity pointing at nothing.
                continue
            topic = "%s/sensor/usms_%s_%s/config" % (DISCOVERY_PREFIX, slug, suffix)
            payload = json.dumps(_discovery_payload(meter, slug, suffix, label, icon, extra))
            if _publish_topic(client, topic, payload, cycle_id):
                published += 1
    log.info("cycle=%s published %d discovery config(s) for %d meter(s)",
             cycle_id, published, len(meters))
    return published


def _reason_failed(reason_code):
    """True when an MQTT v5/v3 reason code means the broker refused us."""
    if reason_code is None:
        return True
    failing = getattr(reason_code, "is_failure", None)
    if failing is not None:
        return bool(failing)
    return int(getattr(reason_code, "value", reason_code)) != 0


def _connect_and_wait(client, cycle_id):
    """Connect, then WAIT for the CONNACK before anyone publishes.

    THE BUG THIS FIXES: `client.connect()` only starts the handshake. Until the
    broker's CONNACK is processed by the network loop, every `publish()` returns
    MQTT_ERR_NO_CONN — "The client is not currently connected." — and the message is
    dropped silently. On a fast local broker the CONNACK usually wins the race, which
    is why this looked healthy here and produced nothing at a user's site: every
    retained value AND every discovery config was thrown away. Waiting for on_connect
    removes the race, and a refused connection is now reported with the broker's own
    reason instead of looking like a successful cycle.
    """
    ready = threading.Event()
    outcome = {}

    def _on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        outcome["rc"] = reason_code
        ready.set()

    client.on_connect = _on_connect

    try:
        client.connect(mqtt_broker, mqtt_port, 60)
    except Exception as e:
        return False, "TCP connect to %s:%s failed: %s: %s" % (
            mqtt_broker, mqtt_port, type(e).__name__, e)

    client.loop_start()
    if not ready.wait(MQTT_CONNECT_TIMEOUT):
        ready.wait(0)  # no-op, keeps the intent obvious
        return False, ("the broker accepted TCP but never sent CONNACK within %ss — "
                       "wrong port, not an MQTT broker, or a proxy in front of it"
                       % MQTT_CONNECT_TIMEOUT)

    reason = outcome.get("rc")
    if _reason_failed(reason):
        return False, "the broker refused the connection: %s — check MQTT_USERNAME / " \
                      "MQTT_PASSWORD and any per-user ACL on the broker" % (reason,)
    return True, None


def _mqtt_session(cycle_id):
    """A connected MQTT client, or None. Falls back to MQTT v3.1.1 for old brokers."""
    for protocol, name in ((mqtt.MQTTv5, "v5"), (mqtt.MQTTv311, "v3.1.1")):
        client = mqtt.Client(
            client_id="",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=protocol,
        )
        if mqtt_username:
            client.username_pw_set(mqtt_username, mqtt_password)
        client.will_set(topic_status, "offline", qos=1, retain=True)

        ok, problem = _connect_and_wait(client, cycle_id)
        if ok:
            log.debug("cycle=%s mqtt connected to %s:%s using MQTT %s",
                      cycle_id, mqtt_broker, mqtt_port, name)
            return client

        log.warning("cycle=%s mqtt %s connection failed: %s", cycle_id, name, problem)
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    log.error("cycle=%s mqtt could not connect to %s:%s — nothing was published this "
              "cycle (no readings, no discovery configs)", cycle_id, mqtt_broker, mqtt_port)
    return None


def publish_mqtt(meters, run_time, cycle_id="none"):
    """Publish what validated. True only when EVERY meter completed cleanly.

    A single-meter account keeps exactly the historical topic set. A multi-meter
    account additionally gets `home/usms/<kind>/…` per meter, while the legacy
    topics stay bound to the primary (electricity) meter so an existing Home
    Assistant setup keeps working untouched.
    """
    status = cycle_status(meters)

    if not mqtt_enabled:
        log.info("cycle=%s mqtt disabled (%s) — status=%s, nothing published",
                 cycle_id, mqtt_disabled_reason(), status)
        return status == "ok"

    client = None
    published = {}
    try:
        client = _mqtt_session(cycle_id)
        if client is None:
            return False

        _clear_stale_branches(client, _meter_topic_slugs(meters), cycle_id)
        discovery_published = publish_discovery(client, meters, cycle_id)
        if discovery_wanted(meters):
            expected = len(meters) * len(_DISCOVERY_FIELDS)
            published["discovery"] = discovery_published == expected

        # Every meter always gets its own branch, even when the account has only
        # one: a topic that appears only sometimes is a documentation trap ("why is
        # home/usms/electricity empty?"). The legacy home/usms/* topics remain as an
        # alias for the primary meter so existing sensors keep working.
        for meter, slug in zip(meters, _meter_topic_slugs(meters)):
            got = _publish_meter(client, meter, cycle_id, prefix="home/usms/%s" % slug)
            meter_status = result_status(meter.get("unit"), meter.get("balance"),
                                         meter.get("polled"))
            got["status"] = _publish_topic(
                client, "home/usms/%s/status" % slug, meter_status, cycle_id)
            for field, ok in got.items():
                published["%s.%s" % (slug, field)] = ok
            log.info("cycle=%s meter=%s (%s) published under home/usms/%s/* status=%s",
                     cycle_id, meter.get("meter_no") or "n/a", slug, slug, meter_status)

        primary = meters[primary_meter_index(meters)]
        if len(meters) > 1:
            log.info("cycle=%s legacy topics follow meter %s",
                     cycle_id, primary.get("meter_no") or "n/a")

        for field, ok in _publish_meter(client, primary, cycle_id).items():
            published[field] = ok

        if status == "ok":
            published["last_run"] = _publish_topic(client, topic_lastrun, run_time, cycle_id)
        else:
            # Deliberately stale: HA's staleness alarm is the signal.
            log.info("cycle=%s not advancing %s — cycle was %s",
                     cycle_id, topic_lastrun, status)

        published["status"] = _publish_topic(client, topic_status, status, cycle_id)
    except Exception as e:
        log.error("cycle=%s mqtt error: %s: %s", cycle_id, type(e).__name__, e)
        return False
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception as e:
                log.debug("cycle=%s mqtt close warning: %s", cycle_id, e)

    ok = status == "ok" and all(published.values())
    log.info("cycle=%s mqtt status=%s per-topic=%s", cycle_id, status, json.dumps(published))
    return ok


def print_summary(meters, run_time):
    print("\n=== USMS Data Summary ===")
    for meter in meters:
        label = "%s (%s)" % (meter.get("meter_no") or "meter", meter.get("kind") or "unknown")
        print(f"{label}")
        print(f"  Remaining Unit    : {meter.get('unit') or 'N/A'}")
        print(f"  Remaining Balance : {meter.get('balance') or 'N/A'}")
        print(f"  Meter Last Polled : {meter.get('polled') or 'N/A'}")
    print(f"Last Run Time     : {run_time}")
    print("========================\n")


# ---------------------------------------------------------------------------
# One cycle + the supervisor loop
# ---------------------------------------------------------------------------

def log_cycle_summary(cycle_id, status, meters, timings, total):
    """One greppable line that answers: which cycle, how slow, what happened.

    unit/balance/polled stay in the line for the single-meter case (and tooling
    that greps them); a multi-meter account shows the primary meter's readings
    here and every meter's own line above.
    """
    primary = meters[primary_meter_index(meters)] if meters else {}
    log.info(
        "cycle=%s result=%s meters=%d unit=%r balance=%r polled=%r captcha_rolls=%d "
        "driver=%.1fs login=%.1fs scrape=%.1fs publish=%.1fs total=%.1fs",
        cycle_id, status, len(meters), primary.get("unit"), primary.get("balance"),
        primary.get("polled"), _cycle_state["captcha_rolls"],
        timings.get("driver", 0.0), timings.get("login", 0.0),
        timings.get("scrape", 0.0), timings.get("publish", 0.0), total,
    )


def run_cycle(cycle_id="none"):
    """One scrape cycle. Returns {'status': ok|partial|fail, 'published': bool}."""
    driver = None
    started = time.monotonic()
    timings = {}
    status = "fail"
    published = False
    meters = []
    _cycle_state["captcha_rolls"] = 0
    log.info("cycle=%s begin (interval=%ds, selenium=%s:%s, mqtt=%s)",
             cycle_id, scrape_interval, selenium_host, selenium_port,
             "on" if mqtt_enabled else "off")

    try:
        mark = time.monotonic()
        driver = create_driver()
        timings["driver"] = time.monotonic() - mark

        mark = time.monotonic()
        if not is_logged_in(driver, cycle_id) and not login(driver, cycle_id):
            # A login that returned False is a failure, not a reason to scrape.
            raise RuntimeError("login failed")
        timings["login"] = time.monotonic() - mark

        mark = time.monotonic()
        meters, run_time = scrape_data(driver, cycle_id)
        timings["scrape"] = time.monotonic() - mark
        status = cycle_status(meters)

        mark = time.monotonic()
        published = publish_mqtt(meters, run_time, cycle_id)
        timings["publish"] = time.monotonic() - mark
        print_summary(meters, run_time)
    finally:
        if driver is not None:
            try:
                driver.quit()
                log.debug("cycle=%s WebDriver session cleaned up.", cycle_id)
            except Exception as e:
                log.warning("cycle=%s WebDriver cleanup warning (non-fatal): %s", cycle_id, e)
        log_cycle_summary(cycle_id, status, meters, timings,
                          time.monotonic() - started)

    return {"status": status, "published": published}


def report_credentials_rejected(reason, cycle_id="none"):
    """Explain a portal credential rejection, with the numbers needed to fix it."""
    shape, shape_hint = username_shape(usms_username)
    log.critical("cycle=%s the USMS portal REJECTED the credentials (%s).", cycle_id, reason)
    # Masked like every other log line: these logs get pasted into issues and chat,
    # and the IC number identifies the account holder.
    log.critical("cycle=%s   username: %s (%d chars, from %s, looks like %s)",
                 cycle_id, _mask_digits(usms_username), len(usms_username or ""),
                 _CREDENTIAL_SOURCE.get("USMS_USERNAME", "unset"), shape)
    log.critical("cycle=%s   password: %d chars, from %s%s", cycle_id,
                 len(usms_password or ""),
                 _CREDENTIAL_SOURCE.get("USMS_PASSWORD", "unset"),
                 " — CONTAINS '$'" if "$" in (usms_password or "") else "")
    if shape_hint:
        log.critical("cycle=%s   %s", cycle_id, shape_hint)
    log.critical("cycle=%s   If the password you typed is longer than %d characters, "
                 "Docker Compose truncated it at a '$'. Fix it by putting the value in the "
                 "same variable as b64:<base64> — that "
                 "survives Compose and needs no file.", cycle_id, len(usms_password or ""))
    log.critical("cycle=%s   If the length matches what you typed, the portal password "
                 "itself is different from this one: log in to https://www.usms.com.bn in a "
                 "browser to confirm it, then update the compose file.", cycle_id)
    log.critical("cycle=%s NO FURTHER LOGIN ATTEMPTS until the credentials are fixed — the "
                 "portal allows only a handful of tries before it locks the account.",
                 cycle_id)


def wait_for_fixed_credentials(cycle_id="none"):
    """Park the process instead of retrying: each retry burns the portal's budget."""
    log.critical("Waiting for the credentials to be corrected. Change USMS_USERNAME / "
                 "USMS_PASSWORD and recreate the container: "
                 "docker compose up -d --force-recreate usms-scrape")

    # The single state in which the scraper is definitively broken used to publish nothing:
    # `status` kept its last retained value, so Home Assistant showed "ok" indefinitely.
    if mqtt_enabled:
        client = _mqtt_session(cycle_id)
        if client is not None:
            try:
                _publish_topic(client, topic_status, "fail", cycle_id)
            finally:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
    hours = 0
    while True:
        time.sleep(3600)
        hours += 1
        log.warning("%dh parked — credentials still not changed, still not attempting a "
                    "login. Recreate the container once they are fixed.", hours)


def install_signal_handlers():
    """Turn SIGTERM into SystemExit so the finally block quits the WebDriver.

    `docker stop` sends SIGTERM and waits 10 s; without this the process died
    mid-cycle and left the Chrome session behind, which on
    SE_NODE_MAX_SESSIONS=1 blocks the next container start.
    """
    def _graceful(signum, _frame):
        log.info("Received signal %s — releasing the WebDriver session and exiting.", signum)
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError) as e:
            log.debug("Could not install a handler for %s: %s", sig, e)


# === MAIN LOOP ===

def main():
    validate_config()
    log_startup_config()
    install_signal_handlers()
    log.info("USMS Scrape starting up.")

    failure_count = 0
    next_run = time.monotonic()

    while True:
        cycle_id = uuid.uuid4().hex[:4]
        next_run += scrape_interval

        try:
            outcome = run_cycle(cycle_id)
            # Deliberately the SCRAPE status only: restarting us cannot heal a broker or
            # Home Assistant outage, and counting it here exited the container every three
            # cycles — re-authenticating against the portal each time, the one thing that
            # has to be rationed.
            cycle_ok = outcome["status"] == "ok"
            if not outcome["published"]:
                log.warning("cycle=%s the scrape succeeded but publishing did not — not "
                            "counted as a failure, so a broker problem cannot hammer the "
                            "portal through our restart policy.", cycle_id)
        except CredentialsRejected as e:
            # Not a transient failure: retrying burns the portal's attempt budget and
            # ends with a locked account. Park until a human fixes the credentials.
            report_credentials_rejected(str(e), cycle_id)
            wait_for_fixed_credentials(cycle_id)
            continue   # only reached if parking returns, which it does not
        except SystemExit:
            raise
        except Exception:
            # exc_info: this is the single funnel for every unexpected failure.
            log.exception("cycle=%s result=error — unhandled failure", cycle_id)
            cycle_ok = False

        # Every failure counts, not just an all-N/A scrape: an unreachable
        # Selenium or a dead portal is exactly what the restart policy is for.
        failure_count = 0 if cycle_ok else failure_count + 1

        if failure_count >= MAX_CONSECUTIVE_FAILURES:
            log.error(
                "cycle=%s %d consecutive failed cycles — exiting so the restart "
                "policy (unless-stopped) recycles us with a clean Selenium session.",
                cycle_id, failure_count,
            )
            sys.exit(1)

        now = time.monotonic()
        if next_run <= now:
            # Overran the interval: run the next cycle after a full interval
            # rather than firing a catch-up burst at the portal.
            log.warning("cycle=%s overran its %ds interval — next cycle in %ds",
                        cycle_id, scrape_interval, scrape_interval)
            next_run = now + scrape_interval

        sleep_for = max(0.0, next_run - now)
        log.info("cycle=%s sleeping %.0fs (interval %ds)", cycle_id, sleep_for, scrape_interval)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted — stopping.")
