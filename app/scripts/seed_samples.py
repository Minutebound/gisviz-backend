"""
app/scripts/seed_samples.py
===========================
Four sample tables for trying the site before your own data is in. They are created as ordinary
tables in the DuckDB WORKSPACE (exactly like tables you make in the DuckDB UI), so they flow through
the same path: workspace -> published file -> datasets DB (draft) -> publish on /datasets/manage.

Loaded by the workspace container when GISVIZ_SEED_SAMPLES=true (only tables that do not exist yet):
  us_cities         points (geometry)  -> suggests a map
  monthly_sales     table with months  -> suggests a line chart
  regional_sales    576 rows           -> exercises every chart type
  africa_countries  polygons + values  -> suggests a choropleth map
To remove them later, DROP the tables in the DuckDB UI.
"""
import csv
import json
import random
import tempfile
from pathlib import Path

from app.services import dataset_ingest

HERE = Path(__file__).parent
CITIES = [
    ("Seattle", -122.335, 47.608, 755000, 97185, 8.4), ("Portland", -122.676, 45.523, 652000, 78476, 3.1),
    ("San Francisco", -122.419, 37.775, 808000, 136689, 1.2), ("Los Angeles", -118.243, 34.052, 3822000, 76135, 0.4),
    ("San Diego", -117.161, 32.716, 1381000, 98657, 2.2), ("Phoenix", -112.074, 33.448, 1644000, 72092, 11.3),
    ("Denver", -104.99, 39.739, 713000, 88213, 6.8), ("Salt Lake City", -111.891, 40.761, 209000, 73280, 5.4),
    ("Austin", -97.743, 30.267, 979000, 89415, 15.2), ("Dallas", -96.797, 32.777, 1299000, 63985, 6.1),
    ("Houston", -95.369, 29.76, 2302000, 60440, 4.9), ("Chicago", -87.63, 41.878, 2665000, 71673, -1.8),
    ("Atlanta", -84.388, 33.749, 499000, 77655, 9.7), ("Miami", -80.192, 25.762, 455000, 54858, 7.3),
    ("New York", -74.006, 40.713, 8336000, 76607, -2.4), ("Boston", -71.058, 42.36, 654000, 89212, 1.6),
]
MONTHS = [("Jan", 42000, 610, 4.1), ("Feb", 38500, 548, 3.8), ("Mar", 51200, 742, 4.4), ("Apr", 47800, 690, 4.2),
          ("May", 63400, 905, 4.9), ("Jun", 71900, 1024, 5.3), ("Jul", 68300, 972, 5.0), ("Aug", 74600, 1088, 5.4),
          ("Sep", 82100, 1180, 5.8), ("Oct", 79400, 1142, 5.6), ("Nov", 91300, 1305, 6.2), ("Dec", 88700, 1261, 6.0)]


PRODUCTS = {"Hardware": ["Sensors", "Drones", "GPS units"], "Software": ["Desktop GIS", "Web maps", "Mobile app"],
            "Data": ["Imagery", "Parcels", "Roads"], "Services": ["Surveys", "Training", "Support"]}
REGIONS = ["North", "South", "East", "West"]


def _regional_sales_rows():
    rng = random.Random(7)
    for m in range(12):
        for ri, region in enumerate(REGIONS):
            for cat, prods in PRODUCTS.items():
                for pi, prod in enumerate(prods):
                    units = max(1, int(rng.gauss(120 + 25 * ri + 18 * pi + 6 * m, 30)))
                    price = round(40 + 35 * pi + rng.random() * 20 + (60 if cat == "Hardware" else 0), 2)
                    margin = round(rng.gauss(32 - 3 * pi + (5 if cat == "Software" else 0), 4), 1)
                    yield (f"2025-{m + 1:02d}-01", prod, region, cat, round(units * price, 2), units, margin, price)



SAMPLES = {
    "us_cities": {"title": "US city snapshot", "description": "Population, income and growth for 16 US cities.",
                  "category": "demographics", "source_name": "Sample data", "license": "CC0-1.0"},
    "monthly_sales": {"title": "Monthly sales", "description": "Revenue, orders and conversion by month.",
                      "category": "economy", "source_name": "Sample data", "license": "CC0-1.0"},
    "regional_sales": {"title": "Regional sales by product",
                       "description": "Monthly revenue, units and margin by region, category and product (synthetic).",
                       "category": "economy", "source_name": "Sample data", "license": "CC0-1.0"},
    "africa_countries": {"title": "Africa: population, GDP and CO2 per person", "category": "demographics",
                         "description": "Latest-year values per country.",
                         "source_name": "Our World in Data / Natural Earth",
                         "source_url": "https://ourworldindata.org/co2-and-greenhouse-gas-emissions",
                         "license": "CC-BY-4.0"},
}


def _files(tmp: Path) -> dict:
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"name": n, "population": p, "median_income": inc, "growth_pct": g}}
        for n, lon, lat, p, inc, g in CITIES]}
    (tmp / "us_cities.geojson").write_text(json.dumps(gj))
    with open(tmp / "monthly_sales.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["month", "revenue", "orders", "conversion_pct"]); w.writerows(MONTHS)
    with open(tmp / "regional_sales.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_month", "product", "region", "category", "revenue", "units", "margin_pct", "unit_price"])
        w.writerows(_regional_sales_rows())
    return {"us_cities": tmp / "us_cities.geojson", "monthly_sales": tmp / "monthly_sales.csv",
            "regional_sales": tmp / "regional_sales.csv",
            "africa_countries": HERE / "seed_data" / "africa_countries.geojson"}



def _files(tmp: Path) -> dict:
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"name": n, "population": p, "median_income": inc, "growth_pct": g}}
        for n, lon, lat, p, inc, g in CITIES]}
    (tmp / "us_cities.geojson").write_text(json.dumps(gj))
    with open(tmp / "monthly_sales.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["month", "revenue", "orders", "conversion_pct"]); w.writerows(MONTHS)
    with open(tmp / "regional_sales.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_month", "product", "region", "category", "revenue", "units", "margin_pct", "unit_price"])
        w.writerows(_regional_sales_rows())
    return {"us_cities": tmp / "us_cities.geojson", "monthly_sales": tmp / "monthly_sales.csv",
            "regional_sales": tmp / "regional_sales.csv",
            "africa_countries": HERE / "seed_data" / "africa_countries.geojson"}


def load_into(con) -> list[str]:
    """Create the sample tables in the workspace connection `con` (skips ones that exist)."""
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' AND database_name = current_database()").fetchall()}
    made = []
    with tempfile.TemporaryDirectory() as d:
        for table, path in _files(Path(d)).items():
            if table in existing:
                continue
            cur = con.cursor()
            try:
                dataset_ingest.stage_file(cur, path, path.name)
                cols = [r[0] for r in cur.execute("DESCRIBE _stage").fetchall()]
                sel = "* EXCLUDE (_gv_geom), _gv_geom AS geometry" if "_gv_geom" in cols else "*"
                cur.execute(f'CREATE TABLE main."{table}" AS SELECT {sel} FROM _stage')
                cur.execute("DROP TABLE _stage")
                made.append(table)
            finally:
                cur.close()
    return made