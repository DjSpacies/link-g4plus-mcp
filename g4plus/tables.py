"""Table roles: which DataIds the live tool reads as named tables (5.6.x address map). Only `ve` is writable."""
TABLES = {
    "ve": dict(id=2365, native="Fuel Table 1", x_labels=3074, y_labels=3075, unit="%",
               note="VE only in modelled fuel modes; in traditional mode the numbers also include other constants"),
    "ign_base": dict(id=2366, native="Ignition Table 1", x_labels=1415, y_labels=1416, unit="deg BTDC"),
}
