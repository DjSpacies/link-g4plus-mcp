"""Canonical live channels and their units (the same names as the ECU tuning skill's log schema)."""
CHANNELS = {
    "rpm": "rpm", "map_kpa": "kPa abs", "baro_kpa": "kPa abs", "mgp_kpa": "kPa gauge", "tps": "%", "ect": "C", "iat": "C",
    "battery_v": "V", "ign_adv": "deg BTDC", "inj_pw": "ms", "lambda": "lambda", "lambda_target": "lambda",
    "fuel_trim": "%", "fuel_pressure_kpa": "kPa", "fuel_temp": "C", "oil_pressure_kpa": "kPa", "oil_temp": "C",
    "knock_count": "count", "boost_target_kpa": "kPa abs", "ethanol_pct": "%",
}
