from decimal import Decimal


# Historical revenue supplied in Cryo_EM_Revenue_2018_2025_Readable.csv.
# Values are user charges before GST, grouped by usage month.
HISTORICAL_REVENUE = (
    ("2019-02", Decimal("63000")), ("2019-05", Decimal("20000")),
    ("2019-08", Decimal("37252")), ("2019-09", Decimal("88000")),
    ("2019-10", Decimal("74135")), ("2019-11", Decimal("198000")),
    ("2019-12", Decimal("17500")), ("2020-01", Decimal("36000")),
    ("2020-02", Decimal("215500")), ("2020-12", Decimal("10000")),
    ("2021-01", Decimal("18000")), ("2021-02", Decimal("67000")),
    ("2021-04", Decimal("75000")), ("2021-06", Decimal("154000")),
    ("2021-07", Decimal("240000")), ("2021-11", Decimal("335882")),
    ("2021-12", Decimal("64800")), ("2022-01", Decimal("263060")),
    ("2022-04", Decimal("46000")), ("2022-07", Decimal("17000")),
    ("2022-08", Decimal("66670")), ("2022-09", Decimal("65880")),
    ("2022-10", Decimal("6000")), ("2022-11", Decimal("66670")),
    ("2022-12", Decimal("76670")), ("2023-01", Decimal("30000")),
    ("2023-04", Decimal("15000")), ("2023-05", Decimal("117940")),
    ("2023-06", Decimal("30000")), ("2023-07", Decimal("289160")),
    ("2023-08", Decimal("19000")), ("2023-09", Decimal("514800")),
    ("2023-11", Decimal("69240")), ("2024-01", Decimal("8000")),
    ("2024-02", Decimal("14000")), ("2024-04", Decimal("122720")),
    ("2024-05", Decimal("711140")), ("2024-08", Decimal("21000")),
    ("2024-09", Decimal("79480")), ("2024-10", Decimal("33500")),
)

HISTORICAL_CATEGORY_TOTALS = {
    "Internal": Decimal("572000"),
    "External/Academic": Decimal("506092"),
    "Industrial": Decimal("3318907"),
}
