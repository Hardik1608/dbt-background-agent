# Provenance of the sales-warehouse test bed

The dbt project in `project/` and the data generator in `src/dbt_agent/fixtures/sales_source/`
are copies from the author's own project **`05_sales-analytics-platform`**
(future repo `sales-analytics-platform`, MIT licence), taken on **2026-10-14**. The original
was not modified.

| Copied from (05_sales-analytics-platform) | Copied to | Changes |
|---|---|---|
| `warehouse/` (models, macros, seeds, snapshots, tests, analyses, `dbt_project.yml`, `packages.yml`, `package-lock.yml`) | `fixtures/sales_warehouse/project/` | none |
| `warehouse/profiles.yml` | `fixtures/sales_warehouse/original_config/profiles.yml` (kept for reference) | replaced in `project/` by a one-target DuckDB profile (`warehouse.duckdb` in the project directory) |
| `.sqlfluff` | `fixtures/sales_warehouse/original_config/.sqlfluff` (kept) | in `project/`: `project_dir`/`profiles_dir` point to `.`, target `dev` |
| `src/sales_analytics/ingestion/synthetic.py`, `landing.py`, `load_bronze.py` | `src/dbt_agent/fixtures/sales_source/` | a two-line provenance header and the import path (`sales_analytics.ingestion` -> `dbt_agent.fixtures.sales_source`) |

`SOURCE_SHA256.txt` lists the SHA-256 of every original file at copy time, so the copy can
be checked against the source repository.

Added for the sandbox: `project/.gitignore` (build directories, packages and DuckDB files).

## Data

No real data is shipped or downloaded. The fixture uses the sales project's **synthetic**
generator (seed 42, 68,628 invoice lines in the UCI Online Retail II schema, with the same
data-quality quirks as the real data). `dbt_agent.fixtures.sales.build_base` replays the 25
monthly batches the way a daily pipeline would (19 history batches with the SCD2 snapshot after
each, one full build, then 6 incremental builds), using a fixed ingestion clock so every
machine builds identical data. `dbt deps` installs `dbt_utils` 1.4.1 (Apache-2.0) at build
time; it is not vendored here.
