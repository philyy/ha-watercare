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
from homeassistant.components.recorder.statistics import async_add_external_statistics

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

    async_add_entities(
        [
            WatercareUsageSensor(
                SENSOR_NAME,
                api,
                consumption_rate,
                wastewater_rate,
                wastewater_ratio,
                annual_line_charge,
                endpoint,
            )
        ],
        True,
    )


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

    def _add_statistics(
        self,
        consumption_statistics,
        cost_statistics,
        consumption_cost_statistics,
        wastewater_cost_statistics,
    ):
        """Write the shared external statistics for the configured endpoint.

        All endpoints share one set of statistic ids (the integration tracks a
        single meter and only one endpoint runs at a time), so switching
        endpoints keeps the same Energy Dashboard source instead of orphaning a
        previous one. Only the display name varies by endpoint.
        """
        if consumption_statistics:
            _LOGGER.debug(
                f"Adding {len(consumption_statistics)} consumption statistics"
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
            _LOGGER.debug(f"Adding {len(cost_statistics)} cost statistics")
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

        period_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_sum = 0
        cost_running_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0

        # Sort periods by date (oldest first) for cumulative calculation
        sorted_periods = sorted(
            billing_periods, key=lambda x: x.get("billingPeriodToDate", "")
        )

        for period in sorted_periods:
            end_date_str = period.get("billingPeriodToDate")
            if end_date_str:
                try:
                    # Parse and convert to NZ timezone
                    end_date = datetime.strptime(end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                    end_date = pytz.utc.localize(end_date).astimezone(NZ_TIMEZONE)

                    period_usage = period.get("waterUsage", 0)
                    running_sum += period_usage

                    numberOfDays = (
                        datetime.strptime(
                            period.get("billingPeriodToDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        - datetime.strptime(
                            period.get("billingPeriodFromDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                    ).days + 1

                    cost_breakdown = self._calculate_cost(period_usage, numberOfDays)
                    cost_running_sum += cost_breakdown["total"]
                    consumption_cost_running_sum += cost_breakdown["consumption"]
                    wastewater_cost_running_sum += cost_breakdown["wastewater"]

                    # Create StatisticData with running sum (critical for Energy Dashboard)
                    period_statistics.append(
                        StatisticData(start=end_date, sum=running_sum)
                    )

                    # Create cost statistics
                    cost_statistics.append(
                        StatisticData(start=end_date, sum=cost_running_sum)
                    )

                    # Create consumption cost statistics if rate is configured
                    if self._consumption_rate > 0:
                        consumption_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=consumption_cost_running_sum
                            )
                        )

                    # Create wastewater cost statistics if rate is configured
                    if self._wastewater_rate > 0:
                        wastewater_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=wastewater_cost_running_sum
                            )
                        )

                except (ValueError, TypeError) as e:
                    _LOGGER.warning(f"Failed to parse date {end_date_str}: {e}")
                    continue

        self._add_statistics(
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
        hour_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        litres_running_sum = 0
        cost_running_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0

        # One hour is 1/24 of a day for the line-charge portion of cost.
        hour_fraction = 1 / 24

        for hour_start in sorted(hourly_consumption):
            litres = hourly_consumption[hour_start]
            litres_running_sum += litres

            hour_cost_breakdown = self._calculate_cost(litres, hour_fraction)
            cost_running_sum += hour_cost_breakdown["total"]
            consumption_cost_running_sum += hour_cost_breakdown["consumption"]
            wastewater_cost_running_sum += hour_cost_breakdown["wastewater"]

            hour_statistics.append(
                StatisticData(start=hour_start, sum=litres_running_sum)
            )
            cost_statistics.append(
                StatisticData(start=hour_start, sum=cost_running_sum)
            )
            if self._consumption_rate > 0:
                consumption_cost_statistics.append(
                    StatisticData(start=hour_start, sum=consumption_cost_running_sum)
                )
            if self._wastewater_rate > 0:
                wastewater_cost_statistics.append(
                    StatisticData(start=hour_start, sum=wastewater_cost_running_sum)
                )

        self._add_statistics(
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
        month_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        litres_running_sum = 0
        cost_running_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0

        for index, (month_start, entry) in enumerate(parsed_months):
            litres = entry.get("litres", 0)
            litres_running_sum += litres

            number_of_days = self._month_length_days(parsed_months, index)
            month_cost_breakdown = self._calculate_cost(litres, number_of_days)
            cost_running_sum += month_cost_breakdown["total"]
            consumption_cost_running_sum += month_cost_breakdown["consumption"]
            wastewater_cost_running_sum += month_cost_breakdown["wastewater"]

            month_statistics.append(
                StatisticData(start=month_start, sum=litres_running_sum)
            )
            cost_statistics.append(
                StatisticData(start=month_start, sum=cost_running_sum)
            )
            if self._consumption_rate > 0:
                consumption_cost_statistics.append(
                    StatisticData(start=month_start, sum=consumption_cost_running_sum)
                )
            if self._wastewater_rate > 0:
                wastewater_cost_statistics.append(
                    StatisticData(start=month_start, sum=wastewater_cost_running_sum)
                )

        self._add_statistics(
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

        litresRunningSum = 0
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
        day_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_cost_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0
        first = True

        for date, litres in daily_consumption.items():
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=NZ_TIMEZONE)

            # HASSIO statistics requires us to add values as a sum of all previous values.
            litresRunningSum += litres

            # Calculate cost for this day
            daily_cost_breakdown = self._calculate_cost(litres, 1)
            running_cost_sum += daily_cost_breakdown["total"]
            consumption_cost_running_sum += daily_cost_breakdown["consumption"]
            wastewater_cost_running_sum += daily_cost_breakdown["wastewater"]

            if first:
                reset = start
                first = False

            # Add consumption statistics
            day_statistics.append(
                StatisticData(start=start, sum=litresRunningSum, last_reset=reset)
            )

            # Add cost statistics
            cost_statistics.append(StatisticData(start=start, sum=running_cost_sum))

            # Add consumption cost statistics if rate is configured
            if self._consumption_rate > 0:
                consumption_cost_statistics.append(
                    StatisticData(start=start, sum=consumption_cost_running_sum)
                )

            # Add wastewater cost statistics if rate is configured
            if self._wastewater_rate > 0:
                wastewater_cost_statistics.append(
                    StatisticData(start=start, sum=wastewater_cost_running_sum)
                )

        self._add_statistics(
            day_statistics,
            cost_statistics,
            consumption_cost_statistics,
            wastewater_cost_statistics,
        )
