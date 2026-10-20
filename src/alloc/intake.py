"""Data intake and data-quality (DQ) checks.

Every record is validated before it reaches the database. Records that fail schema
validation are rejected; records that pass schema checks but carry a business-level
data gap (e.g. a deal with no rating) are loaded and flagged as DQ exceptions, so the
allocation step can block them with a traceable reason instead of silently skipping them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .ratings import RATING_SCALE

VehicleType = Literal["BDC", "drawdown", "insurance_SMA", "evergreen"]
Instrument = Literal["first_lien", "unitranche", "second_lien"]


class Fund(BaseModel):
    fund_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    vehicle_type: VehicleType
    available_capital: float = Field(ge=0)
    target_weight: float = Field(ge=0, le=1)
    min_ticket: float = Field(ge=0)
    max_issuer_pct: float = Field(gt=0, le=1)
    rating_floor: str
    excluded_sectors: list[str] = []
    nav: float = Field(gt=0)

    @field_validator("rating_floor")
    @classmethod
    def _floor_on_scale(cls, v: str) -> str:
        if v not in RATING_SCALE:
            raise ValueError(f"rating_floor '{v}' not on rating scale")
        return v


class Deal(BaseModel):
    deal_id: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    rating: Optional[str] = None
    asset_class: str = Field(min_length=1)
    instrument: Instrument
    size: float = Field(gt=0)
    price: float = Field(gt=0, le=150)
    expected_close: date
    qualifying: bool = True

    @field_validator("rating")
    @classmethod
    def _rating_on_scale(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in RATING_SCALE:
            raise ValueError(f"rating '{v}' not on rating scale")
        return v


@dataclass(frozen=True)
class DQIssue:
    entity: str          # 'fund' or 'deal'
    entity_id: str
    code: str            # SCHEMA_INVALID, DUPLICATE_ID, MISSING_RATING
    severity: str        # Low / Medium / High / Critical
    message: str
    blocking: bool       # True = record rejected (not loaded)


def _validate(records: list[dict], model, entity: str, id_field: str):
    valid, issues, seen = [], [], set()
    for rec in records:
        rid = str(rec.get(id_field, "<missing id>"))
        if rid in seen:
            issues.append(DQIssue(entity, rid, "DUPLICATE_ID", "High",
                                  f"duplicate {id_field} {rid}; later record rejected", True))
            continue
        try:
            obj = model(**rec)
        except ValidationError as e:
            fields = ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors())
            issues.append(DQIssue(entity, rid, "SCHEMA_INVALID", "High",
                                  f"invalid field(s): {fields}", True))
            continue
        seen.add(rid)
        valid.append(obj)
    return valid, issues


def validate_funds(records: list[dict]) -> tuple[list[Fund], list[DQIssue]]:
    return _validate(records, Fund, "fund", "fund_id")


def validate_deals(records: list[dict]) -> tuple[list[Deal], list[DQIssue]]:
    valid, issues = _validate(records, Deal, "deal", "deal_id")
    for d in valid:
        if d.rating is None:
            issues.append(DQIssue("deal", d.deal_id, "MISSING_RATING", "High",
                                  "rating missing; deal loaded but blocked from allocation until resolved",
                                  False))
    return valid, issues


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_dq_exceptions(con: sqlite3.Connection, issues: list[DQIssue]) -> int:
    """Write DQ issues to the exceptions queue (run_id is NULL: raised at intake, before any run)."""
    n0 = con.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0]
    for i, issue in enumerate(issues, start=n0 + 1):
        con.execute(
            "INSERT INTO exceptions (exc_id, run_id, fund_id, rule_id, severity, status, owner, opened_at, detail) "
            "VALUES (?, NULL, ?, ?, ?, 'Open', 'data_ops', ?, ?)",
            (f"E{i:04d}", issue.entity_id if issue.entity == "fund" else None,
             f"DQ-{issue.code}", issue.severity, _now(), f"{issue.entity_id}: {issue.message}"),
        )
    return len(issues)


def load_directory(con: sqlite3.Connection, data_dir: str | Path) -> list[DQIssue]:
    """Validate and load all seed files from `data_dir`. Returns every DQ issue found."""
    d = Path(data_dir)
    read = lambda name: json.loads((d / name).read_text(encoding="utf-8"))

    funds, f_issues = validate_funds(read("funds.json"))
    deals, d_issues = validate_deals(read("deals.json"))

    con.executemany(
        "INSERT INTO funds VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(f.fund_id, f.name, f.vehicle_type, f.available_capital, f.target_weight, f.min_ticket,
          f.max_issuer_pct, f.rating_floor, json.dumps(f.excluded_sectors), f.nav) for f in funds],
    )
    con.executemany(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(x.deal_id, x.issuer, x.sector, x.rating, x.asset_class, x.instrument, x.size, x.price,
          x.expected_close.isoformat(), int(x.qualifying)) for x in deals],
    )
    fund_ids = {f.fund_id for f in funds}
    for h in read("holdings.json"):
        if h["fund_id"] in fund_ids:
            con.execute("INSERT INTO holdings VALUES (?,?,?)", (h["fund_id"], h["issuer"], h["exposure"]))
    for r in read("restricted_list.json"):
        con.execute("INSERT INTO restricted_list VALUES (?,?,?,?,?)",
                    (r["issuer"], r["fund_id"], r["reason"], r["valid_from"], r["valid_to"]))
    for r in read("rules.json"):
        con.execute("INSERT INTO rules VALUES (?,?,?,?,?,?)",
                    (r["rule_id"], r["version"], r["scope"], r["rule_type"], json.dumps(r["params"]),
                     r["effective_from"]))

    fp = d / "fund_portfolio.json"
    if fp.exists():
        for r in json.loads(fp.read_text(encoding="utf-8")):
            if r["fund_id"] in fund_ids:
                con.execute("INSERT INTO fund_portfolio (fund_id, total_assets, qualifying_assets, senior_securities, "
                            "cash, default_funding) VALUES (?,?,?,?,?,?)",
                            (r["fund_id"], r["total_assets"], r["qualifying_assets"], r["senior_securities"],
                             r.get("cash", 0.0), r.get("default_funding")))
    fpl = d / "funding_plans.json"
    if fpl.exists():
        deal_ids = {x.deal_id for x in deals}
        for r in json.loads(fpl.read_text(encoding="utf-8")):
            if r["fund_id"] in fund_ids and r["deal_id"] in deal_ids:
                con.execute("INSERT INTO funding_plans (deal_id, fund_id, source, cash_share, note, updated_by, updated_at) "
                            "VALUES (?,?,?,?,?,'data_ops',?)",
                            (r["deal_id"], r["fund_id"], r["source"], r.get("cash_share"), r.get("note"), _now()))

    issues = f_issues + d_issues
    record_dq_exceptions(con, issues)
    from . import audit
    import hashlib
    audit.append(con, "data_ops", "DATA_LOADED", "seed", {
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(d.glob("*.json"))},
        "funds": len(funds), "deals": len(deals), "dq_issues": [f"{i.entity_id}:{i.code}" for i in issues]})
    con.commit()
    return issues
