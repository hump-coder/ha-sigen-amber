# Sigenergy + Amber Electric Energy Controller

AppDaemon-based energy management for:
- **Hardware**: SigenStor EC 10.0 SP AU + 48 kWh battery + 28 kW solar
- **Provider**: Amber Electric (South Australia)
- **Platform**: Home Assistant

## Control Logic

| Priority | Mode | Trigger | Action |
|----------|------|---------|--------|
| 1 | **Force Charge** | Import price < 0 c/kWh | Charge from grid at max rate, no export |
| 2 | **Cheap Charge** | Low solar forecast + battery < min SOC + price ≤ threshold | Charge from grid to min SOC |
| 3 | **Hold Battery** | Low solar day + battery < min SOC + cheaper window ahead | Preserve battery, wait for cheap window |
| 4 | **Maximum Export** | Battery ≥ full threshold AND export price > 0 | Discharge battery + export solar at max rate |
| 5 | **Self Consume** | Export price > 0 | Solar → load → battery → export |
| 6 | **No Export** | Export price ≤ 0 | Solar → load → battery, no grid export |

**Low solar day** = Solcast today forecast × 85% < expected daily house load (15% uncertainty margin).

## Directory Structure

```
HA_SigenAmber/
├── appdaemon/
│   ├── apps/
│   │   └── energy_controller.py    ← AppDaemon app (main logic)
│   └── apps.yaml                   ← AppDaemon configuration
└── homeassistant/
    ├── packages/
    │   └── energy_controller.yaml  ← HA helpers + template sensors + automations
    └── dashboards/
        └── energy_control.yaml     ← Lovelace dashboard
```

## Installation

### Prerequisites

1. **AppDaemon** – Install as a HA add-on (Supervisor → Add-on Store → AppDaemon 4)
2. **Sigenergy Local Modbus** – [TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus) via HACS
3. **Amber Electric** – Built-in HA integration (Settings → Integrations → Amber Electric)
4. **Amber Express** *(optional)* – [hass-energy/amber-express](https://github.com/hass-energy/amber-express) via HACS
5. **Solcast PV Forecast** – [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) via HACS

### Step 1 – HA Package

Add to `configuration.yaml`:
```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copy `homeassistant/packages/energy_controller.yaml` to your HA `config/packages/` directory.

Restart Home Assistant.

### Step 2 – Enable Sigenergy Control Entities

The control entities in the Sigenergy integration are **disabled by default**.

Enable each of these in HA (Settings → Devices → your Sigenergy device → enable the entity):
- `select.sigen_plant_remote_ems_control_mode`
- `number.sigen_plant_grid_point_maximum_export_limitation`
- `number.sigen_plant_pcs_maximum_export_limitation`
- `number.sigen_plant_ess_max_charging_limit`
- `number.sigen_plant_ess_max_discharging_limit`

Also ensure **Remote EMS Control is enabled** on your Sigenergy plant (via the Sigenergy app or web interface). The `select` entity will not accept commands otherwise.

### Step 3 – Verify Entity IDs

Your entity IDs may differ depending on what name you gave the Sigenergy plant during setup.

In HA → Developer Tools → States, filter for `sigen` and note the actual entity IDs.
Update `appdaemon/apps.yaml` if they differ from the defaults.

For Amber, filter for `the_hump` (or your site name) and verify the entity IDs.

### Step 4 – AppDaemon

Copy both files to your AppDaemon apps directory:
- `appdaemon/apps/energy_controller.py`
- `appdaemon/apps.yaml` (merge with existing if you have one)

Restart AppDaemon. Check the AppDaemon log for:
```
Energy Controller initialised – polling every 60s
```

### Step 5 – Dashboard

In HA → Settings → Dashboards → Add Dashboard.
Paste the contents of `homeassistant/dashboards/energy_control.yaml` into the raw configuration editor.

### Step 6 – Configure Parameters

Open the **Configuration** tab on the dashboard and set:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Expected Daily House Load | 20 kWh | Typical daily consumption |
| Max Export Power | 10 kW | Grid connection / inverter export limit |
| Max Charge Power | 10 kW | Maximum battery charge rate |
| Max Discharge Power | 10 kW | Maximum battery discharge rate |
| Battery Minimum SOC | 20 % | Trigger cheap-charge when below this |
| Battery Full Threshold | 95 % | Trigger maximum export above this |
| Cheap Import Threshold | 10 c/kWh | Max price for opportunistic grid charging |

## Entity ID Reference

### Sigenergy (TypQxQ/Sigenergy-Local-Modbus)

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.sigen_plant_ess_soc` | Sensor | Battery state of charge % |
| `sensor.sigen_plant_ess_power` | Sensor | Battery power kW (+ charge, − discharge) |
| `sensor.sigen_plant_sigen_photovoltaic_power` | Sensor | Solar PV output kW |
| `sensor.sigen_plant_grid_sensor_active_power` | Sensor | Grid power kW (+ import, − export) |
| `sensor.sigen_plant_total_load_power` | Sensor | House load kW |
| `select.sigen_plant_remote_ems_control_mode` | Control | Operating mode |
| `number.sigen_plant_grid_point_maximum_export_limitation` | Control | Grid export limit kW |
| `number.sigen_plant_pcs_maximum_export_limitation` | Control | PCS export limit kW |
| `number.sigen_plant_ess_max_charging_limit` | Control | Max charge rate kW |
| `number.sigen_plant_ess_max_discharging_limit` | Control | Max discharge rate kW |

### Sigenergy Operating Modes

| Mode | Behaviour |
|------|-----------|
| `Maximum Self Consumption` | Solar → load → battery → export. Normal self-consume. |
| `Command Charging (Grid First)` | Force charge battery from grid. |
| `Command Charging (PV First)` | Charge battery from solar preferentially. |
| `Command Discharging (ESS First)` | Actively discharge battery (to load/grid). |
| `Command Discharging (PV First)` | Discharge via solar path first. |
| `Standby` | No active management. |

### Amber Electric (built-in integration, site name = "the_hump")

| Entity | Description |
|--------|-------------|
| `sensor.the_hump_general_price` | Current import price $/kWh |
| `sensor.the_hump_feed_in_price` | Current export (feed-in) price $/kWh |
| `sensor.the_hump_general_forecast` | Import price forecast (attribute: `forecasts`) |
| `sensor.the_hump_feed_in_forecast` | Export price forecast (attribute: `forecasts`) |
| `binary_sensor.the_hump_price_spike` | True when price spike active |
| `binary_sensor.the_hump_demand_window` | True during demand window |
| `sensor.the_hump_renewables` | Grid renewable % |

Forecast `forecasts` attribute is a list of dicts: `{start_time, end_time, per_kwh, spot_per_kwh, renewables, descriptor, ...}`

### Solcast (BJReplay/ha-solcast-solar)

| Entity | Description |
|--------|-------------|
| `sensor.solcast_pv_forecast_forecast_today` | Total PV forecast for today kWh |
| `sensor.solcast_pv_forecast_forecast_remaining_today` | Remaining forecast kWh today |
| `sensor.solcast_pv_forecast_forecast_this_hour` | This hour's forecast Wh |
| `sensor.solcast_pv_forecast_forecast_next_hour` | Next hour's forecast Wh |

## Troubleshooting

**Controller shows "Error – Check Sensors"**
- Check AppDaemon log for the specific entity that returned `unavailable`
- Verify entity IDs in `apps.yaml` match your actual HA entities

**Sigenergy mode not changing**
- Confirm Remote EMS Control is enabled on the plant (Sigenergy app → Plant Settings)
- Confirm the `select` entity is enabled in HA
- Check AppDaemon log for "select/select_option" call errors

**Export limit not working**
- Both `grid_export_limit_entity` and `pcs_export_limit_entity` are set – check both are enabled in HA
- Note: setting the limit to 0 curtails export; the inverter will curtail solar MPPT if it cannot send power anywhere

**Cheap charge not triggering**
- Check `sensor.solcast_pv_forecast_forecast_today` is returning a value
- Verify `input_number.energy_expected_daily_house_load` is set
- Lower the `energy_cheap_import_threshold` if prices aren't reaching your threshold
