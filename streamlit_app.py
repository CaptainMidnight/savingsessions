from typing import Any, cast
from dataclasses import dataclass
from datetime import datetime
import numpy as np
import pendulum
import streamlit as st

from api import API, AuthenticationError, ElectricityMeterPoint


@dataclass
class SavingSession:
    timestamp: datetime
    hh: int
    reward: int


def ss(timestamp: str, hh: int, reward: int):
    return SavingSession(cast(datetime, pendulum.parser.parse(timestamp)), hh, reward)


SAVING_SESSIONS = [
    ss("2023-11-16 17:30", 2, 1800),
]


def weekday(day):
    """True if day is a weekday"""
    return pendulum.MONDAY <= day.day_of_week <= pendulum.FRIDAY


def total(api: API, meter_point: ElectricityMeterPoint, ts: datetime, hh: int):
    readings = api.half_hourly_readings(
        mpan=meter_point.mpan,
        meter=meter_point.meters[0].id,
        start_at=ts,
        first=hh,
    )
    if len(readings) == 0:
        raise ValueError("missing readings")
    total = np.array([reading.value for reading in readings])
    return total


def calculate(api: API, mpans: dict, ss: SavingSession):
    try:
        ss_import = total(api, mpans["IMPORT"], ss.timestamp, ss.hh)
        if meter_point := mpans.get("EXPORT"):
            ss_export = total(api, meter_point, ss.timestamp, ss.hh)
        else:
            ss_export = np.zeros(ss.hh)  # no export
    except ValueError:
        return None

    # Baseline from meter readings from the same time as the Session over the past 10 weekdays (excluding any days with a Saving Session),
    # past 4 weekend days if Saving Session is on a weekend.
    days = 0
    baseline_days = 10 if weekday(ss.timestamp) else 4
    baseline = np.zeros(ss.hh)
    previous_session_days = {ss.timestamp.date() for ss in SAVING_SESSIONS}
    previous = pendulum.period(
        ss.timestamp.subtract(days=1), ss.timestamp.subtract(days=61)
    )
    for dt in previous.range("days"):
        if weekday(dt) != weekday(ss.timestamp):
            continue
        if dt in previous_session_days:
            continue
        try:
            baseline += total(api, mpans["IMPORT"], dt, ss.hh)
            if meter_point := mpans.get("EXPORT"):
                baseline -= total(api, meter_point, dt, ss.hh)
            days += 1

            if days == baseline_days:
                break
        except ValueError:
            pass

    baseline = baseline / days
    # saving is calculated per settlement period (half hour), and only positive savings considered
    kwh = (baseline - ss_import + ss_export).clip(min=0)
    points = kwh.sum() * ss.reward
    reward = int(np.ceil(points / 8) * 8)

    row = {
        "session": ss.timestamp,
        "import": ss_import.sum(),
        "export": ss_export.sum(),
        "baseline": baseline.sum(),
        "saved": kwh.sum(),
        "reward": reward,
        "earnings": reward / 800,
    }
    return row


def error(msg: str):
    st.error(msg, icon="⚠️")
    st.stop()


def main():
    debug = "debug" in st.experimental_get_query_params()

    st.set_page_config(page_icon="🐙", page_title="Octopus Saving Sessions calculator")
    st.header("🐙 Octopus Saving Sessions calculator")

    st.subheader("Your Octopus API Key")
    st.markdown(
        "Find this in your online dashboard: https://octopus.energy/dashboard/developer/"
    )
    if "api_key" not in st.session_state and (
        api_key := st.experimental_get_query_params().get("api_key")
    ):
        st.session_state["api_key"] = api_key[0]
    api_key = st.text_input("API key:", key="api_key", placeholder="sk_live_...")
    if not api_key:
        st.stop()

    st.experimental_set_query_params(api_key=api_key)
    st.info("Tip: bookmark this url to return with your API key remembered.", icon="🔖")

    bar = st.progress(0, text="Authenticating...")

    api = API(api_key)
    try:
        api.authenticate()
    except AuthenticationError:
        error("Authentication error, check your API key")

    bar.progress(0.2, text="Getting account...")
    accounts = api.accounts()
    if not accounts:
        error("No accounts found")
    account = accounts[0]

    if debug:
        st.write(account)
    bar.progress(0.4, text="Getting meters...")
    agreements = api.agreements(account.number)
    if debug:
        for agreement in agreements:
            st.write(agreement)
    if not agreements:
        error("No agreements on account")

    bar.progress(0.5, text="Getting tariffs...")
    mpans: dict[str, ElectricityMeterPoint] = {}
    for agreement in agreements:
        product = api.energy_product(agreement.tariff.productCode)
        if product.direction in mpans:
            st.warning(
                "Multiple %s meterpoints, using first" % product.direction, icon="⚠️"
            )
        else:
            mpans[product.direction] = agreement.meterPoint
            if len(agreement.meterPoint.meters) > 1:
                st.warning(
                    "Meterpoint %s has multiple meters, using first"
                    % agreement.meterPoint.mpan,
                    icon="⚠️",
                )
        if debug:
            st.write(product)

    if "IMPORT" not in mpans:
        error("Import meterpoint not found")

    if "EXPORT" not in mpans:
        st.info("Import meter only", icon="ℹ️")

    bar.progress(0.8, text="Getting readings...")
    rows = []
    missing = []
    for ss in SAVING_SESSIONS:
        row = calculate(api, mpans, ss)
        if row is not None:
            rows.append(row)
        else:
            missing.append(ss.timestamp)

    bar.progress(1.0, text="Done")
    st.subheader("Results")

    st.dataframe(
        rows,
        column_config={
            "session": st.column_config.DatetimeColumn(
                "Session", format="YYYY/MM/DD HH:mm"
            ),
            "import": st.column_config.NumberColumn("Imported", format="%.2f kWh"),
            "export": st.column_config.NumberColumn("Exported", format="%.2f kWh"),
            "baseline": st.column_config.NumberColumn("Baseline", format="%.2f kWh"),
            "saved": st.column_config.NumberColumn("Saved", format="%.2f kWh"),
            "reward": st.column_config.NumberColumn("Reward"),
            "earnings": st.column_config.NumberColumn("Earnings", format="£%.2f"),
        },
    )
    for ts in missing:
        st.info(f"Session on {ts:%Y/%m/%d} is awaiting readings...", icon="⌛")


main()
