#!/usr/bin/env python3
"""
Local test bench for .github/workflows/transcode.yml — run it BEFORE pushing any change to that file.

WHY IT EXISTS (2026-09-19). The workflow drives every production review-proxy transcode and can only be
truly exercised by a real GitHub Actions run, so bugs sat in it unseen: a project name with a double quote
failed the whole step, `$(cmd)` in a name executed on the runner (which holds the R2 keys), an en-dash escape
burned the literal text "xe2x80x93" onto every proxy, and a "%" in a name made the project/client line vanish
while the step still exited 0. This bench found all four. It runs the workflow's REAL `run:` bodies:

  * each step's script is taken from the YAML and run under `bash -e`;
  * GitHub's `${{ }}` substitution is reproduced as TEXT (that textual substitution IS the injection bug class);
    `env:` values are set as real environment variables, exactly as the runner does;
  * `aws` and `curl` are STUBBED (they only record their arguments — so a pass proves the ARGUMENTS the scripts
    build are exact, NOT that R2 or the portal callback accept them);
  * ffmpeg / ffprobe are REAL, run against a small synthetic 1280x720 master;
  * the two callback steps are ALSO run with the REAL curl against a local stand-in for the portal, because the
    stubbed curl cannot show whether a failed callback is retried (2026-09-21: a DNS failure on the callback left
    a finished transcode unreported, and nothing in this bench could have caught it).

USAGE
  python tests/test_workflow.py                       test the working-tree workflow
  python tests/test_workflow.py --spec HEAD~4:.github/workflows/transcode.yml
                                                      test another version (git-ref:path) — use this to prove
                                                      the bench can FAIL: run it against a known-bad version
  python tests/test_workflow.py --frames              also write a cropped PNG of the burned text lines per
                                                      case, to look at (a rendered frame shows what an exit
                                                      code cannot: a missing line, a garbled dash)
Exit 0 = every check passed, 1 = something failed.

NEEDS: python3 + PyYAML, bash (Git Bash on Windows), curl, ffmpeg + ffprobe on PATH. No internet (the callback
checks talk only to a local server and to a name that can never resolve), no secrets.
OUTPUT goes to the OS temp folder (never into this repo).

WHEN YOU ADD SOMETHING to the workflow that consumes payload text, add a hostile case to CASES below.
"""
import argparse
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/transcode.yml"
WORK = Path(tempfile.gettempdir()) / "nrr-transcode-tests"

STEPS = ["Compute keys", "Download master from R2", "Use Boska font", "Build watermark slate",
         "Transcode", "Upload proxy to R2", "Report success"]
MEDIA_KEY = "projects/26/review/20260919T101500-abcdef012345/master.mp4"
PROXY_KEY = "projects/26/review/20260919T101500-abcdef012345/proxy.mp4"
DASH = "\u2013"  # the en-dash the burned project/client line puts between the two names

# label: (project_name, client_name, asset_type). Each hostile case is a name a real admin could plausibly type.
CASES = {
    "control":      ("Kericho Gold Launch", "Acme Ltd", "VFX"),
    "double_quote": ('Nike "Just Do It" Reel', "Acme Ltd", "VFX"),
    "dollar":       ("Cost $5 Promo", "Acme Ltd", "VFX"),
    "cmd_subst":    ("$(touch INJECTED)", "Acme Ltd", "VFX"),
    "backtick":     ("`touch INJECTED`", "Acme Ltd", "VFX"),
    "quotes_colon": ("O'Brien & Co. Phase 2: Final", "Kim's Diner", "3D Animation"),
    "percent":      ("50% Off {Sale} C:\\Reel", "Acme Ltd", "VFX"),
    "pct_function": ("Q3 %{pts} review", "Acme Ltd", "VFX"),
    "empty_names":  ("", "", ""),  # the bash defaults must apply
    # Frame-shape cases (rendered on a BLACK master so every lit pixel is slate): a normal name and a very long one, on
    # landscape and portrait. The long one must never run off the frame.
    "normal_portrait": ("Test MI6", "MI6", "VFX"),
    "long_landscape":  ('The Kericho Gold Reserve Anniversary Launch Film - Cut 4', 'Acme Holdings International Ltd', "VFX"),
    "long_portrait":   ('The Kericho Gold Reserve Anniversary Launch Film - Cut 4', 'Acme Holdings International Ltd', "VFX"),
}
SHAPES = {"normal_portrait": (720, 1280), "long_landscape": (1280, 720), "long_portrait": (720, 1280)}


def load_workflow(spec):
    if spec:
        ref, _, path = spec.partition(":")
        text = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO, capture_output=True, check=True).stdout.decode()
    else:
        text = (REPO / WORKFLOW).read_bytes().decode()
    return yaml.safe_load(text)


def substitute(s, ctx):
    """Reproduce GitHub's textual ${{ }} substitution."""
    def one(m):
        e = m.group(1).strip()
        if e.startswith("github.event.client_payload."):
            return ctx["payload"][e.rsplit(".", 1)[1]]
        if e.startswith("steps.keys.outputs."):
            return ctx["outputs"][e.rsplit(".", 1)[1]]
        if e.startswith("secrets."):
            return "SECRET_" + e.split(".", 1)[1]
        raise ValueError("unhandled expression in workflow: " + e)
    return re.sub(r"\$\{\{\s*(.+?)\s*\}\}", one, str(s))


def write_stubs(d):
    d.mkdir(parents=True, exist_ok=True)
    log = str(d.parent / "calls.log").replace("\\", "/")
    for name in ("aws", "curl"):
        body = ("#!/usr/bin/env bash\n"
                f'printf "{name}:" >> "{log}"; for a in "$@"; do printf " [%s]" "$a" >> "{log}"; done; echo >> "{log}"\n')
        if name == "aws":
            body += 'if [ "$2" = "get-object" ]; then cp "$SYNTH_MASTER" master.mp4; fi\n'
        (d / name).write_text(body + "exit 0\n", newline="\n")


def make_master(path):
    if path.exists():
        return
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def make_black_master(path, w, h):
    """A black master of the given shape: on black, every lit pixel of a decoded proxy frame is slate text or a bracket."""
    if path.exists():
        return
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def lit_columns(proxy, w, h, top, bottom):
    """Leftmost and rightmost lit pixel columns within rows top..bottom of one decoded frame of the proxy."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.2", "-i", str(proxy), "-frames:v", "1",
                          "-vf", "format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    lo, hi = w, -1
    for y in range(max(0, top), min(h, bottom + 1)):
        for x, v in enumerate(raw[y * w:(y + 1) * w]):
            if v > 30:
                lo, hi = min(lo, x), max(hi, x)
    return lo, hi


def slate_fit_checks(wd, case, r):
    """The burned project/client text must stay between the slate's own insets, whatever the name length or frame shape.
    (Found 2026-09-21 while making the text 1.8x larger: a long name ran off a portrait frame.)"""
    w, h = SHAPES[case]
    filt = read(wd / "slate.filter") or ""
    text = {("l1" if "line1.txt" in ln else "l2"): ln for ln in filt.splitlines() if "textfile=" in ln}
    inset = int(re.search(r"drawbox=x=(\d+):y=\1:", filt).group(1))
    off = lambda ln: sum(int(g) for g in re.search(r"y=h-(\d+)-(\d+)-(\d+)", ln).groups())   # text top, measured up from the bottom
    fs = lambda ln: int(re.search(r"fontsize=(\d+)", ln).group(1))
    top, bottom = h - off(text["l2"]), h - off(text["l1"]) + fs(text["l1"])
    lo, hi = lit_columns(wd / "proxy.mp4", w, h, top, bottom)
    r.check(lo >= inset and hi <= w - inset,
            f"the burned text stays inside the slate's insets on a {w}x{h} frame",
            f"text spans columns {lo}..{hi}; it must stay within {inset}..{w - inset}")
    if case == "long_portrait":
        ref = WORK / "cases" / "normal_portrait" / "slate.filter"
        if ref.exists():
            ref_fs = fs(next(ln for ln in ref.read_text().splitlines() if "line1.txt" in ln))
            r.check(fs(text["l1"]) < ref_fs, "a name too long for the frame gets a SMALLER font (never clipped or cut text)",
                    f"{fs(text['l1'])} px vs {ref_fs} px for a normal name")
    if case == "normal_portrait":
        ctrl = WORK / "cases" / "control" / "slate.filter"
        if ctrl.exists():
            ctrl_fs = fs(next(ln for ln in ctrl.read_text().splitlines() if "line1.txt" in ln))
            r.check(fs(text["l1"]) == ctrl_fs, "a normal name keeps the full font size (the fit guard only acts on long names)",
                    f"{fs(text['l1'])} px vs {ctrl_fs} px")


class Results:
    def __init__(self):
        self.failed = 0

    def check(self, ok, label, detail=""):
        self.failed += (not ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
        return ok


def static_checks(doc, r):
    print("== static: payload text must never be substituted into a shell body ==")
    bad = []
    for step in doc["jobs"]["transcode"]["steps"]:
        for expr in re.findall(r"\$\{\{\s*(.+?)\s*\}\}", step.get("run") or ""):
            if not expr.startswith("secrets."):  # repo secrets are owner-controlled
                bad.append(f"{step['name']}: {expr}")
    r.check(not bad, "no ${{ }} other than secrets.* inside any run: body", "; ".join(bad[:4]) + (" ..." if len(bad) > 4 else ""))

    print("== static: the callbacks are retried, visible, and cannot mis-report a good transcode ==")
    steps = {s["name"]: s for s in doc["jobs"]["transcode"]["steps"]}
    ok_step, fail_step = steps["Report success"], steps["Report failure"]
    ok_id = ok_step.get("id")
    cond = str(fail_step.get("if", ""))
    r.check(bool(ok_id) and "failure()" in cond and f"steps.{ok_id}.outcome != 'failure'" in cond,
            "Report failure does NOT fire when only the success callback failed (a good transcode must never be reported failed)",
            f"id={ok_id!r} if={cond!r}")

    def policy(step):  # the retry flags, whitespace/line-continuation normalised so the two steps can be compared
        body = re.sub(r"\s+", " ", re.sub(r"\\\s*\n", " ", step.get("run") or ""))
        m = re.search(r"--connect-timeout \S+ --max-time \S+ --retry \S+ --retry-delay \S+ --retry-max-time \S+", body)
        return m.group(0) if m else None
    r.check(policy(ok_step) is not None and policy(ok_step) == policy(fail_step),
            "both callbacks carry the SAME retry policy (--connect-timeout/--max-time/--retry/--retry-delay/--retry-max-time)",
            f"success={policy(ok_step)!r} failure={policy(fail_step)!r}")
    r.check(all("curl -sS" in (s.get("run") or "") for s in (ok_step, fail_step)),
            "curl runs with -S, so the error text (e.g. 'Could not resolve host') reaches the run log, not just an exit code")


def run_case(doc, case, frames):
    proj, client, asset = CASES[case]
    wd = WORK / "cases" / case
    if wd.exists():
        shutil.rmtree(wd)
    (wd / "assets").mkdir(parents=True)
    shutil.copy(REPO / "assets" / "Boska-Black.ttf", wd / "assets" / "Boska-Black.ttf")
    write_stubs(wd / "stubs")
    by_name = {s["name"]: s for s in doc["jobs"]["transcode"]["steps"]}
    ctx = {"payload": {"media_key": MEDIA_KEY, "project_name": proj, "client_name": client, "asset_type": asset,
                       "callback_url": "https://example.invalid/cb", "approval_id": "99", "signature": "deadbeef"},
           "outputs": {}}
    base = os.environ.copy()
    base["PATH"] = str(wd / "stubs") + os.pathsep + base["PATH"]
    if case in SHAPES:
        sw, sh = SHAPES[case]
        make_black_master(WORK / f"master_black_{sw}x{sh}.mp4", sw, sh)
        base["SYNTH_MASTER"] = str(WORK / f"master_black_{sw}x{sh}.mp4")
    else:
        base["SYNTH_MASTER"] = str(WORK / "master_src.mp4")
    base["GITHUB_OUTPUT"] = str(wd / "gh_output")
    (wd / "gh_output").write_text("")
    codes, err = {}, ""
    for name in STEPS:
        st = by_name[name]
        env = dict(base)
        for k, v in (st.get("env") or {}).items():
            env[k] = substitute(v, ctx)                       # env: values arrive as REAL env vars
        (wd / "step.sh").write_bytes(substitute(st["run"], ctx).replace("\r\n", "\n").encode())
        p = subprocess.run(["bash", "-e", "step.sh"], cwd=wd, env=env, capture_output=True)
        codes[name] = p.returncode
        if name == "Compute keys":
            for line in (wd / "gh_output").read_text().splitlines():
                k, _, v = line.partition("=")
                ctx["outputs"][k] = v
        if p.returncode != 0:
            err = (p.stderr.decode(errors="replace").strip().splitlines() or [""])[0][:100]
            break
    frame = None
    if frames and (wd / "proxy.mp4").exists():
        (WORK / "frames").mkdir(parents=True, exist_ok=True)
        frame = WORK / "frames" / f"{case}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.2", "-i", str(wd / "proxy.mp4"), "-frames:v", "1",
                        "-vf", "crop=470:52:770:606,scale=iw*2:ih*2:flags=neighbor", str(frame)], check=True)
    return wd, codes, err, (proj, client, asset), frame


def read(p):
    return p.read_bytes().decode("utf-8", "replace") if p.exists() else None


def case_checks(doc, case, frames, r):
    wd, codes, err, (proj, client, asset), frame = run_case(doc, case, frames)
    print(f"== case: {case}   project={proj!r} client={client!r} ==")
    ok_steps = all(codes.get(s) == 0 for s in STEPS)
    r.check(ok_steps, "every step exits 0", f"{ {k: v for k, v in codes.items() if v} } {err}")
    r.check(not (wd / "INJECTED").exists(), "nothing in the name was executed as a command")
    want1 = f"{proj or 'Untitled Project'} {DASH} {client or 'Nairoreel Client'}"
    want2 = f"{asset or 'Review'} | FOR REVIEW"
    r.check(read(wd / "line1.txt") == want1, "burned project/client line is the exact text, with a real en-dash",
            f"got {read(wd / 'line1.txt')!r}")
    r.check(read(wd / "line2.txt") == want2, "burned asset line is the exact text", f"got {read(wd / 'line2.txt')!r}")
    r.check((wd / "proxy.mp4").exists(), "a proxy file was produced")
    filt = read(wd / "slate.filter") or ""
    text_lines = [l for l in filt.splitlines() if "textfile=" in l]
    r.check(len(text_lines) == 2 and all("expansion=none" in l for l in text_lines),
            "the two free-text lines use expansion=none (drawtext must not treat names as a template)")
    r.check(not any("expansion=none" in l for l in filt.splitlines() if "textfile=" not in l),
            "the clock line keeps default expansion (it needs %{eif:...})")
    if case == "control":
        calls = (read(wd / "calls.log") or "").splitlines()
        r.check(any(f"[--key] [{MEDIA_KEY}]" in c and "get-object" in c for c in calls), "download uses the exact master key")
        r.check(any(f"[--key] [{PROXY_KEY}]" in c and "put-object" in c for c in calls), "upload uses the exact proxy key")
        body = '{"approval_id": 99, "signature": "deadbeef", "status": "ok", "proxy_key": "' + PROXY_KEY + '"}'
        r.check(any(f"[-d] [{body}]" in c for c in calls), "success callback body is exact, valid JSON")
    if frame:
        print(f"      frame: {frame}")
    if case in SHAPES:
        slate_fit_checks(wd, case, r)


def failure_step_check(doc, r):
    print("== failure callback (fires when a transcode breaks) ==")
    st = {s["name"]: s for s in doc["jobs"]["transcode"]["steps"]}["Report failure"]
    wd = WORK / "cases" / "_failure"
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    write_stubs(wd / "stubs")
    ctx = {"payload": {"callback_url": "https://example.invalid/cb", "approval_id": "99", "signature": "deadbeef"}, "outputs": {}}
    env = os.environ.copy()
    env["PATH"] = str(wd / "stubs") + os.pathsep + env["PATH"]
    for k, v in (st.get("env") or {}).items():
        env[k] = substitute(v, ctx)
    (wd / "step.sh").write_bytes(substitute(st["run"], ctx).replace("\r\n", "\n").encode())
    p = subprocess.run(["bash", "-e", "step.sh"], cwd=wd, env=env, capture_output=True)
    calls = read(wd / "calls.log") or ""
    r.check(p.returncode == 0 and '{"approval_id": 99, "signature": "deadbeef", "status": "failed"}' in calls,
            "failure callback body is exact, valid JSON")


class _Portal(http.server.BaseHTTPRequestHandler):
    """A stand-in for the portal's transcode-callback.php: replies with a scripted list of status codes (the last one
    repeats) and records the body of every POST it receives."""
    script, seen = [200], []

    def do_POST(self):
        cls = type(self)
        cls.seen.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        code = cls.script[min(len(cls.seen) - 1, len(cls.script) - 1)]
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def run_report_success(doc, url):
    """Run the workflow's real 'Report success' body under bash -e with the REAL curl. Only the retry delay is shortened,
    through the env var the workflow itself reads, so the script under test is exactly the one that ships."""
    st = {s["name"]: s for s in doc["jobs"]["transcode"]["steps"]}["Report success"]
    wd = WORK / "cases" / "_callback"
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    ctx = {"payload": {"callback_url": url, "approval_id": "99", "signature": "deadbeef"}, "outputs": {"proxy_key": PROXY_KEY}}
    env = os.environ.copy()
    for k, v in (st.get("env") or {}).items():
        env[k] = substitute(v, ctx)
    env["CALLBACK_RETRY_DELAY"] = "1"
    (wd / "step.sh").write_bytes(substitute(st["run"], ctx).replace("\r\n", "\n").encode())
    t0 = time.time()
    p = subprocess.run(["bash", "-e", "step.sh"], cwd=wd, env=env, capture_output=True, timeout=180)
    return p, time.time() - t0


def callback_behaviour_checks(doc, r):
    print("== callback behaviour: the REAL Report success step, real curl, a local stand-in for the portal ==")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/cb"
    body = '{"approval_id": 99, "signature": "deadbeef", "status": "ok", "proxy_key": "' + PROXY_KEY + '"}'
    try:
        _Portal.script, _Portal.seen = [503, 503, 200], []
        p, _ = run_report_success(doc, url)
        r.check(p.returncode == 0 and len(_Portal.seen) == 3 and set(_Portal.seen) == {body},
                "a portal that answers 503, 503, then 200 is retried until it accepts: exit 0, same exact body 3 times",
                f"exit={p.returncode} posts={len(_Portal.seen)}")

        _Portal.script, _Portal.seen = [401], []
        p, _ = run_report_success(doc, url)
        r.check(p.returncode != 0 and len(_Portal.seen) == 1 and b"::error::" in p.stdout,
                "a permanent refusal (401: bad signature) is NOT retried, and fails with a readable ::error:: line",
                f"exit={p.returncode} posts={len(_Portal.seen)} out={p.stdout[:120]!r}")
    finally:
        srv.shutdown()

    p, secs = run_report_success(doc, "http://no-such-host.invalid/cb")   # .invalid can never resolve (RFC 2606)
    r.check(p.returncode == 6 and secs >= 4,
            "an unresolvable portal host (the 2026-09-21 failure) is retried, then fails with curl's own exit code 6",
            f"exit={p.returncode} after {secs:.1f}s (retry delay is 1s here; no retries would end in ~0.2s)")
    r.check(b"Could not resolve host" in p.stderr and b"::error::" in p.stdout and b"exit 6" in p.stdout,
            "...and the log says WHY in words (curl's message + a ::error:: line that names the exit code)",
            f"stderr={p.stderr[:100]!r} stdout={p.stdout[:100]!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", help="git-ref:path of a workflow version to test instead of the working tree")
    ap.add_argument("--frames", action="store_true", help="write a cropped PNG of the burned text lines per case")
    ap.add_argument("--only", help="comma-separated case names")
    a = ap.parse_args()
    for tool in ("bash", "curl", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"missing required tool on PATH: {tool}")
    WORK.mkdir(parents=True, exist_ok=True)
    make_master(WORK / "master_src.mp4")
    doc = load_workflow(a.spec)
    print(f"testing: {a.spec or WORKFLOW + ' (working tree)'}    output: {WORK}\n")
    r = Results()
    static_checks(doc, r)
    for case in (a.only.split(",") if a.only else CASES):
        case_checks(doc, case, a.frames, r)
    failure_step_check(doc, r)
    callback_behaviour_checks(doc, r)
    print(f"\n{'ALL CHECKS PASSED' if not r.failed else str(r.failed) + ' CHECK(S) FAILED'}")
    sys.exit(1 if r.failed else 0)


if __name__ == "__main__":
    main()
