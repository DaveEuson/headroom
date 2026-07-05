# Smoke test — first run on the Pi

A quick checklist to get ClaudeTrackerPi running and confirm it works.
Takes ~15 minutes. You need the Pi, the Whisplay HAT + PiSugar attached,
and the computer where you normally use Claude Code.

## 1. Flash + boot the Pi

- Flash Raspberry Pi OS (Lite is plenty) with Raspberry Pi Imager.
- In the Imager's settings (gear icon) **before** writing: set a hostname
  (e.g. `raspberrypi`), enable SSH, and enter your Wi-Fi so it comes up
  on the network headless.
- Attach the Whisplay HAT and PiSugar, power on, wait ~60s for first boot.

## 2. Install on the Pi

SSH in from your computer (`ssh pi@raspberrypi.local`), then:

```bash
sudo apt update
git clone -b claude/pi-usage-tracker-6mnamz https://github.com/DaveEuson/ClaudeTrackerPi.git
cd ClaudeTrackerPi
./install.sh
sudo reboot
```

`install.sh` installs the display libraries, enables SPI (needed for the
HAT screen), and sets up the `claude-tracker` service to start on boot.
The reboot makes sure SPI is live. It prints your dashboard URL — note it.

## 3. Send your Claude credentials

On the **computer where you use Claude Code** (not the Pi), get this repo
and run the helper (works on macOS + Linux):

```bash
git clone -b claude/pi-usage-tracker-6mnamz https://github.com/DaveEuson/ClaudeTrackerPi.git
cd ClaudeTrackerPi
./scripts/send-credentials.sh pi@raspberrypi.local
```

Within a minute or two the meters go live.

## 4. Check it works

Tick these off:

- [ ] **Phone/laptop browser** → `http://raspberrypi.local:8080`
      (or the IP `install.sh` printed) shows the dashboard.
- [ ] **Meters** show real percentages that match `/usage` in Claude Code.
- [ ] **Clock** shows the right local time; **reset countdowns** look sane.
- [ ] **Battery tile** appears with a plausible % (needs PiSugar — see below).
- [ ] **Whisplay HAT screen** shows the compact dashboard + mascot.
- [ ] **Pip** animates and matches state (dances if you're mid-session).

Preview the other looks by adding to the URL:
`?night=1` (night mode + sleeping Pip), `?active=0` (idle/chilling),
`?active=1` (dancing).

## 5. If something's off

```bash
journalctl -u claude-tracker -f      # live logs on the Pi
sudo systemctl restart claude-tracker
```

| Symptom | Fix |
|---|---|
| Banner: "No Claude credentials found" | Re-run step 3 |
| Banner: "sign-in expired" | Re-run step 3 (refreshes the token) |
| Battery tile missing | Install PiSugar power manager: `wget https://cdn.pisugar.com/release/pisugar-power-manager.sh -O - \| sudo bash` |
| HAT screen stays black | Confirm SPI is on (`ls /dev/spidev*` should list a device); make sure PiSugar's own Whisplay demo/daemon isn't running (two programs can't share the screen) |
| HAT image shifted ~20px, mirrored, or colors inverted | Cosmetic init tweak — note exactly what you see and it's a one-line change in `app/display.py` |
| Web page loads but meters never fill | Check the logs; usually a credentials or network issue, shown in the top banner |

## Notes

- The web dashboard always runs even if the HAT isn't detected — the screen
  just switches itself off. So a blank HAT never blocks the phone view.
- Everything lives on the `claude/pi-usage-tracker-6mnamz` branch (the PR
  isn't merged yet), which is why the clone commands pin that branch.
