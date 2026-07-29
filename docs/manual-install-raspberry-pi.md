# Manual Install — Daze Wallbox on Raspberry Pi

Instructions for manually installing the Daze Wallbox Home Assistant integration on a Raspberry Pi.

---

## Prerequisites

- Raspberry Pi running **Home Assistant OS** or **Home Assistant Supervised**
- SSH access or Samba share enabled
- [Daze web portal](https://webportal.dazeservice.com) account with **Access Token** and **Refresh Token**

---

## Step 1 — Copy the integration folder

Pick one method:

### Option A — Clone directly on the Pi (SSH)

```bash
ssh pi@<your-pi-ip>

cd /config   # HA OS / HA Supervised

git clone https://github.com/andrea/daze-addon.git /tmp/daze-addon
cp -r /tmp/daze-addon/custom_components/daze ./custom_components/
rm -rf /tmp/daze-addon
```

### Option B — SCP from a Mac

```bash
# From the machine that has the repo
scp -r /path/to/daze-addon/custom_components/daze/ \
  pi@<your-pi-ip>:/config/custom_components/daze/
```

### Option C — Samba share (HA OS)

1. Install and enable the **Samba** add-on in Home Assistant
2. Browse to `\\ha\config` (Windows) or `//ha/config` (macOS/Linux)
3. Navigate to `custom_components/` (create if missing)
4. Copy the `daze/` folder into it

---

## Step 2 — Verify file structure

```
/config/custom_components/daze/
├── __init__.py
├── manifest.json
├── config_flow.py
├── const.py
├── coordinator.py
├── sensor.py
├── switch.py
├── number.py
├── select.py
├── services.yaml
├── translations/
│   └── en.json
└── strings.json
```

---

## Step 3 — Restart Home Assistant

```bash
# Via SSH (HA OS):
ha core restart

# Or via web UI:
# Settings → System → Restart
```

---

## Step 4 — Configure

1. Go to **Settings → Devices & services**
2. Click **Add integration** → search for **Daze Wallbox**
3. Enter your **Access Token** and **Refresh Token**
4. Select your network → confirm → done

---

## Updating

Manual installs don't auto-update. To upgrade:

```bash
# Re-run the copy steps above, then restart
```

Or switch to HACS later for automatic updates.
