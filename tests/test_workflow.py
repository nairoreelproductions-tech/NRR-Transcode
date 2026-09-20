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
  * ffmpeg / ffprobe are REAL, run against a small synthetic 1280x720 master.

USAGE
  python tests/test_workflow.py                       test the working-tree workflow
  python tests/test_workflow.py --spec HEAD~4:.github/workflows/transcode.yml
                                                      test another version (git-ref:path) — use this to prove
                                                      the bench can FAIL: run it against a known-bad version
  python tests/test_workflow.py --frames              also write a cropped PNG of the burned text lines per
                                                      case, to look at (a rendered frame shows what an exit
                                                      code cannot: a missing line, a garbled dash)
Exit 0 = every check passed, 1 = something failed.

NEEDS: python3 + PyYAML, bash (Git Bash on Windows), ffmpeg + ffprobe on PATH. No network, no secrets.
OUTPUT goes to the OS temp folder (never into this repo).

WHEN YOU ADD SOMETHING to the workflow that consumes payload text, add a hostile case to CASES below.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
}


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
    want1 = f"{proj or 'Untitled Project'} {DASH} {client or 'NairoReel Client'}"
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", help="git-ref:path of a workflow version to test instead of the working tree")
    ap.add_argument("--frames", action="store_true", help="write a cropped PNG of the burned text lines per case")
    ap.add_argument("--only", help="comma-separated case names")
    a = ap.parse_args()
    for tool in ("bash", "ffmpeg", "ffprobe"):
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
    print(f"\n{'ALL CHECKS PASSED' if not r.failed else str(r.failed) + ' CHECK(S) FAILED'}")
    sys.exit(1 if r.failed else 0)


if __name__ == "__main__":
    main()
