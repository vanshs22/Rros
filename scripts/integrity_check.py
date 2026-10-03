"""Fail-closed tenant/data integrity scan with JSON output for release automation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {
    "organizations",
    "users",
    "customers",
    "leads",
    "appointments",
    "transactions",
    "payments",
    "invoices",
    "reviews",
    "communication_events",
    "call_events",
    "channel_consents",
    "revenue_opportunities",
    "opportunity_events",
    "recovery_plans",
    "recovery_actions",
    "recovery_approvals",
    "attribution_links",
    "integrations",
    "integration_events",
    "connector_actions",
    "integration_jobs",
    "connector_receipts",
    "webhook_events",
    "business_policies",
    "billing_sessions",
    "attribution_events",
    "integration_oauth_states",
    "customer_identities",
    "recovery_playbooks",
}

# Every query returns rows rather than only a count so reports include actionable IDs.
ORPHAN_CHECKS = {
    "customers.organization_id": """
        SELECT c.id FROM customers c LEFT JOIN organizations o ON o.id = c.organization_id
        WHERE o.id IS NULL {tenant}
    """,
    "leads.customer_id": """
        SELECT l.id FROM leads l LEFT JOIN customers c
          ON c.organization_id = l.organization_id AND c.id = l.customer_id
        WHERE l.customer_id IS NOT NULL AND c.id IS NULL {tenant}
    """,
    "appointments.customer_or_lead": """
        SELECT a.id FROM appointments a
        LEFT JOIN customers c ON c.organization_id = a.organization_id AND c.id = a.customer_id
        LEFT JOIN leads l ON l.organization_id = a.organization_id AND l.id = a.lead_id
        WHERE (a.customer_id IS NOT NULL AND c.id IS NULL)
           OR (a.lead_id IS NOT NULL AND l.id IS NULL) {tenant}
    """,
    "transactions.customer_or_opportunity": """
        SELECT t.id FROM transactions t
        LEFT JOIN customers c ON c.organization_id = t.organization_id AND c.id = t.customer_id
        LEFT JOIN revenue_opportunities o ON o.organization_id = t.organization_id AND o.id = t.source_opportunity_id
        WHERE (t.customer_id IS NOT NULL AND c.id IS NULL)
           OR (t.source_opportunity_id IS NOT NULL AND o.id IS NULL) {tenant}
    """,
    "payments.customer_id": """
        SELECT p.id FROM payments p LEFT JOIN customers c
          ON c.organization_id = p.organization_id AND c.id = p.customer_id
        WHERE p.customer_id IS NOT NULL AND c.id IS NULL {tenant}
    """,
    "invoices.customer_id": """
        SELECT i.id FROM invoices i LEFT JOIN customers c
          ON c.organization_id = i.organization_id AND c.id = i.customer_id
        WHERE i.customer_id IS NOT NULL AND c.id IS NULL {tenant}
    """,
    "reviews.customer_id": """
        SELECT r.id FROM reviews r LEFT JOIN customers c
          ON c.organization_id = r.organization_id AND c.id = r.customer_id
        WHERE r.customer_id IS NOT NULL AND c.id IS NULL {tenant}
    """,
    "communication_events.customer_or_lead": """
        SELECT e.id FROM communication_events e
        LEFT JOIN customers c ON c.organization_id = e.organization_id AND c.id = e.customer_id
        LEFT JOIN leads l ON l.organization_id = e.organization_id AND l.id = e.lead_id
        WHERE (e.customer_id IS NOT NULL AND c.id IS NULL)
           OR (e.lead_id IS NOT NULL AND l.id IS NULL) {tenant}
    """,
    "call_events.customer_or_lead": """
        SELECT e.id FROM call_events e
        LEFT JOIN customers c ON c.organization_id = e.organization_id AND c.id = e.customer_id
        LEFT JOIN leads l ON l.organization_id = e.organization_id AND l.id = e.lead_id
        WHERE (e.customer_id IS NOT NULL AND c.id IS NULL)
           OR (e.lead_id IS NOT NULL AND l.id IS NULL) {tenant}
    """,
    "channel_consents.customer_id": """
        SELECT x.id FROM channel_consents x LEFT JOIN customers c
          ON c.organization_id = x.organization_id AND c.id = x.customer_id
        WHERE c.id IS NULL {tenant}
    """,
    "revenue_opportunities.references": """
        SELECT o.id FROM revenue_opportunities o
        LEFT JOIN customers c ON c.organization_id = o.organization_id AND c.id = o.customer_id
        LEFT JOIN leads l ON l.organization_id = o.organization_id AND l.id = o.lead_id
        LEFT JOIN users u ON u.organization_id = o.organization_id AND u.id = o.assigned_user_id
        WHERE (o.customer_id IS NOT NULL AND c.id IS NULL)
           OR (o.lead_id IS NOT NULL AND l.id IS NULL)
           OR (o.assigned_user_id IS NOT NULL AND u.id IS NULL) {tenant}
    """,
    "opportunity_events.opportunity_id": """
        SELECT e.id FROM opportunity_events e LEFT JOIN revenue_opportunities o
          ON o.organization_id = e.organization_id AND o.id = e.opportunity_id
        WHERE o.id IS NULL {tenant}
    """,
    "recovery_plans.opportunity_id": """
        SELECT p.id FROM recovery_plans p LEFT JOIN revenue_opportunities o
          ON o.organization_id = p.organization_id AND o.id = p.opportunity_id
        WHERE o.id IS NULL {tenant}
    """,
    "recovery_actions.opportunity_id": """
        SELECT a.id FROM recovery_actions a LEFT JOIN revenue_opportunities o
          ON o.organization_id = a.organization_id AND o.id = a.opportunity_id
        WHERE o.id IS NULL {tenant}
    """,
    "recovery_approvals.references": """
        SELECT a.id FROM recovery_approvals a
        LEFT JOIN recovery_actions x ON x.organization_id = a.organization_id AND x.id = a.action_id
        LEFT JOIN users u ON u.organization_id = a.organization_id AND u.id = a.approved_by_user_id
        WHERE x.id IS NULL OR u.id IS NULL {tenant}
    """,
    "attribution_links.references": """
        SELECT a.id FROM attribution_links a
        LEFT JOIN transactions t ON t.organization_id = a.organization_id AND t.id = a.transaction_id
        LEFT JOIN revenue_opportunities o ON o.organization_id = a.organization_id AND o.id = a.opportunity_id
        WHERE t.id IS NULL OR o.id IS NULL {tenant}
    """,
    "integration_events.integration_id": """
        SELECT e.id FROM integration_events e LEFT JOIN integrations i
          ON i.organization_id = e.organization_id AND i.id = e.integration_id
        WHERE i.id IS NULL {tenant}
    """,
    "connector_actions.references": """
        SELECT a.id FROM connector_actions a
        LEFT JOIN integrations i ON i.organization_id = a.organization_id AND i.id = a.integration_id
        LEFT JOIN revenue_opportunities o ON o.organization_id = a.organization_id AND o.id = a.opportunity_id
        WHERE i.id IS NULL OR (a.opportunity_id IS NOT NULL AND o.id IS NULL) {tenant}
    """,
    "integration_jobs.references": """
        SELECT j.id FROM integration_jobs j
        LEFT JOIN integrations i ON i.organization_id = j.organization_id AND i.id = j.integration_id
        LEFT JOIN connector_actions a ON a.organization_id = j.organization_id AND a.id = j.connector_action_id
        WHERE (j.integration_id IS NOT NULL AND i.id IS NULL)
           OR (j.connector_action_id IS NOT NULL AND a.id IS NULL) {tenant}
    """,
    "connector_receipts.references": """
        SELECT r.id FROM connector_receipts r
        LEFT JOIN integrations i ON i.organization_id = r.organization_id AND i.id = r.integration_id
        LEFT JOIN connector_actions a ON a.organization_id = r.organization_id AND a.id = r.action_id
        WHERE i.id IS NULL OR a.id IS NULL {tenant}
    """,
    "webhook_events.organization_id": """
        SELECT e.id FROM webhook_events e LEFT JOIN organizations o ON o.id = e.organization_id
        WHERE o.id IS NULL {tenant}
    """,
    "business_policies.organization_id": """
        SELECT p.id FROM business_policies p LEFT JOIN organizations o ON o.id = p.organization_id
        WHERE o.id IS NULL {tenant}
    """,
    "customer_identities.references": """
        SELECT i.id FROM customer_identities i
        LEFT JOIN customers c ON c.organization_id = i.organization_id AND c.id = i.customer_id
        WHERE c.id IS NULL {tenant}
    """,
    "recovery_playbooks.organization_id": """
        SELECT p.id FROM recovery_playbooks p LEFT JOIN organizations o ON o.id = p.organization_id
        WHERE o.id IS NULL {tenant}
    """,
    "billing_sessions.organization_id": """
        SELECT b.id FROM billing_sessions b LEFT JOIN organizations o ON o.id = b.organization_id
        WHERE o.id IS NULL {tenant}
    """,
    "attribution_events.opportunity_id": """
        SELECT e.id FROM attribution_events e LEFT JOIN revenue_opportunities o
          ON o.organization_id = e.organization_id AND o.id = e.opportunity_id
        WHERE o.id IS NULL {tenant}
    """,
    "integration_oauth_states.user_id": """
        SELECT s.id FROM integration_oauth_states s LEFT JOIN users u
          ON u.organization_id = s.organization_id AND u.id = s.user_id
        WHERE u.id IS NULL {tenant}
    """,
}

DUPLICATE_CHECKS = {
    "webhook_events.external_id": ("webhook_events", "organization_id, external_id"),
    "customers.external_id": ("customers", "organization_id, external_id"),
    "leads.external_id": ("leads", "organization_id, external_id"),
    "payments.external_id": ("payments", "organization_id, external_id"),
    "transactions.external_id": ("transactions", "organization_id, external_id"),
    "connector_actions.idempotency_key": ("connector_actions", "organization_id, idempotency_key"),
    "integration_jobs.dedupe_key": ("integration_jobs", "organization_id, dedupe_key"),
    "revenue_opportunities.detection_key": ("revenue_opportunities", "organization_id, detection_key"),
    "integrations.provider": ("integrations", "organization_id, provider"),
    "billing_sessions.provider_session_id": ("billing_sessions", "organization_id, provider_session_id"),
}


def _tenant_clause(organization_id: str | None, column: str = "organization_id") -> tuple[str, dict[str, Any]]:
    if not organization_id:
        return "", {}
    return f"AND {column} = :organization_id", {"organization_id": organization_id}


def scan(database_url: str, organization_id: str | None = None) -> dict[str, Any]:
    engine = create_engine(database_url, future=True)
    violations: list[dict[str, Any]] = []
    try:
        available = set(inspect(engine).get_table_names())
        missing = sorted(REQUIRED_TABLES - available)
        for table in missing:
            violations.append({"check": f"schema.missing_table:{table}", "count": 1, "examples": [table]})
        if missing:
            return _report(database_url, organization_id, violations)
        tenant, params = _tenant_clause(organization_id)
        with engine.connect() as connection:
            for check, query in ORPHAN_CHECKS.items():
                rows = connection.execute(text(query.format(tenant=tenant)), params).fetchall()
                if rows:
                    violations.append({"check": f"orphan:{check}", "count": len(rows), "examples": [str(row[0]) for row in rows[:10]]})
            for check, (table, columns) in DUPLICATE_CHECKS.items():
                tenant_group = f"WHERE organization_id = :organization_id" if organization_id else ""
                duplicate_query = f"""
                    SELECT {columns}, COUNT(*) AS duplicate_count FROM {table}
                    {tenant_group}
                    {"AND " if tenant_group else "WHERE "}{"external_id IS NOT NULL" if "external_id" in columns else "1=1"}
                    GROUP BY {columns} HAVING COUNT(*) > 1
                    LIMIT 10
                """
                rows = connection.execute(text(duplicate_query), params).fetchall()
                if rows:
                    violations.append({"check": f"duplicate:{check}", "count": len(rows), "examples": [list(row) for row in rows[:10]]})

            # A primary attribution must point to a paid transaction and cannot exceed it.
            tenant_filter = "AND a.organization_id = :organization_id" if organization_id else ""
            rows = connection.execute(text(f"""
                SELECT a.id FROM attribution_links a
                JOIN transactions t ON t.organization_id = a.organization_id AND t.id = a.transaction_id
                WHERE a.primary_attribution = 1 AND t.status <> 'paid' {tenant_filter}
                LIMIT 10
            """), params).fetchall()
            if rows:
                violations.append({"check": "state.primary_attribution_requires_paid_transaction", "count": len(rows), "examples": [str(row[0]) for row in rows[:10]]})

            rows = connection.execute(text(f"""
                SELECT a.id FROM attribution_links a
                JOIN transactions t ON t.organization_id = a.organization_id AND t.id = a.transaction_id
                WHERE a.primary_attribution = 1 AND t.status = 'paid'
                GROUP BY a.id, t.amount
                HAVING a.attributed_amount > t.amount {tenant_filter}
            """), params).fetchall()
            if rows:
                violations.append({"check": "state.attribution_amount_cannot_exceed_transaction", "count": len(rows), "examples": [str(row[0]) for row in rows[:10]]})

            rows = connection.execute(text(f"""
                SELECT o.id FROM revenue_opportunities o
                LEFT JOIN attribution_links a ON a.organization_id = o.organization_id AND a.opportunity_id = o.id AND a.primary_attribution = 1
                WHERE 1=1 {"AND o.organization_id = :organization_id" if organization_id else ""}
                GROUP BY o.id, o.recovered_amount
                HAVING ABS(o.recovered_amount - COALESCE(SUM(a.attributed_amount), 0)) > 0.01
            """), params).fetchall()
            if rows:
                violations.append({"check": "state.opportunity_recovered_amount_matches_primary_ledger", "count": len(rows), "examples": [str(row[0]) for row in rows[:10]]})

            rows = connection.execute(text(f"""
                SELECT a.id FROM recovery_approvals a
                JOIN recovery_actions x ON x.organization_id = a.organization_id AND x.id = a.action_id
                WHERE a.decision = 'approved' AND x.status IN ('failed', 'cancelled') {tenant_filter}
                LIMIT 10
            """), params).fetchall()
            if rows:
                violations.append({"check": "state.approved_action_cannot_be_terminal_failure", "count": len(rows), "examples": [str(row[0]) for row in rows[:10]]})
    except Exception as exc:
        violations.append({"check": "scanner.error", "count": 1, "examples": [repr(exc)]})
    finally:
        engine.dispose()
    return _report(database_url, organization_id, violations)


def _report(database_url: str, organization_id: str | None, violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "report": "rros-data-integrity",
        "database": database_url.split("?")[0].replace(os.getenv("HOME", "__never__"), "~"),
        "organization_id": organization_id,
        "passed": not violations,
        "violation_count": len(violations),
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", "sqlite:///./revenue_recovery.db"))
    parser.add_argument("--organization-id")
    parser.add_argument("--json", action="store_true", help="emit only machine-readable JSON")
    args = parser.parse_args()
    report = scan(args.database_url, args.organization_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
