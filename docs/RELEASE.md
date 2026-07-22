# Release checklist

How to cut a Headroom release. Tags drive everything: pushing `vX.Y.Z` builds
the companion apps **and** the Mini firmware image and attaches them to a
GitHub Release; the setup page + companion download links always point at
`releases/latest`.

## What the automation does

| Workflow | Trigger | Produces |
|---|---|---|
| `firmware.yml` | push/PR touching `firmware/**` | compile-check + firmware artifacts (CI gate) |
| `release.yml` | push tag `v*` | `HeadroomCompanion-{windows.exe,macos,linux}` + `headroom-mini-merged.bin`, attached to the Release |
| `pages.yml` | push to `main` touching `docs/**` | deploys the setup/flasher page to GitHub Pages |

Fixed URLs the site depends on (resolve once a Release exists):
- Flasher image: `https://github.com/DaveEuson/headroom/releases/latest/download/headroom-mini-merged.bin`
- Companion: `.../releases/latest/download/HeadroomCompanion-{windows.exe,macos,linux}`
- Setup page: `https://daveeuson.github.io/headroom/`

## One-time setup (first release only)

- [ ] **GitHub → Settings → Pages → Source = "GitHub Actions."** Without this,
      `pages.yml` has nothing to publish to and the flasher page never goes live.

## Every release

1. [ ] **Bump the version.** Firmware `FW_VERSION` + `UA` in
       `firmware/src/main.cpp`; companion `USER_AGENT` in
       `companion/companion.py` if it changed. Keep them in step with the tag.
2. [ ] **Green CI on the branch** — `firmware.yml` must be passing (it is the
       only pre-tag compile check for the firmware).
3. [ ] **Merge the PR into `main`.** This fires `pages.yml`, which redeploys the
       setup page. (It does *not* build binaries — only the tag does.)
4. [ ] **Push the tag** from `main`:
       ```
       git checkout main && git pull
       git tag v1.4.0 && git push origin v1.4.0
       ```
       `release.yml` builds the three companion apps + the merged firmware image
       and creates Release `v1.4.0` with them attached. (Tags are pushed by a
       human — the sandbox can't.)
5. [ ] **Watch `release.yml` go green** and confirm the Release has **4 assets**:
       three `HeadroomCompanion-*` and `headroom-mini-merged.bin`.
6. [ ] **Smoke test the retail path** in Chrome/Edge:
       - Open `https://daveeuson.github.io/headroom/`, click **Connect &
         Install**, flash a board.
       - Same window → **Connect to Wi-Fi** (Improv) → board joins.
       - On the board's screen open `/connect` (self-hosted) and `/alerts`
         (send-test push).
       - Download a companion binary from the page and confirm it feeds a board.

## Release notes template

```
## Headroom v1.4.0

### Headroom Mini (ESP32-S3) — first full firmware
- Browser flasher (ESP Web Tools) + Wi-Fi over USB (Improv) — no VS Code/CLI.
- Self-contained on-device usage polling (/connect) — no companion needed.
- Touch (cycle screens, % used/left, brightness) + motion (face-down dim,
  shake wake), battery gauge, usage-history graph, phone push alerts (/alerts).

### Companion
- Multi-device push (comma-separated --pi), single-instance lock, live-usage
  backoff.
```

## Rollback

Releases are immutable; to ship a fix, tag a new patch (`v1.4.1`). The setup
page and companion links track `releases/latest`, so a new Release moves users
forward automatically — nothing else to update.
