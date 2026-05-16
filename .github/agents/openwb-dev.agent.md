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

## Repository & Branch Structure

### Git Setup

- **Origin:** `https://github.com/openWB/core` (upstream)
- **Fork:** `https://github.com/s0170071/core.git` (remote name: `fork`)
- **Active branch:** `dev/2.1.9-all-fixes` — based on upstream `f34d6752c` (v2.1.9-Patch.2) with all custom edits
- **Device tracks:** `fork` remote, branch `dev/2.1.9-all-fixes`
- **Deploy script:** `deployall.bat` / `deployall.ps1` — diffs local vs device, deploys changed files, restarts, health-checks

### Backend — `C:\Users\Toby\Documents\GitHub\openWB`

```
.github/agents/
  openwb-dev.agent.md              # this file — Copilot agent customization
deployall.bat                      # deploy launcher
deployall.ps1                      # deploy logic: diff, scp, restart, health-check
packages/
  main.py                          # entry point
  conftest.py                      # shared pytest fixtures
  control/
    process.py                     # charging logic, _update_state() sets EVSE current
    process_test.py                # tests for current_offset feature
    bat_all.py                     # battery aggregate controller (state machine: BAT_MODE/EV_MODE/MIN_SOC_BAT)
    bat_all_test.py                # battery tests
    counter.py                     # EVU counter, surplus calculation
    chargepoint/                   # chargepoint models, state machine
      chargepoint.py               # Chargepoint class
      chargepoint_data.py          # Set/Get dataclasses
    ev/
      ev.py                        # Ev class + EvData (per-vehicle: name, tag_id, current_offset)
      ev_template.py               # EvTemplate + EvTemplateData (brand-level: min/max current, phases)
      charge_template.py           # ChargeTemplate (chargemode, scheduled charging)
    counter_all.py                 # counter aggregation, load management
    algorithm/                     # surplus, min_current, additional_current logic
  helpermodules/                   # utils, pub, update_config, setdata, subdata
    main.py                        # alternative entry point with enhanced logging
    logger.py                      # logging configuration
  modules/                         # hardware drivers (chargepoints, inverters, vehicles)
    common/
      evse.py                      # EVSE register read/write, phase switching
      configurable_device.py       # device abstraction
    internal_chargepoint_handler/
      chargepoint_module.py        # internal CP with relay safety
      internal_chargepoint_handler.py
      logger.py                    # dedicated chargepoint logger
      relay_safety.py              # relay safety monitoring
    chargepoints/
      openwb_series2_satellit/     # satellite chargepoint driver
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
6. `BatAll._get_charging_power_left()` → state machine (PROTECT/PRIORITY/ASSIST) → `charging_power_left` → `counter.calc_surplus()` / `calc_raw_surplus()`
7. **Phase switching data flow:** `bat_all.power_for_bat_charging()` → `counter.calc_surplus()` → `counter.get_usable_surplus()` → `ev._check_phase_switch_conditions()` → `ev.auto_phase_switch()` → `chargepoint.set_phases()`
8. **Phase switching decision in `chargepoint.get_phases_by_selected_chargemode()`:** runs every tick BEFORE the algorithm. For PV mode (`phases_chargemode == 0`), defaults to 1-phase when not charging. When charging, uses `phases_in_use`. Preserves `control_parameter.phases` in `CHARGING_STATES` to prevent overwriting algorithm decisions.

## Deployment Workflow

### Automated deploy (preferred)
```powershell
.\deployall.bat
# Diffs local branch vs device, shows changed files, asks confirmation
# Deploys via SCP, restarts openwb2, waits 45s, checks for errors
# Reports: DEPLOY OK or DEPLOY COMPLETED WITH ERRORS
```

### Manual backend files
```
scp packages/control/ev/ev.py openwb@192.168.1.20:/var/www/html/openWB/packages/control/ev/ev.py
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

### Battery state machine rework (`bat_all.py`)

- Removed dead `hysteresis_discharge` field
- Removed `i_term_power` integral controller (superseded by state machine)
- Fixed ASSIST formula: `discharge_rate + min(0, power)` (prevents inflation when battery is charging)
- Fixed `get_power_limit()` loop: `remaining_power_limit -= power_limit` (multi-battery support)
- Fixed corrupted line in `counter.py` (`raw_power_left` was split by stale i_term insertion)

### UI build fix (`state.examples` undefined in production)

- `openwb-ui-settings/src/store/index.js` — added `examples: {}` to initial state to prevent `'in' operator` crash on `updateTopic` in production builds

### Phase switch oscillation fix (`ev.py`, `chargepoint.py`)

**Root cause:** `_check_phase_switch_conditions()` in `ev.py` used `max(min_current, required_current)` for the 3→1 threshold. During PV charging, `required_current` tracks the algorithm's target (~9A at 3-phase), making `min_current_range = 9 + nominal_difference(2) = 11`. With the car charging at 9A × 3 phases, any transient surplus dip triggered a premature 3→1 switch. After switching to 1-phase, surplus recovered (less power used), triggering 1→3 again — creating a ~5-minute oscillation cycle.

**Fix:** Changed to use only `control_parameter.min_current` (template minimum, typically 6A). The 3→1 switch now only triggers when current is actually near the hardware minimum AND surplus is negative — meaning the algorithm has already reduced current as far as possible.

**Also fixed:** `get_phases_by_selected_chargemode()` in `chargepoint.py` — added guard for `CHARGING_STATES` with `control_parameter.phases > 1` to prevent the every-tick phase recalculation from overwriting the algorithm's phase commitment after a successful switch.

**Files changed:**
- `packages/control/ev/ev.py` — `_check_phase_switch_conditions()` threshold fix
- `packages/control/chargepoint/chargepoint.py` — `get_phases_by_selected_chargemode()` CHARGING_STATES guard

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
- **Device SSH key:** `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOOSp6X70Xgj6bTUFzOP84MT5Di8doc0ST8dEvHi45bb` — not registered with GitHub (push from Windows instead)
- **PowerShell stderr:** Native commands writing to stderr cause `NativeCommandError` when `$ErrorActionPreference = 'Stop'` — wrap SSH calls with `$ErrorActionPreference = 'Continue'`
- **Phase switch oscillation pattern:** If logs show repeated `Umschaltung von 1 auf 3` / `Umschaltung von 3 auf 1` every ~5 min, check `_check_phase_switch_conditions()` thresholds. The 3→1 condition uses `min_current + nominal_difference` — if this is set too high (e.g. by using `required_current` instead of `min_current`), surplus transients cause premature switches
- **`get_phases_by_selected_chargemode` runs every tick:** This method determines `phases_to_use` BEFORE the algorithm runs. For PV mode, it must preserve phase commitments made by the algorithm/switch-on logic. States to guard: all `CHARGING_STATES`, not just `WAIT_FOR_USING_PHASES`
- **Battery changes don't affect phase switching:** `bat_all` changes (ASSIST formula, i_term removal) feed into surplus via `power_for_bat_charging()` → `calc_surplus()` → `get_usable_surplus()`. The ASSIST fix (`discharge_rate + min(0, power)`) makes surplus more conservative, reducing oscillation risk
