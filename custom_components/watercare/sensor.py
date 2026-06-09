"""Watercare sensors."""

from datetime import datetime, timedelta
import logging
import json
import pytz

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
    StatisticMeanType,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)

from .const import (
    DOMAIN,
    NZ_TIMEZONE,
    SENSOR_NAME,
    CONF_CONSUMPTION_RATE,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    CONF_ANNUAL_LINE_CHARGE,
    CONF_ENDPOINT,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_ENDPOINT,
    ENDPOINT_DISPLAY_NAMES,
    STATISTIC_TYPES,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=12)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
):
    """Set up the Watercare sensor platform."""

    if "api" not in hass.data[DOMAIN]:
        _LOGGER.error("API instance not found in config entry data.")
        return False

    api = hass.data[DOMAIN]["api"]

    # Get rates and endpoint from config entry data
    consumption_rate = entry.data.get(CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE)
    wastewater_rate = entry.data.get(CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE)
    wastewater_ratio = entry.data.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO)
    annual_line_charge = entry.data.get(
        CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE
    )
    endpoint = entry.data.get(CONF_ENDPOINT, DEFAULT_ENDPOINT)

    # Check for updated values in options
    if entry.options:
        consumption_rate = entry.options.get(CONF_CONSUMPTION_RATE, consumption_rate)
        wastewater_rate = entry.options.get(CONF_WASTEWATER_RATE, wastewater_rate)
        wastewater_ratio = entry.options.get(CONF_WASTEWATER_RATIO, wastewater_ratio)
        annual_line_charge = entry.options.get(
            CONF_ANNUAL_LINE_CHARGE, annual_line_charge
        )
        endpoint = entry.options.get(CONF_ENDPOINT, endpoint)

    sensor = WatercareUsageSensor(
        SENSOR_NAME,
        api,
        consumption_rate,
        wastewater_rate,
        wastewater_ratio,
        annual_line_charge,
        endpoint,
    )
    # Expose the sensor so the import-history button/service can drive it.
    hass.data[DOMAIN]["sensor"] = sensor
    async_add_entities([sensor], True)


class WatercareUsageSensor(SensorEntity):
    """Define Watercare Usage sensor."""

    def __init__(
        self,
        name,
        api,
        consumption_rate,
        wastewater_rate,
        wastewater_ratio,
        annual_line_charge,
        endpoint,
    ):
        """Initialize Watercare Usage sensor."""
        self._name = name
        self._icon = "mdi:water"
        self._state = None
        self._unit_of_measurement = "L"
        self._unique_id = DOMAIN
        self._device_class = "water"
        self._state_class = "total_increasing"
        self._state_attributes = {}
        self._api = api
        self._consumption_rate = consumption_rate
        self._wastewater_rate = wastewater_rate
        self._wastewater_ratio = wastewater_ratio
        self._annual_line_charge = annual_line_charge
        self._endpoint = endpoint

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def icon(self):
        """Icon to use in the frontend, if any."""
        return self._icon

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        return self._state_attributes

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def state_class(self):
        """Return the state class."""
        return self._state_class

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def unique_id(self):
        """Return the unique id."""
        return self._unique_id

    def _calculate_cost(self, usage_litres, numberOfDays):
        """Calculate the total cost based on usage and configured rates."""
        usage_thousands = usage_litres / 1000.0

        # Calculate cost components
        consumption_cost = usage_thousands * self._consumption_rate
        wastewater_cost = (
            usage_thousands * self._wastewater_rate * self._wastewater_ratio
        )
        line_charge = (self._annual_line_charge / 365) * numberOfDays
        total_cost = consumption_cost + wastewater_cost + line_charge

        return {
            "total": total_cost,
            "consumption": consumption_cost,
            "wastewater": wastewater_cost,
            "line_charge": DEFAULT_ANNUAL_LINE_CHARGE / 365,
        }

    def _get_statistic_name(self, statistic_type: str) -> str:
        """Generate consistent statistic names based on endpoint and type."""
        endpoint_name = ENDPOINT_DISPLAY_NAMES.get(
            self._endpoint, self._endpoint.title()
        )
        type_name = STATISTIC_TYPES.get(statistic_type, statistic_type.title())
        return f"Watercare {endpoint_name} {type_name}"

    async def _get_last_cumulative(self, statistic_id):
        """Return (last_start_timestamp, last_sum) for a statistic id.

        Returns (None, 0.0) if nothing is stored yet. Used to continue the
        cumulative sum from what's already in the recorder rather than
        recomputing from zero each poll (which, with a trailing fetch window,
        makes the running total drop at the window edge and produces negative
        days on the Energy Dashboard).
        """
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        rows = last.get(statistic_id)
        if not rows:
            return None, 0.0
        return rows[0]["start"], rows[0].get("sum") or 0.0

    async def _build_cumulative_statistics(
        self,
        points,
        consumption_id,
        cost_id,
        consumption_cost_id,
        wastewater_cost_id,
        anchor=True,
    ):
        """Build cumulative StatisticData lists from chronological points.

        ``points`` is an iterable of ``(start_datetime, litres, number_of_days)``
        ordered oldest-first. When ``anchor`` is True (normal polling) each
        series continues from the value already stored for its statistic id and
        points at or before the last stored timestamp are skipped, so the running
        sums only ever grow and never reset. When False (a full history import)
        the sums are recomputed from zero over all points, overwriting the series.
        """
        if anchor:
            last_start, consumption_sum = await self._get_last_cumulative(
                consumption_id
            )
            _, cost_sum = await self._get_last_cumulative(cost_id)
            _, consumption_cost_sum = await self._get_last_cumulative(
                consumption_cost_id
            )
            _, wastewater_cost_sum = await self._get_last_cumulative(
                wastewater_cost_id
            )
        else:
            last_start = None
            consumption_sum = cost_sum = consumption_cost_sum = wastewater_cost_sum = 0.0

        consumption_stats = []
        cost_stats = []
        consumption_cost_stats = []
        wastewater_cost_stats = []

        for start, litres, number_of_days in points:
            if last_start is not None and start.timestamp() <= last_start:
                continue  # already stored in a previous poll

            consumption_sum += litres
            breakdown = self._calculate_cost(litres, number_of_days)
            cost_sum += breakdown["total"]
            consumption_cost_sum += breakdown["consumption"]
            wastewater_cost_sum += breakdown["wastewater"]

            consumption_stats.append(StatisticData(start=start, sum=consumption_sum))
            cost_stats.append(StatisticData(start=start, sum=cost_sum))
            if self._consumption_rate > 0:
                consumption_cost_stats.append(
                    StatisticData(start=start, sum=consumption_cost_sum)
                )
            if self._wastewater_rate > 0:
                wastewater_cost_stats.append(
                    StatisticData(start=start, sum=wastewater_cost_sum)
                )

        return (
            consumption_stats,
            cost_stats,
            consumption_cost_stats,
            wastewater_cost_stats,
        )

    def _statistic_ids(self):
        """Return (consumption, cost, consumption_cost, wastewater_cost) ids."""
        if self._endpoint == "dailywithstats":
            return (
                f"{DOMAIN}:daily_consumption",
                f"{DOMAIN}:daily_cost",
                f"{DOMAIN}:daily_consumption_cost",
                f"{DOMAIN}:daily_wastewater_cost",
            )
        return (
            f"{DOMAIN}:water_consumption",
            f"{DOMAIN}:water_cost",
            f"{DOMAIN}:consumption_cost",
            f"{DOMAIN}:wastewater_cost",
        )

    @staticmethod
    def _parse_period_start(timestamp_str, mode):
        """Parse an API timestamp into an NZ-localised period start."""
        if not timestamp_str:
            return None
        try:
            ts = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        except (ValueError, TypeError):
            return None
        ts = pytz.utc.localize(ts).astimezone(NZ_TIMEZONE)
        if mode == "day":
            return datetime.strptime(ts.strftime("%Y-%m-%d"), "%Y-%m-%d").replace(
                tzinfo=NZ_TIMEZONE
            )
        return ts.replace(minute=0, second=0, microsecond=0)

    def _accumulate_history(self, response, buckets):
        """Aggregate one chunk of API data into {period_start: litres}."""
        try:
            data = json.loads(response)
        except (TypeError, json.JSONDecodeError):
            return
        if self._endpoint == "halfhourly":
            for entry in data:
                start = self._parse_period_start(entry.get("timestamp"), "hour")
                if start is not None:
                    buckets[start] = buckets.get(start, 0) + entry.get("litres", 0)
        elif self._endpoint == "monthly":
            for entry in data:
                start = self._parse_period_start(entry.get("timestamp"), "hour")
                if start is not None:
                    buckets[start] = entry.get("litres", 0)
        elif self._endpoint == "dailywithstats":
            for entry in data.get("usage", []):
                start = self._parse_period_start(entry.get("timestamp"), "day")
                if start is not None:
                    buckets[start] = buckets.get(start, 0) + entry.get("litres", 0)

    async def async_import_history(self, start=None):
        """Backfill historical statistics from `start` (or install date) to now.

        Fetches the full history in API-sized chunks, rebuilds one continuous
        cumulative series from zero and imports it (overwriting), establishing a
        clean baseline that subsequent anchored polls extend. Safe to re-run.
        """
        if self._endpoint == "mechanicalmonthly":
            _LOGGER.info(
                "History import not applicable for mechanicalmonthly "
                "(a normal update already returns all billing periods)"
            )
            return

        if not await self._api.async_ensure_authenticated():
            _LOGGER.error("Watercare history import: authentication failed")
            return

        end = datetime.now(NZ_TIMEZONE)
        if start is None:
            start = self._api.install_date or (end - timedelta(days=730))
        if start.tzinfo is None:
            start = pytz.utc.localize(start)
        start = start.astimezone(NZ_TIMEZONE)

        # Only halfhourly is dense enough to hit the API's row cap; daily and
        # monthly fit any realistic range in a single call.
        chunk = (
            timedelta(days=150)
            if self._endpoint == "halfhourly"
            else timedelta(days=3650)
        )
        _LOGGER.info(
            "Watercare history import (%s) from %s to %s",
            self._endpoint,
            start.date(),
            end.date(),
        )

        buckets = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + chunk, end)
            response = await self._api.get_data(self._endpoint, cursor, chunk_end)
            if response:
                self._accumulate_history(response, buckets)
            cursor = chunk_end

        if not buckets:
            _LOGGER.warning("Watercare history import: no data returned")
            return

        ordered = sorted(buckets)
        if self._endpoint == "monthly":
            points = [
                (
                    ordered[i],
                    buckets[ordered[i]],
                    max((ordered[i] - ordered[i - 1]).days, 1) if i > 0 else 30,
                )
                for i in range(len(ordered))
            ]
        elif self._endpoint == "halfhourly":
            points = [(k, buckets[k], 1 / 24) for k in ordered]
        else:  # dailywithstats
            points = [(k, buckets[k], 1) for k in ordered]

        stats = await self._build_cumulative_statistics(
            points, *self._statistic_ids(), anchor=False
        )
        if self._endpoint == "dailywithstats":
            self._add_daily_statistics(*stats)
        else:
            self._add_water_statistics(*stats)
        _LOGGER.info(
            "Watercare history import complete: %d points imported", len(points)
        )

    async def async_update(self):
        """Update the sensor data."""
        _LOGGER.debug(f"Beginning sensor update using endpoint: {self._endpoint}")
        response = await self._api.get_data(endpoint=self._endpoint)

        # Route to appropriate processing method based on endpoint
        if self._endpoint == "dailywithstats":
            await self.process_daily_data(response)
        elif self._endpoint == "halfhourly":
            await self.process_halfhourly_data(response)
        elif self._endpoint == "monthly":
            await self.process_monthly_data(response)
        else:
            # For mechanicalmonthly - use the billing period processing
            await self.process_data(response)

    async def process_data(self, response):
        """Process the API response."""
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return

        try:
            billing_periods = json.loads(response)
        except (TypeError, json.JSONDecodeError) as err:
            _LOGGER.error("Failed to parse Watercare API response: %s", err)
            return

        _LOGGER.debug(f"Processing data: {billing_periods}")

        if not billing_periods:
            _LOGGER.warning("No billing periods found")
            return

        # Get the most recent billing period for current usage
        latest_period = billing_periods[0]
        daily_average = latest_period.get("statistics", {}).get("dailyAverage", 0)

        # Set the sensor state to cumulative usage for Energy Dashboard
        billing_period_usage = latest_period.get("waterUsage", 0)
        self._state = billing_period_usage

        numberOfDays = (
            datetime.strptime(
                latest_period.get("billingPeriodToDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            - datetime.strptime(
                latest_period.get("billingPeriodFromDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        ).days + 1

        cost_breakdown = self._calculate_cost(billing_period_usage, numberOfDays)

        self._state_attributes = {
            "billing_period_usage": billing_period_usage,
            "daily_average": daily_average,
            "billing_period_from": latest_period.get("billingPeriodFromDate"),
            "billing_period_to": latest_period.get("billingPeriodToDate"),
            "reading_type": latest_period.get("readingType"),
            "account_balance": latest_period.get("accountBalance"),
            "amount_due": latest_period.get("amountDue"),
            "household_efficiency_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("currentHouseholdBand"),
            "usage_to_lower_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("usageToLowerBand"),
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        # Generate external statistics for Energy Dashboard
        await self.generate_statistics(billing_periods)

    def _add_water_statistics(
        self,
        consumption_statistics,
        cost_statistics,
        consumption_cost_statistics,
        wastewater_cost_statistics,
    ):
        """Write the watercare:water_* external statistics.

        Shared by the process_data family of endpoints (mechanicalmonthly,
        monthly, halfhourly), matching upstream's behaviour of pooling those
        into watercare:water_consumption. The dailywithstats endpoint keeps its
        own watercare:daily_* ids and is not routed through here.
        """
        if consumption_statistics:
            _LOGGER.debug(
                f"Adding {len(consumption_statistics)} water consumption statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Water Consumption",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:water_consumption",
                    unit_of_measurement=self._unit_of_measurement,
                    mean_type=StatisticMeanType.NONE,
                    unit_class="volume",
                ),
                consumption_statistics,
            )
        else:
            _LOGGER.warning("No valid consumption statistics generated")

        if cost_statistics:
            _LOGGER.debug(f"Adding {len(cost_statistics)} water cost statistics")
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Total Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:water_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                cost_statistics,
            )

        if consumption_cost_statistics and self._consumption_rate > 0:
            _LOGGER.debug(
                f"Adding {len(consumption_cost_statistics)} consumption cost statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Consumption Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:consumption_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                consumption_cost_statistics,
            )

        if wastewater_cost_statistics and self._wastewater_rate > 0:
            _LOGGER.debug(
                f"Adding {len(wastewater_cost_statistics)} wastewater cost statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Wastewater Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:wastewater_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                wastewater_cost_statistics,
            )

    async def generate_statistics(self, billing_periods):
        """Generate external statistics from billing period data following Energy Dashboard pattern."""
        if not billing_periods:
            return

        # Build chronological (start, litres, number_of_days) points.
        points = []
        sorted_periods = sorted(
            billing_periods, key=lambda x: x.get("billingPeriodToDate", "")
        )
        for period in sorted_periods:
            end_date_str = period.get("billingPeriodToDate")
            if not end_date_str:
                continue
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                end_date = pytz.utc.localize(end_date).astimezone(NZ_TIMEZONE)
                number_of_days = (
                    datetime.strptime(end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                    - datetime.strptime(
                        period.get("billingPeriodFromDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                ).days + 1
            except (ValueError, TypeError) as e:
                _LOGGER.warning(f"Failed to parse date {end_date_str}: {e}")
                continue
            points.append((end_date, period.get("waterUsage", 0), number_of_days))

        (
            period_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        ) = await self._build_cumulative_statistics(
            points,
            f"{DOMAIN}:water_consumption",
            f"{DOMAIN}:water_cost",
            f"{DOMAIN}:consumption_cost",
            f"{DOMAIN}:wastewater_cost",
        )

        self._add_water_statistics(
            period_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        )

    async def process_halfhourly_data(self, response):
        """Process the half-hourly usage data.

        The halfhourly endpoint returns a flat list of readings:
            [{"timestamp": "2026-05-29T12:00:00.000Z", "litres": 4}, ...]

        Home Assistant long-term statistics must start on the hour, so the
        30-minute readings are aggregated into hourly buckets before being
        pushed as external statistics.
        """
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return

        try:
            readings = json.loads(response)
        except (TypeError, json.JSONDecodeError) as err:
            _LOGGER.error("Failed to parse Watercare API response: %s", err)
            return

        if not readings:
            _LOGGER.warning("No half-hourly readings found")
            return

        # Aggregate the half-hourly readings into hourly buckets (NZ time).
        hourly_consumption = {}
        for entry in readings:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)
            if not timestamp_str:
                continue
            try:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Failed to parse timestamp %s: %s", timestamp_str, err)
                continue
            timestamp = pytz.utc.localize(timestamp).astimezone(NZ_TIMEZONE)
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly_consumption[hour_start] = (
                hourly_consumption.get(hour_start, 0) + litres
            )

        if not hourly_consumption:
            _LOGGER.warning("No valid half-hourly readings to process")
            return

        # Set the sensor state to the most recent day's total consumption.
        daily_totals = {}
        for hour_start, litres in hourly_consumption.items():
            date_str = hour_start.strftime("%Y-%m-%d")
            daily_totals[date_str] = daily_totals.get(date_str, 0) + litres
        latest_date = max(daily_totals)
        self._state = daily_totals[latest_date]

        cost_breakdown = self._calculate_cost(self._state, 1)
        self._state_attributes = {
            "latest_day": latest_date,
            "latest_day_consumption": self._state,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        # Generate hourly statistics for the Energy Dashboard.
        # One hour is 1/24 of a day for the line-charge portion of cost.
        points = [
            (hour_start, hourly_consumption[hour_start], 1 / 24)
            for hour_start in sorted(hourly_consumption)
        ]
        (
            hour_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        ) = await self._build_cumulative_statistics(
            points,
            f"{DOMAIN}:water_consumption",
            f"{DOMAIN}:water_cost",
            f"{DOMAIN}:consumption_cost",
            f"{DOMAIN}:wastewater_cost",
        )

        self._add_water_statistics(
            hour_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        )

    async def process_monthly_data(self, response):
        """Process the monthly usage data.

        The monthly endpoint returns a flat list of monthly totals:
            [{"timestamp": "2026-02-28T11:00:00.000Z", "litres": 8465,
              "numberOfMissingDays": 6, "statistics": {...}}, ...]

        Each entry is one calendar month, so the readings map directly onto
        monthly external statistics.
        """
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return

        try:
            months = json.loads(response)
        except (TypeError, json.JSONDecodeError) as err:
            _LOGGER.error("Failed to parse Watercare API response: %s", err)
            return

        if not months:
            _LOGGER.warning("No monthly readings found")
            return

        # Parse and sort the months chronologically.
        parsed_months = []
        for entry in months:
            timestamp_str = entry.get("timestamp")
            if not timestamp_str:
                continue
            try:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Failed to parse timestamp %s: %s", timestamp_str, err)
                continue
            timestamp = pytz.utc.localize(timestamp).astimezone(NZ_TIMEZONE)
            month_start = timestamp.replace(minute=0, second=0, microsecond=0)
            parsed_months.append((month_start, entry))

        if not parsed_months:
            _LOGGER.warning("No valid monthly readings to process")
            return

        parsed_months.sort(key=lambda item: item[0])

        # Set the sensor state to the most recent month's usage.
        latest_start, latest_entry = parsed_months[-1]
        self._state = latest_entry.get("litres", 0)

        latest_stats = latest_entry.get("statistics", {})
        efficiency = latest_stats.get("efficiency", {})
        cost_breakdown = self._calculate_cost(
            self._state, self._month_length_days(parsed_months, len(parsed_months) - 1)
        )
        self._state_attributes = {
            "latest_month": latest_start.strftime("%Y-%m"),
            "latest_month_consumption": self._state,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "current_period_average": latest_stats.get("currentPeriodAverage"),
            "difference_to_previous_period": latest_stats.get(
                "differenceToPreviousPeriod"
            ),
            "current_household_band": efficiency.get("currentHouseholdBand"),
            "usage_to_lower_band": efficiency.get("usageToLowerBand"),
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        # Generate monthly statistics for the Energy Dashboard.
        points = [
            (
                month_start,
                entry.get("litres", 0),
                self._month_length_days(parsed_months, index),
            )
            for index, (month_start, entry) in enumerate(parsed_months)
        ]
        (
            month_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        ) = await self._build_cumulative_statistics(
            points,
            f"{DOMAIN}:water_consumption",
            f"{DOMAIN}:water_cost",
            f"{DOMAIN}:consumption_cost",
            f"{DOMAIN}:wastewater_cost",
        )

        self._add_water_statistics(
            month_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        )

    @staticmethod
    def _month_length_days(parsed_months, index):
        """Estimate the number of days a month spans for line-charge costs."""
        if index > 0:
            days = (parsed_months[index][0] - parsed_months[index - 1][0]).days
            if days > 0:
                return days
        if len(parsed_months) > index + 1:
            days = (parsed_months[index + 1][0] - parsed_months[index][0]).days
            if days > 0:
                return days
        return 30

    async def process_daily_data(self, response):
        """Process the daily data."""
        try:
            parsed_data = json.loads(response)
        except json.JSONDecodeError:
            _LOGGER.error("Failed to parse JSON response for dailywithstats endpoint")
            return

        _LOGGER.debug(f"Parsed data: {parsed_data}")
        usage_data = parsed_data.get("usage", [])
        statistic_data = parsed_data.get("statistics", {})

        daily_consumption = {}

        for entry in usage_data:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)
            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            timestamp = pytz.utc.localize(timestamp).astimezone(NZ_TIMEZONE)
            date_str = timestamp.strftime("%Y-%m-%d")

            daily_consumption[date_str] = daily_consumption.get(date_str, 0) + litres

        _LOGGER.debug(f"Daily consumption: {daily_consumption}")

        # Assign yesterday's consumption to state
        yesterday_date = (datetime.now(NZ_TIMEZONE) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        yesterday_consumption = daily_consumption.get(yesterday_date, 0)
        self._state = yesterday_consumption
        _LOGGER.debug(f"yesterday_consumption: {yesterday_consumption}")

        # Calculate cost for yesterday's consumption
        cost_breakdown = self._calculate_cost(yesterday_consumption, 1)

        efficiency_data = statistic_data.get("efficiency", {})
        self._state_attributes = {
            "yesterday_consumption": yesterday_consumption,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
            "account_balance": parsed_data.get("accountBalance"),
            "amount_due": parsed_data.get("amountDue"),
            "reading_type": parsed_data.get("readingType"),
            "currentPeriodAverage": statistic_data.get("currentPeriodAverage"),
            "differenceToPreviousPeriod": statistic_data.get(
                "differenceToPreviousPeriod"
            ),
            "currentHouseholdBand": efficiency_data.get("currentHouseholdBand"),
            "usageToLowerBand": efficiency_data.get("usageToLowerBand"),
        }

        # Generate statistics for daily data
        points = [
            (
                datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=NZ_TIMEZONE),
                litres,
                1,
            )
            for date, litres in sorted(daily_consumption.items())
        ]
        (
            day_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        ) = await self._build_cumulative_statistics(
            points,
            f"{DOMAIN}:daily_consumption",
            f"{DOMAIN}:daily_cost",
            f"{DOMAIN}:daily_consumption_cost",
            f"{DOMAIN}:daily_wastewater_cost",
        )

        self._add_daily_statistics(
            day_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        )

    def _add_daily_statistics(
        self,
        consumption_statistics,
        cost_statistics,
        consumption_cost_statistics,
        wastewater_cost_statistics,
    ):
        """Write the watercare:daily_* external statistics (dailywithstats)."""
        if consumption_statistics:
            _LOGGER.debug(
                f"Adding {len(consumption_statistics)} daily consumption statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name=self._get_statistic_name("consumption"),
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:daily_consumption",
                    unit_of_measurement=self._unit_of_measurement,
                    mean_type=StatisticMeanType.NONE,
                    unit_class="volume",
                ),
                consumption_statistics,
            )
        else:
            _LOGGER.warning("No daily statistics found, skipping update")

        if cost_statistics:
            _LOGGER.debug(f"Adding {len(cost_statistics)} daily cost statistics")
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Daily Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:daily_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                cost_statistics,
            )

        if consumption_cost_statistics and self._consumption_rate > 0:
            _LOGGER.debug(
                f"Adding {len(consumption_cost_statistics)} daily consumption cost statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Daily Consumption Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:daily_consumption_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                consumption_cost_statistics,
            )

        if wastewater_cost_statistics and self._wastewater_rate > 0:
            _LOGGER.debug(
                f"Adding {len(wastewater_cost_statistics)} daily wastewater cost statistics"
            )
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_sum=True,
                    name="Watercare Daily Wastewater Cost",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:daily_wastewater_cost",
                    unit_of_measurement="NZD",
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                ),
                wastewater_cost_statistics,
            )
