"""Flow confidence: clean data is not the same as a reliable order of steps.

Run with:  pytest -q tests/test_flow_confidence.py
"""
import json
import re
from pathlib import Path

import pytest

from app.core import jobs, reference
from app.core.db import connect
from app.core.facts import Fact
from app.core.flow_confidence import NOT_CONFIRMED, flow_confidence
from app.core.narrative import validate
from scripts.generate_synthetic import main as generate
from test_quality import to_events

P2P = [{"name": "Request", "steps": ["Create Request", "Approve Request"]},
       {"name": "Ordering", "steps": ["Create Order"]},
       {"name": "Receiving", "steps": ["Receive Goods"]},
       {"name": "Invoicing", "steps": ["Receive Invoice", "Pay Invoice"]}]


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(scope="module")
def logs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flow")
    out = {}
    for name, extra in (("clean", []), ("shuffled", ["--shuffle-order"])):
        src = tmp / f"{name}.parquet"
        generate(["--cases", "3000", "--start", "2024-01-01", "--months", "12", "--out", str(src), *extra])
        out[name] = to_events(src, tmp)
    return out


def computed(events: Path, order=None):
    ref = reference.compute(connect(), events, f"flow-{events.stem}", "Purchasing", order, use_saved=False)
    return reference.Order.from_dict(ref["order"]), ref["stats"], ref["flow"]


def solid_arrows(dot: str) -> list[str]:
    return [l for l in dot.splitlines() if re.search(r"\bs\d+ -> s\d+", l) and "style=invis" not in l
            and "dir=both" not in l]


def test_shuffled_log_has_no_flow(logs):
    order, stats, flow = computed(logs["shuffled"])
    assert flow["band"] == "None" and flow["source"] == "data"
    assert "no fixed order in the data" in flow["reasons"] and "no business order given" in flow["reasons"]
    dot = reference.to_reference_dot(order, stats, True, flow)
    assert solid_arrows(dot) == []
    assert "Step overview — no fixed order found" in dot
    reached = [r["cases_reached"] for r in sorted(stats["steps"], key=lambda r: order.drawn_steps().index(r["step"]))]
    boxes = re.findall(r"\b(s\d+) \[label=\"[^\"]*?\\n([\d,]+) cases", dot)
    assert [int(n.replace(",", "")) for _, n in boxes] == sorted(reached, reverse=True)   # sorted by cases


def test_clean_log_flows(logs):
    order, stats, flow = computed(logs["clean"])
    assert flow["band"] == "High" and flow["score"] >= 80 and flow["allowed"]["arrows"]
    dot = reference.to_reference_dot(order, stats, True, flow)
    assert solid_arrows(dot) and "Process flow:" in dot


def test_confirmed_order_on_shuffled_log(logs):
    confirmed = reference.Order("Purchasing", P2P, [], "edited", True)
    order, stats, flow = computed(logs["shuffled"], confirmed)
    assert flow["source"] == "confirmed" and flow["band"] in ("Medium", "High")
    dot = reference.to_reference_dot(order, stats, True, flow)
    assert "Intended order:" in dot and solid_arrows(dot)
    assert stats["in_order_share"] < 0.3
    conformance = " ".join(reference.describe_conformance(order, stats, flow))
    assert "follow the intended order exactly" in conformance and "is done before" in conformance


def test_template_and_ai_are_capped_at_medium():
    for source in ("template", "ai"):
        flow = flow_confidence(0.95, source, 0.97, 0.99, 1.0)
        assert flow["band"] == "Medium" and NOT_CONFIRMED in flow["reasons"]
    assert flow_confidence(0.95, "confirmed", 0.97, 0.99, 1.0)["band"] == "High"
    assert flow_confidence(0.02, "confirmed", 0.01, 0.3, None)["band"] == "Medium"   # at least Medium


def test_validator_rejects_sequence_words_without_flow():
    facts = {"F006": Fact(id="F006", type="variant_share", label="A → B", value=0.4, unit="share",
                          support=900, badge="Exploratory", section="variants")}
    out = {"summary": [{"text": "Offer Processing is followed by Hire Processing in {{F006}} of cases.",
                        "facts": ["F006"]}], "findings": [], "recommendations": []}
    errors = validate(out, {"F006"}, facts, flow_band="None")
    assert any("followed by" in e and "flow confidence is None" in e for e in errors)
    assert not any("sequence word" in e for e in validate(out, {"F006"}, facts, flow_band="High"))


def test_report_shows_both_scores(tmp_path, logs):
    from app.core.mining import run_mining, save_mining
    from app.core.quality import run_quality, save_report
    from app.core.report import generate_report
    src = tmp_path / "shuffled.parquet"
    generate(["--cases", "3000", "--start", "2024-01-01", "--months", "12", "--shuffle-order", "--out", str(src)])
    job = jobs.create_job(src.name, src)
    job["order_choice"] = "data"          # the data's own order (page 3 pick), not the matching template
    jobs.save_job(job)
    folder = jobs.job_folder(job["job_id"])
    events = to_events(src, tmp_path)
    events.replace(folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps({"case_id": "case_id", "activity": "activity"}), encoding="utf-8")
    quality = run_quality(connect(), folder / "events.parquet")
    save_report(quality, folder)
    mining = run_mining(connect(), folder / "events.parquet", quality.band)
    save_mining(mining, folder)
    assert mining["flow_confidence"]["data"]["band"] == "None"
    job.update(status="mined", band=quality.band, feasibility=quality.feasibility,
               flow_band=mining["flow_confidence"]["data"]["band"])
    jobs.save_job(job)
    html = generate_report(job["job_id"])["html"].read_text(encoding="utf-8")
    section = html.split("<h2>4. Process map and steps</h2>")[1].split("</section>")[0]
    assert f"Data quality: {quality.feasibility} {quality.band} · Flow confidence: " in section
    assert "None (no fixed order in the data; no business order given)" in section
    assert "The data does not show a fixed order. The map shows which steps occur and how often, grouped by stage." \
        in section
    # No conformance on a guessed order (B2.7); the order source is in the report header.
    assert "How reality compares" not in section and "Found in data — not confirmed" in html
    assert "Step order not confirmed" in html.split("<h2>7. ")[1].split("</section>")[0]
    facts = json.loads((folder / "facts.json").read_text(encoding="utf-8"))
    assert all(f["badge"] == "Exploratory" for f in facts if f["type"] in ("variant_share", "in_order_share"))
    assert {"flow_confidence", "order_source"} <= {f["type"] for f in facts}
    assert jobs.load_job(job["job_id"])["flow_band"] == "None"
