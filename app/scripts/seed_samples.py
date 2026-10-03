"""
app/scripts/seed_samples.py
===========================
OPTIONAL dev helper: creates four sample datasets (new ds_ codes) and uploads their data through
the same ingest path as the admin upload, so the site can be tried without your own files.

    docker compose exec api python -m app.scripts.seed_samples

  us_cities         points (geometry)  -> suggests a map
  monthly_sales     table with months  -> suggests a line chart
  regional_sales    576 rows           -> exercises every chart type
  africa_countries  polygons + values  -> suggests a choropleth map
Delete them afterwards on /admin/datasets.
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


TITLES = {
    "us_cities": ("US city snapshot", "demographics", "Population, income and growth for 16 US cities."),
    "monthly_sales": ("Monthly sales", "economy", "Revenue, orders and conversion by month."),
    "regional_sales": ("Regional sales by product", "economy", "Monthly revenue, units and margin by region, category and product (synthetic)."),
    "africa_countries": ("Africa: population, GDP and CO2 per person", "climate", "Latest-year values per country."),
}


def run() -> dict:
    """Create the four sample datasets and upload their files. Returns {sample_name: dataset_code}."""
    from app.services import catalog_service
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for name, path in _files(Path(d)).items():
            title, category, desc = TITLES[name]
            row = catalog_service.create({"title": title, "category": category, "description": desc,
                                          "source_name": "Sample data", "license": "CC0-1.0"})
            r = dataset_ingest.ingest(row["dataset_id"], path, path.name)
            print(f"{row['dataset_id']}  {title}: {r['rows']} rows, geometry={r['geometry_type']}")
            out[name] = row["dataset_id"]
    return out


if __name__ == "__main__":
    run()