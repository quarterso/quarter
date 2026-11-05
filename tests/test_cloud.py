import gzip
import shutil
from pathlib import Path

import pytest

from quarter_ledger import cloud, period
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.pricing import PriceBook
from quarter_ledger.store import Ledger

FIXTURES = Path(__file__).parent / "fixtures" / "cloud"


def october(records):
    """In whole cents, as the vendors show their totals."""
    return round(sum(r.amount for r in records if r.at.startswith("2026-10")), -4)


@pytest.mark.parametrize(("name", "vendor", "total"), [
    ("aws-cur2-data-export.csv", "aws", "870.36"),  # Bedrock, SageMaker and Claude from Marketplace
    ("azure-ea-cost-details.csv", "azure", "2024.93"),  # includes Claude bought in the Marketplace
    ("azure-mca-cost-details.csv", "azure", "2024.93"),  # billed in EUR, read from costInUsd
    ("google-bigquery-standard-export.csv", "google", "106.56"),  # by usage day; the invoice month says 108.66
    ("google-bigquery-standard-export-dotted.csv", "google", "106.56"),
])
def test_each_export_reads_to_its_ai_total(name, vendor, total):
    records, left = cloud.read([FIXTURES / name])
    assert {r.vendor for r in records} == {vendor}
    assert october(records) == to_micros(total)
    assert left["rows"] > 0  # compute, storage and tax are left out
    assert all(r.bucket for r in records)


def test_a_folder_of_gzipped_parts_reads_once(tmp_path):
    for n, name in enumerate(("azure-ea-cost-details.csv", "aws-cur2-data-export.csv")):
        part = tmp_path / "export" / f"part_{n}.csv.gz"
        part.parent.mkdir(exist_ok=True)
        part.write_bytes(gzip.compress((FIXTURES / name).read_bytes()))
    records, _ = cloud.read(cloud.files(tmp_path / "export"))
    assert october(records) == to_micros("2895.30")  # 2024.9348 + 870.36..., rounded once


def test_parquet_is_refused(tmp_path):
    (tmp_path / "x.parquet").write_bytes(b"PAR1")
    with pytest.raises(ValueError, match="Parquet"):
        cloud.files(tmp_path)


def test_another_currency_without_a_usd_column_is_refused(tmp_path):
    f = tmp_path / "eur.csv"
    f.write_text("Date,MeterCategory,MeterName,CostInBillingCurrency,BillingCurrency\n"
                 "10/01/2026,Azure OpenAI,gpt 4o Inp,10,EUR\n")
    with pytest.raises(ValueError, match="EUR"):
        cloud.read([f])


def test_connect_books_a_revised_export_once_and_restates_a_locked_month(tmp_path):
    db = str(tmp_path / "l.db")
    shutil.copy(FIXTURES / "azure-ea-cost-details.csv", tmp_path / "a.csv")
    main(["--db", db, "connect", "cloud", "--file", str(tmp_path / "a.csv"), "--month", "2026-10", "--no-alerts"])
    main(["--db", db, "connect", "cloud", "--file", str(tmp_path / "a.csv"), "--month", "2026-10", "--no-alerts"])
    ledger = Ledger(db)

    def charged(month):
        return round(ledger.db.execute("select coalesce(sum(amount), 0) from entries where kind = 'charge'"
                                       " and substr(at, 1, 7) = ?", (month,)).fetchone()[0], -4)

    assert charged("2026-10") == to_micros("2024.93")
    period.lock(ledger, "2026-10", "Sam", tmp_path, PriceBook.load({}), "t")
    text = (tmp_path / "a.csv").read_text().replace(",412.18365,", ",422.18365,", 1)  # Azure revises a day
    (tmp_path / "a.csv").write_text(text)
    main(["--db", db, "connect", "cloud", "--file", str(tmp_path / "a.csv"), "--month", "2026-10", "--no-alerts"])
    assert charged("2026-10") == to_micros("2024.93")
    assert charged("2026-11") == to_micros(10)


def test_a_folder_with_manifests_reads_only_the_newest_run(tmp_path):
    import json
    import os
    run = tmp_path / "export" / "20261001-20261031"
    for n, stamp in ((1, 100), (2, 200)):  # Azure keeps one copy per run
        part = run / f"run{n}" / "part_0.csv"
        part.parent.mkdir(parents=True)
        shutil.copy(FIXTURES / "azure-ea-cost-details.csv", part)
        m = run / f"run{n}" / "manifest.json"
        m.write_text(json.dumps({"blobs": [{"blobName": f"export/20261001-20261031/run{n}/part_0.csv"}]}))
        os.utime(m, (stamp, stamp))
    (run / "stale.csv").write_text("leftover")
    got = cloud.files(tmp_path / "export")
    assert got == [run / "run2" / "part_0.csv"]
    assert october(cloud.read(got)[0]) == to_micros("2024.93")


def test_a_manifest_listing_parquet_is_refused(tmp_path):
    (tmp_path / "Manifest.json").write_text('{"dataFiles": ["s3://b/x/part-0.snappy.parquet"]}')
    with pytest.raises(ValueError, match="Parquet"):
        cloud.files(tmp_path)


def test_aws_net_cost_is_preferred_and_cognitive_services_is_ai(tmp_path):
    f = tmp_path / "aws.csv"
    f.write_text("line_item_usage_start_date,line_item_product_code,line_item_usage_type,"
                 "line_item_unblended_cost,line_item_net_unblended_cost\n"
                 "2026-10-02T00:00:00Z,AmazonBedrock,USE1-Claude-Input,100,90\n")
    [r], _ = cloud.read([f])
    assert r.amount == to_micros(90)
    g = tmp_path / "az.csv"
    g.write_text("Date,ConsumedService,MeterName,CostInBillingCurrency,BillingCurrency\n"
                 "10/02/2026,Microsoft.CognitiveServices,gpt 4o Inp,12,USD\n")
    [r], _ = cloud.read([g])
    assert (r.vendor, r.amount) == ("azure", to_micros(12))


def test_google_json_lines_with_nested_credits(tmp_path):
    import json
    row = {"service": {"description": "Vertex AI"}, "sku": {"description": "Gemini Input"},
           "usage_start_time": "2026-10-03T04:00:00Z", "cost": 10.5, "currency": "USD",
           "credits": [{"name": "Free tier", "amount": -2.5}, {"name": "CUD", "amount": -1}]}
    other = {**row, "service": {"description": "Compute Engine"}}
    f = tmp_path / "billing.jsonl"
    f.write_text(json.dumps(row) + "\n" + json.dumps(other) + "\n")
    [r], left = cloud.read([f])
    assert (r.vendor, r.amount, left["rows"]) == ("google", to_micros(7), 1)
