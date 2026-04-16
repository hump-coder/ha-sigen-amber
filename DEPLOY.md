# Deployment Guide – ha-sigen-amber

This file documents exactly how to deploy files from this repo to the Home Assistant
instance. Reference this in every session before making changes.

## Target System

| Item | Value |
|------|-------|
| HA hostname | `homeassistant.local` |
| SSH user | `karl` (key at `~/.ssh/id_rsa`) |
| HA config root | `/config/` |
| AppDaemon config | `/addon_configs/a0d7b954_appdaemon/` |
| AppDaemon apps dir | `/addon_configs/a0d7b954_appdaemon/apps/` |
| Dashboards dir | `/config/dashboards/` |
| Packages dir | `/config/packages/` |

## SSH Access

```bash
ssh homeassistant.local        # works directly, key already trusted
```

## File Map – Repo → HA

| Repo path | HA path | Restart needed? |
|-----------|---------|-----------------|
| `packages/energy_controller.yaml` | `/config/packages/energy_controller.yaml` | HA restart |
| `appdaemon/apps/energy_controller.py` | `/addon_configs/a0d7b954_appdaemon/apps/energy_controller.py` | AppDaemon restart |
| `appdaemon/apps/apps.yaml` | `/addon_configs/a0d7b954_appdaemon/apps/apps.yaml` | AppDaemon restart |
| `dashboards/energy_control.yaml` | `/config/dashboards/energy_control.yaml` | Hard refresh only |
| `dashboards/energy_override_buttons.yaml` | `/config/dashboards/energy_override_buttons.yaml` | Hard refresh only |

## Deploy Commands

Files cannot be written directly via SSH (root-owned dirs). Always copy to `/tmp/` first,
then `sudo cp` to destination.

### Deploy a single dashboard file (no restart needed)
```bash
scp dashboards/energy_control.yaml homeassistant.local:/tmp/ && \
ssh homeassistant.local 'sudo cp /tmp/energy_control.yaml /config/dashboards/energy_control.yaml'
```

### Deploy the HA package (requires HA restart)
```bash
scp packages/energy_controller.yaml homeassistant.local:/tmp/pkg.yaml && \
ssh homeassistant.local 'sudo cp /tmp/pkg.yaml /config/packages/energy_controller.yaml'
```
Then restart HA: **Settings → System → Restart**

### Deploy AppDaemon app (requires AppDaemon restart)
```bash
scp appdaemon/apps/energy_controller.py homeassistant.local:/tmp/ && \
ssh homeassistant.local 'sudo cp /tmp/energy_controller.py /addon_configs/a0d7b954_appdaemon/apps/energy_controller.py'
```
Then restart AppDaemon: **Settings → Apps → AppDaemon → Restart**

### Deploy apps.yaml (merge with existing hello_world entry)
```bash
scp appdaemon/apps/apps.yaml homeassistant.local:/tmp/apps_new.yaml && \
ssh homeassistant.local '
  head -4 /addon_configs/a0d7b954_appdaemon/apps/apps.yaml > /tmp/apps_merged.yaml
  cat /tmp/apps_new.yaml >> /tmp/apps_merged.yaml
  sudo cp /tmp/apps_merged.yaml /addon_configs/a0d7b954_appdaemon/apps/apps.yaml
'
```
Then restart AppDaemon.

### Deploy everything at once
```bash
scp packages/energy_controller.yaml homeassistant.local:/tmp/pkg.yaml
scp appdaemon/apps/energy_controller.py homeassistant.local:/tmp/energy_controller.py
scp appdaemon/apps/apps.yaml homeassistant.local:/tmp/apps_new.yaml
scp dashboards/energy_control.yaml homeassistant.local:/tmp/energy_control.yaml
scp dashboards/energy_override_buttons.yaml homeassistant.local:/tmp/energy_override_buttons.yaml

ssh homeassistant.local '
  sudo cp /tmp/pkg.yaml /config/packages/energy_controller.yaml
  sudo cp /tmp/energy_controller.py /addon_configs/a0d7b954_appdaemon/apps/energy_controller.py
  sudo cp /tmp/energy_control.yaml /config/dashboards/energy_control.yaml
  sudo cp /tmp/energy_override_buttons.yaml /config/dashboards/energy_override_buttons.yaml
  head -4 /addon_configs/a0d7b954_appdaemon/apps/apps.yaml > /tmp/apps_merged.yaml
  cat /tmp/apps_new.yaml >> /tmp/apps_merged.yaml
  sudo cp /tmp/apps_merged.yaml /addon_configs/a0d7b954_appdaemon/apps/apps.yaml
'
```
Then restart both HA and AppDaemon.

## Checking Logs

### AppDaemon log (most useful for controller debugging)
Check via: **Settings → Apps → AppDaemon → Log**

### HA config validation (before restarting)
Not easily available via SSH (protection mode blocks docker access).
Use **Developer Tools → Check Configuration** in the HA UI instead,
or just restart and check **Settings → System → Logs**.

## Key HA Entities to Know

### Must be enabled in HA before controller can write to them
Go to **Settings → Devices & Services → Sigenergy → your device** and enable:
- `number.sigen_plant_ess_max_charging_limit`
- `number.sigen_plant_ess_max_discharging_limit`
- `number.sigen_plant_grid_export_limitation`
- `number.sigen_plant_pcs_export_limitation`
- `select.sigen_plant_remote_ems_control_mode`

### Must be ON for controller to send commands to inverter
- `switch.sigen_plant_remote_ems_controlled_by_home_assistant`

## Adding Override Buttons to Another Dashboard

Add this single line anywhere in that dashboard's `cards:` list:
```yaml
- !include energy_override_buttons.yaml
```
The file `/config/dashboards/energy_override_buttons.yaml` must already be deployed.

## Git Workflow

All changes should be committed and pushed:
```bash
git add <files>
git commit -m "Description

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```
Remote: `git@github.com:hump-coder/ha-sigen-amber.git`
