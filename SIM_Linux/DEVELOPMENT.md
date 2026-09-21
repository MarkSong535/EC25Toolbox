# Development and release checklist

## Scope

Stage this directory only. The original macOS application is not replaced.
Unrelated untracked `vohive-release-*` directories are outside this port; do not
use `git add .` to sweep them into the commit. No generated binary, private
archive, real configuration, token, GPG key or database belongs in Git.

Dependencies are pinned in `requirements-portal.txt`. The original repository's
license and upstream driver/patch attribution must remain. SVG paths were supplied
by the operator from SVG Repo; verify their upstream license before redistribution
if required (the supplied snippets did not include original icon page/license URLs).

## Local checks

From `SIM_Linux`, with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-portal.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
for script in install.sh install-portal.sh install-voice.sh install-acme-cloudflare.sh; do
  sh -n "$script"
done
```

Optional browser regression (requires installed Google Chrome):

```sh
.venv/bin/pip install playwright
.venv/bin/python tests/browser_portal.py
```

Browser tests intercept requests into a Flask test app and use temporary messages;
no carrier SMS, production DB, Cloudflare policy or real Drive is changed. They
check literal hostile text, compose flow, archive links/colors, sidebar behavior,
keyboard dismissal, narrow heading alignment and exact Send icon/label. Screenshots
are written to the operating system's temporary directory, not committed.

The configuration tests load shipped examples with network features disabled
and test placeholder rejection. A passing template test does not validate live
secrets, filesystem/device permissions, carrier behavior or edge Access policies.

## Manual commit and push

The expected remote is `git@github.com:MarkSong535/EC25Toolbox.git` and local branch
is `main`. Before the first commit, review the staged diff and file list:

```sh
git remote -v
git status --short
git diff --cached --stat
git diff --cached --check
git diff --cached -- SIM_Linux
```

If the directory is not staged yet, stage only `SIM_Linux`. Once satisfied:

```sh
git commit -m "Add Raspberry Pi SMS portal, email forwarding and Proton archives"
git push
```

These are operator instructions, not commands run as part of preparing this
handoff. The existing `main -> origin/main` tracking plus `push.default=simple`
supports plain `git push`; GitHub SSH authorization and a compatible remote
history are still required. If rejected for divergence, inspect/fetch and reconcile
deliberately; never force-push by default. Remote configuration is local Git
metadata and is not included in the commit.

## Before publishing

- Confirm staged files contain only intended source, docs, tests and placeholders.
- Check for private-key blocks, tokens, real passwords, personal SMS and archives.
- Inspect the repository's existing history as well if making a private repo public;
  a clean new diff does not prove the historical repository contains no secrets.
- Run tests above and real installation acceptance checks separately.
- Keep deployment, account login and archive-upload verification status distinct.
- Do not copy local `/etc`, `/var/lib`, backup folders or a complete Pi home into Git.
