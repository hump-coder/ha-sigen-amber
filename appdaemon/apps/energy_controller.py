"""
Sigenergy + Amber Electric Energy Controller for Home Assistant (AppDaemon)
============================================================================
Controls a SigenStor ESS battery + solar system using Amber Electric live
and forecast pricing to maximise self-consumption, minimise costs, and
maximise export revenue.

Control logic priority (highest → lowest):
  1. FORCE_CHARGE   – Import price < 0: charge from grid, curtail export
  2. CHEAP_CHARGE   – Low solar day, battery < min SOC, price ≤ cheap threshold
  3. HOLD_BATTERY   – Low solar day, battery < min SOC, cheaper window ahead
  4. EXPORT_MAX     – Battery ≥ full threshold AND export price > 0
  5. SELF_CONSUME   – Export price > 0: solar→load→battery→export
  6. NO_EXPORT      – Export price ≤ 0: solar→load→battery, no grid export

All configuration values are read live from HA input_* helpers so changes
in the UI take effect at the next control cycle (~60 s) without restarting.

Dependencies:
  - AppDaemon 4.x
  - Sigenergy Local Modbus integration (TypQxQ/Sigenergy-Local-Modbus)
  - Amber Electric and/or Amber Express HA integrations
  - Solcast PV Forecast integration (BJReplay/ha-solcast-solar)
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timezone, timedelta
from enum import Enum


# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

class Mode(str, Enum):
    FORCE_CHARGE        = "Force Charge"         # Negative import price – grid charges battery
    CHEAP_CHARGE        = "Cheap Charge"         # Charging to meet minimum SOC at low price
    HOLD_BATTERY        = "Hold Battery"         # Preserving charge while awaiting cheap window
    EXPORT_MAX          = "Maximum Export"       # Battery full + good export price
    SELF_CONSUME        = "Self Consume"         # Normal operation with export enabled
    NO_EXPORT           = "No Export"            # Export price ≤ 0 – curtail all export
    # Manual override modes (set via dashboard buttons, stay until cleared)
    MAN_EXPORT_BATTERY  = "Manual: Export Battery"       # Force discharge + export
    MAN_STOP_BATT_EXP   = "Manual: Stop Battery Export"  # Solar export ok, no battery discharge
    MAN_CHARGE_GRID     = "Manual: Charge from Grid"     # Force charge from grid
    MAN_NO_EXPORT       = "Manual: No Export"            # No grid export at all
    MAN_SELF_CONSUME    = "Manual: Self Consume"         # Normal self-consume + export
    MAN_SOLAR_PRIORITY  = "Manual: Solar Priority"       # Solar+battery cover load, no import/export
    MANUAL              = "Manual Override"              # Hands-off (legacy boolean)
    ERROR               = "Error"                        # Sensor data unavailable


# Map input_select option strings → Mode enum
MANUAL_MODE_MAP = {
    "Export Battery":      Mode.MAN_EXPORT_BATTERY,
    "Stop Battery Export": Mode.MAN_STOP_BATT_EXP,
    "Charge from Grid":    Mode.MAN_CHARGE_GRID,
    "No Export":           Mode.MAN_NO_EXPORT,
    "Self Consume":        Mode.MAN_SELF_CONSUME,
    "Solar Priority":      Mode.MAN_SOLAR_PRIORITY,
}


# Sigenergy select entity option strings (from Sigenergy Local Modbus integration)
SIGEN_MODE_SELF_CONSUME   = "Maximum Self Consumption"
SIGEN_MODE_CHARGE_GRID    = "Command Charging (Grid First)"
SIGEN_MODE_CHARGE_PV      = "Command Charging (PV First)"
SIGEN_MODE_DISCHARGE_ESS  = "Command Discharging (ESS First)"
SIGEN_MODE_DISCHARGE_PV   = "Command Discharging (PV First)"
SIGEN_MODE_STANDBY        = "Standby"

# Mode icons for dashboard display
MODE_ICONS = {
    Mode.FORCE_CHARGE:       "⚡ CHARGING (FREE/PAID GRID)",
    Mode.CHEAP_CHARGE:       "🔋 CHEAP CHARGE",
    Mode.HOLD_BATTERY:       "⏳ HOLD BATTERY",
    Mode.EXPORT_MAX:         "📤 MAX EXPORT",
    Mode.SELF_CONSUME:       "☀️ SELF CONSUME + EXPORT",
    Mode.MAN_EXPORT_BATTERY: "🔧 MANUAL: EXPORT BATTERY",
    Mode.MAN_STOP_BATT_EXP:  "🔧 MANUAL: STOP BATTERY EXPORT",
    Mode.MAN_CHARGE_GRID:    "🔧 MANUAL: CHARGE FROM GRID",
    Mode.MAN_NO_EXPORT:      "🔧 MANUAL: NO EXPORT",
    Mode.MAN_SELF_CONSUME:   "🔧 MANUAL: SELF CONSUME",
    Mode.MAN_SOLAR_PRIORITY: "🔧 MANUAL: SOLAR PRIORITY",
    Mode.NO_EXPORT:     "🏠 SELF CONSUME ONLY",
    Mode.MANUAL:        "🔧 MANUAL OVERRIDE",
    Mode.ERROR:         "❌ ERROR – CHECK SENSORS",
}


class EnergyController(hass.Hass):
    """AppDaemon app that manages Sigenergy ESS based on Amber pricing."""

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        self.log("=== Energy Controller starting ===")
        self._last_mode: Mode | None = None
        self._consecutive_errors = 0

        # Daily cost/revenue accumulators – restored from persistent input_number helpers
        # so they survive AppDaemon restarts. Written back every control cycle.
        # Clamp values that are at the helper minimum (-9999) — indicates never initialised.
        def _restore(key: str) -> float:
            v = self._get_float(self.args.get(key, ""), 0.0)
            return 0.0 if abs(v) >= 9998 else v

        self._daily_import_cost    = _restore("daily_import_cost_entity")
        self._daily_export_revenue = _restore("daily_export_revenue_entity")
        self._daily_import_kwh     = _restore("daily_import_kwh_entity")
        self._daily_export_kwh     = _restore("daily_export_kwh_entity")
        self._last_cost_ts: datetime | None = None

        # Schedule main control loop
        interval = int(self.args.get("interval", 60))
        self.run_every(self.control_loop, "now+5", interval)

        # Reset accumulators at midnight each day
        self.run_daily(self._reset_daily_costs, "00:00:01")

        # Also trigger immediately on price changes for fast response
        for key in ("live_import_price_entity", "live_export_price_entity",
                    "amber_express_import_entity", "amber_express_export_entity"):
            entity = self.args.get(key, "")
            if entity:
                self.listen_state(self.on_price_change, entity,
                                  immediate=False)

        # Trigger on Solcast update
        solcast = self.args.get("solcast_today_entity", "")
        if solcast:
            self.listen_state(self.on_solar_update, solcast, immediate=False)

        self.log(f"Energy Controller initialised – polling every {interval}s")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_price_change(self, entity, attribute, old, new, kwargs):
        if old != new:
            self.log(f"Price update ({entity}): {old} → {new}")
            self.control_loop(kwargs)

    def on_solar_update(self, entity, attribute, old, new, kwargs):
        if old != new:
            self.log(f"Solar forecast updated: {old} → {new} kWh")
            self.control_loop(kwargs)

    # ------------------------------------------------------------------
    # Main control loop
    # ------------------------------------------------------------------

    def control_loop(self, kwargs):
        """Evaluate state and apply optimal operating mode."""
        try:
            # Manual override (boolean hands-off switch)
            if self._manual_override_active():
                self._publish_state(Mode.MANUAL, "Manual override is enabled – controller is hands off")
                self._last_mode = Mode.MANUAL
                return

            # Named manual mode buttons
            manual_mode = self._manual_mode_select()
            if manual_mode is not None:
                state = self._read_state()
                if state:
                    self._apply_mode(manual_mode, state)
                self._publish_state(manual_mode,
                    f"Manual override: {self.get_state(self.args.get('manual_mode_entity', ''))} "
                    f"– set to Auto to return to automatic control")
                if manual_mode != self._last_mode:
                    self.log(f"★ Mode: {self._last_mode} → {manual_mode.value} (manual)")
                    self._last_mode = manual_mode
                return

            state = self._read_state()
            if state is None:
                self._consecutive_errors += 1
                self._publish_state(Mode.ERROR,
                    f"Failed to read sensor data (attempt {self._consecutive_errors})")
                return

            self._consecutive_errors = 0
            mode, reason = self._determine_mode(state)
            self._apply_mode(mode, state)
            self._update_daily_costs(state)
            self._publish_state(mode, reason)
            self._publish_today_plan(state)
            self._publish_morning_soc_floor(state)
            self._publish_daily_costs()

            if mode != self._last_mode:
                self.log(f"★ Mode: {self._last_mode} → {mode.value} | {reason}")
                self._last_mode = mode

        except Exception as exc:  # noqa: BLE001
            self.log(f"Unexpected error in control_loop: {exc}", level="ERROR")

    # ------------------------------------------------------------------
    # State reading
    # ------------------------------------------------------------------

    def _read_state(self) -> dict | None:
        """Collect all sensor readings into a single state dict."""
        try:
            import_price = self._current_import_price()
            export_price = self._current_export_price()
            if import_price is None or export_price is None:
                self.log("Import or export price unavailable", level="WARNING")
                return None

            battery_soc = self._get_float(self.args["battery_soc_entity"])
            if battery_soc is None:
                self.log("Battery SOC unavailable", level="WARNING")
                return None

            return {
                "battery_soc":          battery_soc,
                "battery_power_kw":     self._get_float(self.args["battery_power_entity"], 0.0),
                "pv_power_kw":          self._get_float(self.args["pv_power_entity"], 0.0),
                "grid_power_kw":        self._get_float(self.args["grid_power_entity"], 0.0),
                "load_power_kw":        self._get_float(self.args["load_power_entity"], 0.0),
                "import_price":         import_price,      # $/kWh
                "export_price":         export_price,      # $/kWh
                "solar_forecast_kwh":   self._get_float(self.args.get("solcast_today_entity", ""), 0.0),
                "solar_remaining_kwh":  self._get_float(self.args.get("solcast_remaining_entity", ""), 0.0),
                "expected_load_kwh":    self._get_float(self.args["expected_load_entity"], 20.0),
                "max_export_kw":        self._get_float(self.args["max_export_kw_entity"], 10.0),
                "max_charge_kw":        self._get_float(self.args["max_charge_kw_entity"], 10.0),
                "max_discharge_kw":     self._get_float(self.args["max_discharge_kw_entity"], 10.0),
                "battery_min_soc":      self._get_float(self.args["battery_min_soc_entity"], 20.0),
                "export_min_soc":       self._get_float(self.args["export_min_soc_entity"], 20.0),
                "battery_full_pct":     self._get_float(self.args["battery_full_threshold_entity"], 95.0),
                "cheap_threshold_c":    self._get_float(self.args["cheap_threshold_entity"], 10.0),
                "min_export_price_c":   self._get_float(self.args["min_export_price_entity"], 0.0),
                "forecast_prices":      self._forecast_prices(),
                "forecast_export_prices": self._forecast_export_prices(),
            }
        except KeyError as exc:
            self.log(f"Missing apps.yaml key: {exc}", level="ERROR")
            return None

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _live_source(self) -> str:
        return self.get_state(self.args.get("live_source_entity", "")) or "Amber Electric"

    def _forecast_source(self) -> str:
        return self.get_state(self.args.get("forecast_source_entity", "")) or "Amber Electric"

    def _current_import_price(self) -> float | None:
        if self._live_source() == "Amber Express":
            entity = self.args.get("amber_express_import_entity",
                                   self.args.get("live_import_price_entity", ""))
        else:
            entity = self.args.get("live_import_price_entity", "")
        return self._get_float(entity)

    def _current_export_price(self) -> float | None:
        if self._live_source() == "Amber Express":
            entity = self.args.get("amber_express_export_entity",
                                   self.args.get("live_export_price_entity", ""))
        else:
            entity = self.args.get("live_export_price_entity", "")
        return self._get_float(entity)

    def _forecast_prices(self) -> list[dict]:
        """Return import price forecast list from the configured source."""
        if self._forecast_source() == "Amber Express":
            entity = self.args.get("amber_express_forecast_import_entity",
                                   self.args.get("forecast_import_entity", ""))
        else:
            entity = self.args.get("forecast_import_entity", "")
        return self._get_forecast_attribute(entity)

    def _forecast_export_prices(self) -> list[dict]:
        """Return export (feed-in) price forecast list from configured source."""
        if self._forecast_source() == "Amber Express":
            entity = self.args.get("amber_express_forecast_export_entity",
                                   self.args.get("forecast_export_entity", ""))
        else:
            entity = self.args.get("forecast_export_entity", "")
        return self._get_forecast_attribute(entity)

    def _get_forecast_attribute(self, entity: str) -> list[dict]:
        """Extract the 'forecasts' attribute from an Amber forecast sensor."""
        if not entity:
            return []
        raw = self.get_state(entity, attribute="forecasts")
        if isinstance(raw, list):
            return raw
        return []

    # ------------------------------------------------------------------
    # Mode determination
    # ------------------------------------------------------------------

    def _determine_mode(self, state: dict) -> tuple[Mode, str]:
        """Apply priority rules to select operating mode."""
        import_price      = state["import_price"]    # $/kWh
        export_price      = state["export_price"]    # $/kWh
        battery_soc       = state["battery_soc"]     # %
        cheap_thresh      = state["cheap_threshold_c"] / 100.0   # → $/kWh
        min_export_price  = state["min_export_price_c"] / 100.0  # → $/kWh

        # ── Priority 1: Negative import price → charge from grid ──────────
        if import_price < 0:
            return (Mode.FORCE_CHARGE,
                    f"Import price is negative ({import_price*100:.2f}c/kWh) "
                    f"– charging battery from grid, curtailing export")

        # ── Priority 2: Low solar day + battery below minimum SOC ─────────
        is_low_solar, solar_reason = self._is_low_solar_day(state)
        if is_low_solar and battery_soc < state["battery_min_soc"]:
            if import_price <= cheap_thresh:
                return (Mode.CHEAP_CHARGE,
                        f"Low solar day ({solar_reason}), battery {battery_soc:.0f}% "
                        f"< min {state['battery_min_soc']:.0f}%, price "
                        f"{import_price*100:.2f}c ≤ threshold {cheap_thresh*100:.0f}c")

            # Not cheap right now – is there a cheaper window coming?
            next_cheap = self._next_cheap_window(state["forecast_prices"], cheap_thresh)
            if next_cheap:
                return (Mode.HOLD_BATTERY,
                        f"Low solar day ({solar_reason}), battery {battery_soc:.0f}% "
                        f"< min, waiting for cheap window at {next_cheap}")
            else:
                # No cheap window ahead – charge now at current price as fallback
                return (Mode.CHEAP_CHARGE,
                        f"Low solar day ({solar_reason}), battery {battery_soc:.0f}% "
                        f"< min, no cheaper window ahead – charging now at "
                        f"{import_price*100:.2f}c/kWh")

        # ── Priority 3: Battery full + export price ≥ min → max export ────
        if battery_soc >= state["battery_full_pct"] and export_price >= min_export_price:
            return (Mode.EXPORT_MAX,
                    f"Battery full ({battery_soc:.0f}% ≥ {state['battery_full_pct']:.0f}%) "
                    f"and export price {export_price*100:.2f}c/kWh ≥ "
                    f"min {min_export_price*100:.2f}c/kWh – maximising export")

        # ── Priority 4: Export price ≥ min → self consume + export ─────────
        if export_price >= min_export_price:
            eff_min_soc, soc_reason = self._effective_export_min_soc(state)
            if battery_soc > eff_min_soc:
                action = "discharging battery + exporting"
            else:
                action = f"holding battery (SOC {battery_soc:.0f}% ≤ floor {eff_min_soc:.0f}%)"
            return (Mode.SELF_CONSUME,
                    f"Export price {export_price*100:.2f}c/kWh ≥ "
                    f"min {min_export_price*100:.2f}c/kWh – {action} | {soc_reason}")

        # ── Default: Export price below minimum → no export ────────────────
        return (Mode.NO_EXPORT,
                f"Export price {export_price*100:.2f}c/kWh < "
                f"min {min_export_price*100:.2f}c/kWh "
                f"– curtailing all export, self consuming only")

    def _is_low_solar_day(self, state: dict) -> tuple[bool, str]:
        """
        Fuzzy check: will today's solar likely be insufficient for house load?
        Returns (is_low, description_string).
        """
        forecast   = state["solar_forecast_kwh"]
        load       = state["expected_load_kwh"]
        if load <= 0:
            return False, "no expected load configured"

        # Apply a 15 % uncertainty margin to the solar forecast
        # (Solcast forecasts can be optimistic; this adds conservatism)
        uncertainty = 0.15
        effective_forecast = forecast * (1.0 - uncertainty)
        is_low = effective_forecast < load
        reason = (
            f"forecast {forecast:.1f} kWh × {1-uncertainty:.0%} = "
            f"{effective_forecast:.1f} kWh vs load {load:.1f} kWh"
        )
        return is_low, reason

    def _next_cheap_window(self, forecasts: list[dict], threshold: float) -> str | None:
        """
        Return human-readable time of next cheap import window, or None.
        Threshold is in $/kWh.
        """
        now = datetime.now(timezone.utc)
        for interval in forecasts:
            try:
                start_raw = interval.get("start_time", "")
                if not start_raw:
                    continue
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if start <= now:
                    continue
                price = float(interval.get("per_kwh", 9999))
                if price <= threshold:
                    # Format in local time
                    local_start = start.astimezone()
                    return local_start.strftime("%H:%M")
            except (ValueError, TypeError, KeyError):
                continue
        return None

    # ------------------------------------------------------------------
    # Mode application
    # ------------------------------------------------------------------

    def _apply_mode(self, mode: Mode, state: dict):
        """Write control commands to Sigenergy entities."""
        max_exp  = state["max_export_kw"]
        max_chg  = state["max_charge_kw"]
        max_dis  = state["max_discharge_kw"]
        exp_pos  = state["export_price"] > 0

        if mode == Mode.FORCE_CHARGE:
            self._set_sigen_mode(SIGEN_MODE_CHARGE_GRID)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(0.0)
            self._set_export_limits(0.0, max_exp)

        elif mode == Mode.CHEAP_CHARGE:
            self._set_sigen_mode(SIGEN_MODE_CHARGE_GRID)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(0.0)
            self._set_export_limits(0.0, max_exp)

        elif mode == Mode.HOLD_BATTERY:
            # Allow solar to charge battery but prevent discharge
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(0.0)
            self._set_export_limits(max_exp if exp_pos else 0.0, max_exp)

        elif mode == Mode.EXPORT_MAX:
            # Actively discharge battery + export solar
            self._set_sigen_mode(SIGEN_MODE_DISCHARGE_ESS)
            self._set_charge_limit(0.0)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(max_exp, max_exp)

        elif mode == Mode.SELF_CONSUME:
            eff_min_soc, _ = self._effective_export_min_soc(state)
            above_min_soc = state["battery_soc"] > eff_min_soc
            if above_min_soc:
                # Actively discharge battery: load covered first, remainder exported to grid
                self._set_sigen_mode(SIGEN_MODE_DISCHARGE_ESS)
                self._set_charge_limit(0.0)
                self._set_discharge_limit(max_dis)
                self._set_export_limits(max_exp, max_exp)
            else:
                # Below export min SOC: cover load from solar/battery but no grid export
                self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
                self._set_charge_limit(max_chg)
                self._set_discharge_limit(max_dis)
                self._set_export_limits(0.0, max_exp)

        elif mode == Mode.NO_EXPORT:
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(0.0, max_exp)

        elif mode == Mode.MAN_EXPORT_BATTERY:
            # Actively discharge battery to grid at full rate
            self._set_sigen_mode(SIGEN_MODE_DISCHARGE_ESS)
            self._set_charge_limit(0.0)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(max_exp, max_exp)

        elif mode == Mode.MAN_STOP_BATT_EXP:
            # Solar can still export but battery must not discharge to grid
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(0.0)
            self._set_export_limits(max_exp if exp_pos else 0.0, max_exp)

        elif mode == Mode.MAN_CHARGE_GRID:
            # Force charge from grid at max rate
            self._set_sigen_mode(SIGEN_MODE_CHARGE_GRID)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(0.0)
            self._set_export_limits(0.0, max_exp)

        elif mode == Mode.MAN_NO_EXPORT:
            # No grid export at all – self consume only
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(0.0, max_exp)

        elif mode == Mode.MAN_SELF_CONSUME:
            # Normal self consume with export allowed
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(max_exp, max_exp)

        elif mode == Mode.MAN_SOLAR_PRIORITY:
            # Maximum Self Consumption: solar → load → battery, no export.
            # Battery discharges only when solar is insufficient.
            self._set_sigen_mode(SIGEN_MODE_SELF_CONSUME)
            self._set_charge_limit(max_chg)
            self._set_discharge_limit(max_dis)
            self._set_export_limits(0.0, max_exp)

        # MANUAL (hands-off) and ERROR modes: do nothing, don't touch hardware

    # ------------------------------------------------------------------
    # Sigenergy entity writers
    # ------------------------------------------------------------------

    def _set_sigen_mode(self, option: str):
        entity = self.args.get("mode_select_entity", "")
        if not entity:
            return
        current = self.get_state(entity)
        if current == option:
            return
        self.log(f"  Sigenergy mode → {option}")
        self.call_service("select/select_option",
                          entity_id=entity, option=option)

    def _set_export_limits(self, grid_kw: float, pcs_kw: float):
        """Set grid-point and PCS export limits.
        grid_kw controls whether power reaches the grid.
        pcs_kw should always be max_exp – setting PCS to 0 curtails all solar
        including generation used for local load, not just grid export."""
        self._set_number(self.args.get("grid_export_limit_entity", ""), grid_kw, "grid export limit")
        pcs = self.args.get("pcs_export_limit_entity", "")
        if pcs:
            self._set_number(pcs, pcs_kw, "PCS export limit")

    def _set_charge_limit(self, kw: float):
        self._set_number(self.args.get("charge_limit_entity", ""), kw, "charge limit")

    def _set_discharge_limit(self, kw: float):
        self._set_number(self.args.get("discharge_limit_entity", ""), kw, "discharge limit")

    def _set_number(self, entity: str, value: float, label: str = ""):
        if not entity:
            return
        try:
            current = float(self.get_state(entity) or 0)
            if abs(current - value) < 0.05:
                return  # No change needed
        except (ValueError, TypeError):
            pass
        self.log(f"  {label} → {value} kW")
        self.call_service("number/set_value", entity_id=entity, value=round(value, 3))

    # ------------------------------------------------------------------
    # HA state publishing (virtual sensors)
    # ------------------------------------------------------------------

    def _publish_state(self, mode: Mode, reason: str):
        """Write current mode/reason to HA virtual sensors."""
        icon = MODE_ICONS.get(mode, "❓")
        self.set_state("sensor.energy_controller_mode",
                       state=mode.value,
                       attributes={
                           "icon": "mdi:battery-charging",
                           "friendly_name": "Energy Controller Mode",
                           "icon_label": icon,
                           "reason": reason,
                       })
        self.set_state("sensor.energy_controller_reason",
                       state=reason[:255],
                       attributes={
                           "friendly_name": "Energy Controller Reason",
                           "full_reason": reason,
                       })

    def _publish_today_plan(self, state: dict):
        """
        Build a today's action plan from the Amber price forecast and
        publish it as a virtual sensor with attributes for the dashboard.
        """
        forecasts = state["forecast_prices"]
        export_fc = state["forecast_export_prices"]
        cheap_thresh = state["cheap_threshold_c"] / 100.0
        full_pct     = state["battery_full_pct"]
        min_soc      = state["battery_min_soc"]

        # Build lookup: start_time → export price
        export_by_start: dict[str, float] = {}
        for ef in export_fc:
            key = ef.get("start_time", "")
            if key:
                export_by_start[key] = float(ef.get("per_kwh", 0))

        now_utc = datetime.now(timezone.utc)
        plan_rows: list[dict] = []
        markdown_rows: list[str] = []
        markdown_rows.append("| Time  | Import | Export | Action |")
        markdown_rows.append("|-------|--------|--------|--------|")

        is_low_solar, _ = self._is_low_solar_day(state)

        for interval in forecasts[:24]:  # Show max 24 hours
            try:
                start_raw = interval.get("start_time", "")
                if not start_raw:
                    continue
                start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if start_dt < now_utc - __import__("datetime").timedelta(minutes=30):
                    continue  # Skip past intervals

                import_p = float(interval.get("per_kwh", 0))
                export_p = export_by_start.get(start_raw, 0.0)
                local_t  = start_dt.astimezone().strftime("%H:%M")

                # Determine action for this time slot (price-only, no SOC sim)
                if import_p < 0:
                    action = "⚡ Force Charge"
                    colour = "blue"
                elif is_low_solar and import_p <= cheap_thresh:
                    action = "🔋 Cheap Charge"
                    colour = "cyan"
                elif export_p > 0:
                    action = "📤 Export / Self Consume"
                    colour = "green"
                else:
                    action = "🏠 Self Consume"
                    colour = "orange"

                plan_rows.append({
                    "time":     local_t,
                    "import_c": round(import_p * 100, 2),
                    "export_c": round(export_p * 100, 2),
                    "action":   action,
                    "colour":   colour,
                })
                markdown_rows.append(
                    f"| {local_t} | {import_p*100:+.1f}c | "
                    f"{export_p*100:+.1f}c | {action} |"
                )
            except (ValueError, TypeError, KeyError):
                continue

        plan_md = "\n".join(markdown_rows) if len(markdown_rows) > 2 else "No forecast data available"

        # Solar vs load summary
        solar_f = state["solar_forecast_kwh"]
        load_e  = state["expected_load_kwh"]
        surplus = solar_f - load_e
        if surplus >= 0:
            solar_summary = f"☀️ {solar_f:.1f} kWh forecast – {surplus:.1f} kWh surplus"
        else:
            solar_summary = f"☁️ {solar_f:.1f} kWh forecast – {abs(surplus):.1f} kWh deficit"

        self.set_state("sensor.energy_today_plan",
                       state=solar_summary,
                       attributes={
                           "friendly_name": "Today's Energy Plan",
                           "plan_markdown": plan_md,
                           "plan_data": plan_rows,
                           "solar_forecast_kwh": solar_f,
                           "expected_load_kwh":  load_e,
                           "surplus_kwh": round(surplus, 2),
                           "is_low_solar_day": is_low_solar,
                       })

    def _publish_morning_soc_floor(self, state: dict):
        """Publish the dynamic morning export SOC floor as a virtual sensor."""
        eff_min_soc, _ = self._effective_export_min_soc(state)   # uses cached stats
        m              = self._morning_solar_stats(state)         # uses cached stats
        can_export     = state["battery_soc"] > eff_min_soc

        morning_start = float(self.args.get("morning_solar_start_hour", 7.5))
        morning_end   = float(self.args.get("morning_solar_end_hour",   11.0))
        start_hhmm    = f"{int(morning_start):02d}:{int((morning_start % 1) * 60):02d}"
        end_hhmm      = f"{int(morning_end):02d}:{int((morning_end % 1) * 60):02d}"

        self.set_state(
            "sensor.energy_morning_soc_floor",
            state=f"{eff_min_soc:.0f}%",
            attributes={
                "friendly_name":               "Morning Export SOC Floor",
                "effective_min_soc":           round(eff_min_soc, 1),
                "base_min_soc":                state["export_min_soc"],
                "relax_factor":                round(m["relax_factor"], 2)      if m else 0.0,
                "morning_solar_kwh":           round(m["morning_solar_kwh"], 2) if m else 0.0,
                "morning_solar_effective_kwh": round(m["effective_solar"], 2)   if m else 0.0,
                "morning_load_kwh":            round(m["morning_load_kwh"], 2)  if m else 0.0,
                "surplus_kwh":                 round(m["surplus"], 2)           if m else 0.0,
                "can_export":                  can_export,
                "window_start":                start_hhmm,
                "window_end":                  end_hhmm,
            },
        )

    # ------------------------------------------------------------------
    # Daily cost / revenue tracking
    # ------------------------------------------------------------------

    def _reset_daily_costs(self, kwargs=None):
        """Called at midnight: copy today's totals to yesterday helpers, then zero today."""
        self.log("Rolling daily cost/revenue: today → yesterday, resetting today")
        pairs = [
            ("daily_import_cost_entity",    "yesterday_import_cost_entity",    self._daily_import_cost),
            ("daily_export_revenue_entity", "yesterday_export_revenue_entity", self._daily_export_revenue),
            ("daily_import_kwh_entity",     "yesterday_import_kwh_entity",     self._daily_import_kwh),
            ("daily_export_kwh_entity",     "yesterday_export_kwh_entity",     self._daily_export_kwh),
        ]
        for today_key, yesterday_key, value in pairs:
            y_entity = self.args.get(yesterday_key)
            if y_entity:
                self.call_service("input_number/set_value",
                                  entity_id=y_entity, value=round(value, 4))
            t_entity = self.args.get(today_key)
            if t_entity:
                self.call_service("input_number/set_value",
                                  entity_id=t_entity, value=0)

        self._daily_import_cost = 0.0
        self._daily_export_revenue = 0.0
        self._daily_import_kwh = 0.0
        self._daily_export_kwh = 0.0
        self._last_cost_ts = None

    def _update_daily_costs(self, state: dict):
        """Integrate grid power × price over the elapsed interval."""
        now = datetime.now(timezone.utc)
        if self._last_cost_ts is None:
            self._last_cost_ts = now
            return

        elapsed_h = (now - self._last_cost_ts).total_seconds() / 3600.0
        # Cap at 5 minutes to avoid inflating costs after a restart gap
        elapsed_h = min(elapsed_h, 5 / 60.0)
        self._last_cost_ts = now

        grid_kw = state["grid_power_kw"]   # + = import, – = export
        if grid_kw > 0:
            kwh = grid_kw * elapsed_h
            self._daily_import_kwh += kwh
            self._daily_import_cost += kwh * state["import_price"]
        elif grid_kw < 0:
            kwh = abs(grid_kw) * elapsed_h
            self._daily_export_kwh += kwh
            self._daily_export_revenue += kwh * state["export_price"]

    def _publish_daily_costs(self):
        """Persist accumulators to input_number helpers and publish virtual sensors."""
        net = self._daily_import_cost - self._daily_export_revenue

        # Persist to HA input_number helpers (survive AppDaemon restarts)
        persist = [
            ("daily_import_cost_entity",    self._daily_import_cost),
            ("daily_export_revenue_entity", self._daily_export_revenue),
            ("daily_import_kwh_entity",     self._daily_import_kwh),
            ("daily_export_kwh_entity",     self._daily_export_kwh),
        ]
        for key, value in persist:
            entity = self.args.get(key)
            if entity:
                self.call_service("input_number/set_value",
                                  entity_id=entity, value=round(value, 4))

        # Virtual sensors (for dashboard display and history graphs)
        self.set_state(
            "sensor.energy_daily_import_cost",
            state=round(self._daily_import_cost, 4),
            attributes={
                "friendly_name": "Daily Import Cost",
                "unit_of_measurement": "$",
                "device_class": "monetary",
                "import_kwh": round(self._daily_import_kwh, 3),
            },
        )
        self.set_state(
            "sensor.energy_daily_export_revenue",
            state=round(self._daily_export_revenue, 4),
            attributes={
                "friendly_name": "Daily Export Revenue",
                "unit_of_measurement": "$",
                "device_class": "monetary",
                "export_kwh": round(self._daily_export_kwh, 3),
            },
        )
        self.set_state(
            "sensor.energy_daily_net_cost",
            state=round(net, 4),
            attributes={
                "friendly_name": "Daily Net Cost",
                "unit_of_measurement": "$",
                "device_class": "monetary",
                "import_cost": round(self._daily_import_cost, 4),
                "export_revenue": round(self._daily_export_revenue, 4),
                "import_kwh": round(self._daily_import_kwh, 3),
                "export_kwh": round(self._daily_export_kwh, 3),
            },
        )

    # ------------------------------------------------------------------
    # Dynamic export SOC floor
    # ------------------------------------------------------------------

    def _morning_solar_stats(self, state: dict) -> dict | None:
        """
        Compute morning window solar/load stats, cached in the state dict so
        Solcast is read and parsed only once per control cycle regardless of
        how many callers need this data.

        Returns a dict of intermediate values, or None if Solcast data is
        unavailable or expected_load is unconfigured.
        """
        if "_morning_stats" in state:
            return state["_morning_stats"]

        morning_start     = float(self.args.get("morning_solar_start_hour", 7.5))
        morning_end       = float(self.args.get("morning_solar_end_hour",   11.0))
        morning_solar_kwh = self._solcast_window_kwh(morning_start, morning_end)
        morning_load_kwh  = state["expected_load_kwh"] * (morning_end - morning_start) / 24.0

        if morning_solar_kwh is None or morning_load_kwh <= 0:
            state["_morning_stats"] = None
            return None

        effective_solar = morning_solar_kwh * 0.85
        surplus         = effective_solar - morning_load_kwh
        relax_factor    = max(0.0, min(1.0, surplus / morning_load_kwh)) if surplus > 0 else 0.0

        result = {
            "morning_start":     morning_start,
            "morning_end":       morning_end,
            "morning_solar_kwh": morning_solar_kwh,
            "effective_solar":   effective_solar,
            "morning_load_kwh":  morning_load_kwh,
            "surplus":           surplus,
            "relax_factor":      relax_factor,
        }
        state["_morning_stats"] = result
        return result

    def _effective_export_min_soc(self, state: dict) -> tuple[float, str]:
        """
        Return the effective battery SOC floor for grid export.

        Outside the morning window the configured export_min_soc is returned
        unchanged.  Within the morning window (default 07:30–11:00) the floor
        is relaxed toward battery_min_soc in proportion to how much the
        Solcast morning-window solar forecast exceeds the morning load.

        The logic: if morning solar comfortably covers morning load we can
        afford to let the battery run lower during the price spike, because
        the subsequent solar will replenish it without needing to import.
        """
        base  = state["export_min_soc"]
        floor = state["battery_min_soc"]

        if floor >= base:
            return base, f"export min SOC {base:.0f}% (floor = min SOC)"

        morning_end  = float(self.args.get("morning_solar_end_hour",    11.0))
        relax_start  = float(self.args.get("morning_relax_start_hour", 22.0))
        now          = datetime.now().astimezone()
        hour         = now.hour + now.minute / 60.0

        # Relaxation window spans midnight: relax_start (e.g. 22:00) → morning_end (e.g. 11:00).
        # Outside that window solar is either generating (day) or the evening is too early to act.
        if not (hour >= relax_start or hour < morning_end):
            return base, f"export min SOC {base:.0f}% (outside relaxation window {relax_start:.0f}:00–{morning_end:.0f}:00)"

        m = self._morning_solar_stats(state)
        if m is None:
            return base, f"export min SOC {base:.0f}% (no Solcast data or load unconfigured)"

        if m["surplus"] <= 0:
            return base, (
                f"export min SOC {base:.0f}% – morning solar "
                f"{m['effective_solar']:.1f} kWh ≤ load {m['morning_load_kwh']:.1f} kWh, "
                f"no relaxation"
            )

        effective_min = round(base - m["relax_factor"] * (base - floor), 1)
        return effective_min, (
            f"export min SOC {effective_min:.0f}% (base {base:.0f}% → "
            f"floor {floor:.0f}% | morning solar {m['effective_solar']:.1f} kWh, "
            f"load {m['morning_load_kwh']:.1f} kWh, surplus {m['surplus']:.1f} kWh, "
            f"relax {m['relax_factor']:.0%})"
        )

    def _solcast_window_kwh(self, start_hour: float, end_hour: float) -> float | None:
        """
        Sum Solcast forecast kWh for a local-time window (e.g. 7.5 → 11.0).

        Tries the 'detailedForecast' attribute first (older BJReplay builds),
        then 'forecasts' (newer builds).  Each interval is assumed to be
        30 minutes; kWh = pv_estimate_kw × 0.5.
        """
        entity = self.args.get("solcast_today_entity", "")
        if not entity:
            return None

        intervals = self.get_state(entity, attribute="detailedForecast")
        if not isinstance(intervals, list):
            intervals = self.get_state(entity, attribute="forecasts")
        if not isinstance(intervals, list):
            return None

        total_kwh = 0.0
        for interval in intervals:
            try:
                start_raw = (interval.get("period_start")
                             or interval.get("start_time", ""))
                if not start_raw:
                    continue
                dt = datetime.fromisoformat(start_raw).astimezone()
                h  = dt.hour + dt.minute / 60.0
                if start_hour <= h < end_hour:
                    kw = float(interval.get("pv_estimate",
                               interval.get("per_kwh", 0)))
                    total_kwh += kw * 0.5   # 30-min period → kWh
            except (ValueError, TypeError, KeyError):
                continue

        return total_kwh

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _manual_override_active(self) -> bool:
        entity = self.args.get("manual_override_entity", "")
        if not entity:
            return False
        return self.get_state(entity) == "on"

    def _manual_mode_select(self) -> "Mode | None":
        """Return the active manual Mode if input_select is not 'Auto', else None."""
        entity = self.args.get("manual_mode_entity", "")
        if not entity:
            return None
        value = self.get_state(entity)
        return MANUAL_MODE_MAP.get(value)  # None if "Auto" or unrecognised

    def _get_float(self, entity_id: str, default: float | None = None) -> float | None:
        """Safely read an entity state as float. Returns default (or None) on error."""
        if not entity_id:
            return default
        try:
            val = self.get_state(entity_id)
            if val in (None, "unknown", "unavailable", ""):
                return default
            return float(val)
        except (ValueError, TypeError):
            return default
