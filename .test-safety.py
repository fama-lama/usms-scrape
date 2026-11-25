"""Safety matrix for the ONNX Runtime thread-affinity hardening in usms.py.

Loads usms.py WITHOUT running its main loop (everything above the MAIN LOOP
marker), then pushes the hardening through every shape a caller could use and
through the OS variations other people's machines have.

Run inside the published image:
  docker run --rm -v usms.py:/app/usms.py:ro -v .test-safety.py:/test.py:ro \
      --entrypoint python3 IMAGE /test.py
"""
import glob
import io
import os
import sys

SRC = os.environ.get("USMS_SRC", "/app/usms.py")
with open(SRC) as fh:
    head = fh.read().split("# === MAIN LOOP ===")[0]

ns = {"__name__": "usms_under_test", "__file__": SRC}
exec(compile(head, SRC, "exec"), ns)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


import onnxruntime as ort

MODEL = (glob.glob("/usr/local/lib/python3*/site-packages/ddddocr/**/*.onnx", recursive=True)
         or glob.glob("/usr/local/lib/python3*/site-packages/ddddocr/*.onnx"))[0]

# --- 1. CPU detection across operating systems -----------------------------
real_affinity = getattr(os, "sched_getaffinity", None)
real_cpu_count = os.cpu_count

print(f"INFO  cpus visible={len(real_affinity(0)) if real_affinity else 'n/a'} "
      f"cpu_count={real_cpu_count()}")

check("available_cpus >= 1", ns["_available_cpus"]() >= 1, ns["_available_cpus"]())

if real_affinity is not None:
    del os.sched_getaffinity                      # macOS / Windows shape
    try:
        check("no sched_getaffinity -> cpu_count",
              ns["_available_cpus"]() == max(1, real_cpu_count()))
    finally:
        os.sched_getaffinity = real_affinity

    def _boom(_pid):
        raise OSError("simulated")

    os.sched_getaffinity = _boom                # sandbox / seccomp shape
    try:
        check("sched_getaffinity OSError -> cpu_count",
              ns["_available_cpus"]() == max(1, real_cpu_count()))
    finally:
        os.sched_getaffinity = real_affinity

os.cpu_count = lambda: None                     # exotic python build
if real_affinity is not None:
    del os.sched_getaffinity                    # ... and no sched_getaffinity
try:
    check("cpu_count None -> 1", ns["_available_cpus"]() == 1, ns["_available_cpus"]())
finally:
    os.cpu_count = real_cpu_count
    if real_affinity is not None:
        os.sched_getaffinity = real_affinity

expected = max(1, min(ns["ORT_MAX_THREADS"], ns["_available_cpus"]()))
print(f"INFO  expected intra_op_threads={expected}")

# --- 2. idempotency --------------------------------------------------------
ns["_harden_onnxruntime"]()
first = ort.InferenceSession
ns["_harden_onnxruntime"]()
ns["_harden_onnxruntime"]()
check("idempotent (no double wrap)", ort.InferenceSession is first)
check("hardened flag set", getattr(ort, "_usms_affinity_hardened", False) is True)


def intra(session):
    return session.get_session_options().intra_op_num_threads


def inter(session):
    return session.get_session_options().inter_op_num_threads


# --- 3. every call shape ddddocr or a future version might use -------------
s = ort.InferenceSession(MODEL)                                  # path only
check("bare positional: intra", intra(s) == expected, intra(s))
check("bare positional: inter == 1", inter(s) == 1, inter(s))
check("bare positional: inputs readable", len(s.get_inputs()) >= 1)

s = ort.InferenceSession(MODEL, None)                            # explicit None slot
check("positional None slot: intra", intra(s) == expected, intra(s))

s = ort.InferenceSession(MODEL, None, ["CPUExecutionProvider"])  # + providers
check("providers positional honoured",
      s.get_providers() == ["CPUExecutionProvider"], s.get_providers())

s = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])   # ddddocr's shape
check("providers kwarg honoured (ddddocr)",
      s.get_providers() == ["CPUExecutionProvider"], s.get_providers())
check("providers kwarg: intra", intra(s) == expected, intra(s))

own = ort.SessionOptions()
own.intra_op_num_threads = 7                                     # caller's own options
s = ort.InferenceSession(MODEL, own)
check("caller options overridden", intra(s) == expected, intra(s))

own_kw = ort.SessionOptions()
own_kw.intra_op_num_threads = 7
s = ort.InferenceSession(MODEL, sess_options=own_kw)              # ... as kwarg
check("caller kwarg options overridden", intra(s) == expected, intra(s))

try:                                                             # unknown slot
    ort.InferenceSession(MODEL, "not-options")
    check("unknown positional arg passed through", False, "no exception")
except TypeError:
    check("unknown positional arg passed through", True)
except Exception as exc:                                          # noqa: BLE001
    check("unknown positional arg passed through", False, type(exc).__name__)

# --- 4. ddddocr still works through the patched module --------------------
import ddddocr
from PIL import Image, ImageDraw

eng = ddddocr.DdddOcr(show_ad=False)
img = Image.new("RGB", (200, 80), "white")
draw = ImageDraw.Draw(img)
draw.text((40, 25), "a8f2k", fill="black")
buf = io.BytesIO()
img.save(buf, format="PNG")
out = eng.classification(buf.getvalue())
check("ddddocr classification returns str", isinstance(out, str), type(out).__name__)
print(f"INFO  ddddocr read synthetic image as {out!r}")

print()
print(f"{'ALL CHECKS PASSED' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
