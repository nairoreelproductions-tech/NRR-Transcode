# NRR-Transcode

Workflow-only repo. Transcodes an uploaded review master into a watermarked streaming proxy
for the NairoReel client portal: the portal sends a `repository_dispatch`, this runs ffmpeg on
a GitHub runner, writes the proxy to R2, and calls the portal back (HMAC-signed).

- **This repo is the source of truth** for `.github/workflows/transcode.yml`. Edit it here.
- No portal code lives here. The portal side is `includes/transcode.php` in the portal repo,
  and the design record is `TRANSCODE-SETUP.md` / `CANONICAL.md` there.
- `assets/Boska-Black.ttf` is the wordmark font the burned-in slate uses.

## Test before you push

The workflow cannot be truly exercised without a real Actions run, so a local bench runs its
real `run:` bodies against a synthetic video with hostile project names:

    python tests/test_workflow.py            # must print ALL CHECKS PASSED
    python tests/test_workflow.py --frames   # also writes cropped frames of the burned text to look at

Needs python3 + PyYAML, bash, ffmpeg/ffprobe. Details, and how to prove the bench can fail,
are in the docstring of `tests/test_workflow.py`. Add a hostile case there whenever the
workflow starts consuming new payload text.

Rule for the workflow itself: payload values reach a shell only through `env:`, never as
`${{ }}` inside a `run:` body (see the comment at the top of `steps:`).
