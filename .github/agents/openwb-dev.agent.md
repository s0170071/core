---
description: "openWB EV charger development — backend, UI, device deployment"
tools:
  - search
  - editFiles
  - web
  - terminalLastCommand
  - runInTerminal
  - sendToTerminal
  - getTerminalOutput
  - readFile
  - listDirectory
  - replaceinFile
  - createFile
  - agent
---

# openWB Development Agent

You are an expert on the openWB EV charging controller codebase. You work across two repos and deploy to a Raspberry Pi device.

## Device & Environment

- **Device:** Raspberry Pi 3B+ at `192.168.1.20`, user `openwb`, SSH access configured
- **OS:** Debian Linux, Python 3.9.2, pytest 6.2.5
- **openWB version:** 2.1.9-Patch.2, Release branch, commit `f34d6752c`
- **Service:** `openwb2.service` (systemd), runs `packages/main.py`
- **MQTT broker:** mosquitto on device, ports 1883 (MQTT), 9001 (websocket), 1884 (local bridge, no ACL)
- **Web server:** Apache on port 80, proxies `/ws` → `ws://localhost:9001`

## Repository Layout

### Backend — `C:\Users\Toby\Documents\GitHub\openWB`

```
packages/
  main.py                          # entry point
  control/
    process.py                     # charging logic, _update_state() sets EVSE current
    chargepoint/                   # chargepoint models, state machine
    ev/
      ev.py                        # Ev class + EvData (per-vehicle: name, tag_id, current_offset)
      ev_template.py               # EvTemplate + EvTemplateData (brand-level: min/max current, phases)
      charge_template.py           # ChargeTemplate (chargemode, scheduled charging)
    counter_all.py, bat_all.py     # load management
  helpermodules/                   # utils, pub, update_config
  modules/                         # hardware drivers (chargepoints, inverters, vehicles)
data/config/                       # runtime config, services, cron
runs/                              # shell scripts (backup, restore, update)
```

### UI — `C:\Users\Toby\Documents\GitHub\openwb-ui-settings`

```
src/
  App.vue                          # root component, MQTT connection (ws://host:80/ws)
  main.js
  router/index.js                  # Vue Router with beforeEach access guard
  store/index.js                   # Vuex store, MQTT state, accessAllowed getter
  views/
    VehicleConfiguration.vue       # vehicle + template settings
    GeneralConfiguration.vue       # general settings
    ...
  components/
    OpenwbPageBlocker.vue          # boot_done / update_in_progress blocker modal
    OpenwbPageUser.vue             # login / user management
```

- **Stack:** Vue 3.5, Vuex, Vite 7, Bootstrap
- **Build:** `npm install && npx vite build` (output in `dist/`)
- **Base path:** `/openWB/web/settings/` (configured in `vite.config.js`)

## Key Data Flow

1. `EvData` (per-vehicle) stores `current_offset`, `name`, `tag_id`, `charge_template`, `ev_template` indices
2. `EvTemplateData` (brand-level) stores `min_current`, `max_current_*`, `prevent_phase_switch`, `nominal_difference`, `bidi`
3. `Process._update_state()` reads `chargepoint.data.set.charging_ev_data` (type `Ev`) to get both
4. MQTT topics: `openWB/vehicle/{id}/current_offset` (per-vehicle), `openWB/vehicle/template/ev_template/{id}` (template)
5. UI subscribes via wildcard `openWB/vehicle/+/current_offset` etc.

## Deployment Workflow

### Backend files
```
scp packages/control/ev/ev.py openwb@192.168.1.20:/var/www/html/openWB/packages/control/ev/ev.py
# repeat for each changed file
# Restart service for changes to take effect:
ssh openwb@192.168.1.20 "sudo systemctl restart openwb2"
```

### UI build & deploy
```powershell
cd C:\Users\Toby\Documents\GitHub\openwb-ui-settings
npx vite build
ssh openwb@192.168.1.20 "rm -rf /var/www/html/openWB/web/settings/assets/*"
scp -r dist/assets/* openwb@192.168.1.20:/var/www/html/openWB/web/settings/assets/
scp dist/index.html openwb@192.168.1.20:/var/www/html/openWB/web/settings/index.html
```

### Run tests
```
ssh openwb@192.168.1.20 "cd /var/www/html/openWB && python3 -m pytest packages/ -v --ignore=packages/modules"
```

## Completed Edits

### `current_offset` feature (per-vehicle EVSE current compensation)

**Files changed:**
- `packages/control/ev/ev.py` — added `current_offset: float = 0` to `EvData`
- `packages/control/process.py` — offset logic in `_update_state()` after phase-switch guards, before hardware write
- `packages/control/process_test.py` — 7 parametrized tests (no offset, +/- offset, min clamp, max clamp, zero skip, phase-switch skip)
- `openwb-ui-settings/src/views/VehicleConfiguration.vue` — number input in per-vehicle section, MQTT topic `openWB/vehicle/+/current_offset`

### UI build fix (`state.examples` undefined in production)

- `openwb-ui-settings/src/store/index.js` — added `examples: {}` to initial state to prevent `'in' operator` crash on `updateTopic` in production builds

### MQTT access topics

- Published retained `openWB/system/security/access/{PageName}` = `true` for all 13 routes via port 1884 (the default port 1883 ACL blocks writes to `openWB/system/` for anonymous clients)

## Known Issues & Gotchas

- **ACL:** Port 1883/9001 ACL only allows anonymous writes to `openWB/set/#`. Use port 1884 (local bridge, no ACL) for publishing to `openWB/system/` topics
- **`per_listener_settings true`:** Each mosquitto listener has its own ACL — port 1884 has none
- **Vite build hashes:** Each build produces new asset hashes; always deploy both `assets/` and `index.html` together, and delete old assets first
- **Browser cache:** After UI deploy, hard-refresh (`Ctrl+Shift+R`) is required
- **`state.examples`:** Only populated in dev mode; production builds need the empty `{}` default to avoid `'in' operator` TypeError
- **Node.js:** v24.15.0 LTS installed on Windows via `winget install OpenJS.NodeJS.LTS`
- **pymodbus:** Device requires v2.5.2 (`pip install pymodbus==2.5.2`), not v3.x
- **Tests:** Run on device only (dependencies like pymodbus, /proc/cpuinfo not available on Windows)
- **UI repo branch:** Use `main` but check for incompatible PRs (e.g. user-management merge at `5d1fb7e` adds access guards the backend doesn't support). Current working commit: `ba457f7`
