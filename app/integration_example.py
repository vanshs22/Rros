"""
Integration Example - How to use the new safety, compliance, and review systems together.

This shows the complete flow for processing a recovery action with full safety and compliance.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, Dict, Optional

# Example integration with existing recovery action flow
def process_recovery_action_with_safety(
    db,  # SQLAlchemy session
    organization_id: str,
    opportunity_id: str,
    action_type: str,
    channel: str,
    payload: Dict[str, Any],
    customer_id: str,
    agent_run_id: Optional[str] = None,
    autonomy_mode: str = "recommend"
) -> Dict[str, Any]:
    """
    Complete recovery action processing with integrated safety and compliance.
    
    This function replaces the existing action creation with full safety enforcement.
    """
    from .safety import create_safety_enforced_action, validate_recovery_action
    from .compliance import validate_communication_compliance
    from .models import RecoveryAction, RecoveryApproval
    
    result = {
        "success": False,
        "action_id": None,
        "needs_approval": False,
        "safety_report": None,
        "compliance_report": None,
        "errors": [],
    }
    
    try:
        # Step 1: Safety Validation
        is_safe, safety_report = validate_recovery_action(
            db=db,
            organization_id=organization_id,
            action_type=action_type,
            channel=channel,
            customer_id=customer_id,
            opportunity_id=opportunity_id,
            payload=payload,
            agent_run_id=agent_run_id,
            autonomy_mode=autonomy_mode
        )
        
        result["safety_report"] = safety_report
        
        if not is_safe:
            result["errors"].append({
                "code": "SAFETY_VALIDATION_FAILED",
                "message": "Action failed safety validation",
                "violations": safety_report.get("violations", [])
            })
            return result
        
        # Step 2: Compliance Validation (for communications)
        if channel in ["sms", "email", "whatsapp", "voice"]:
            content = payload.get("message", "") if isinstance(payload, dict) else ""
            is_compliant, compliance_report = validate_communication_compliance(
                db=db,
                organization_id=organization_id,
                channel=channel,
                direction="outbound",
                customer_id=customer_id,
                content=content,
                is_marketing=payload.get("is_marketing", False) if isinstance(payload, dict) else False
            )
            
            result["compliance_report"] = compliance_report
            
            if not is_compliant:
                result["errors"].append({
                    "code": "COMPLIANCE_VALIDATION_FAILED",
                    "message": "Action failed compliance validation",
                    "violations": compliance_report.get("violations", [])
                })
                return result
        
        # Step 3: Create Safety-Enforced Action
        action, final_safety_report = create_safety_enforced_action(
            db=db,
            organization_id=organization_id,
            opportunity_id=opportunity_id,
            action_type=action_type,
            channel=channel,
            payload=payload,
            autonomy_mode=autonomy_mode,
            agent_run_id=agent_run_id
        )
        
        if not action:
            result["errors"].append({
                "code": "ACTION_CREATION_FAILED",
                "message": "Failed to create safety-enforced action"
            })
            return result
        
        result["action_id"] = action.id
        result["needs_approval"] = action.status == "awaiting_approval"
        result["safety_report"] = final_safety_report
        
        # Step 4: Handle approval workflow if needed
        if result["needs_approval"]:
            # In a real implementation, this would trigger approval notifications
            result["approval_workflow"] = {
                "status": "pending",
                "message": "Action requires human approval",
                "approval_url": f"/approvals/{action.id}",  # Example
            }
        
        # Step 5: If approved and safe, queue for execution
        if action.status == "approved":
            # Queue for execution (example)
            from .queue import enqueue_job
            enqueue_job(
                db,
                "execute_recovery_action",
                organization_id,
                resource=action.id,
                payload={"action_id": action.id}
            )
            result["execution_queued"] = True
        
        result["success"] = True
        
    except Exception as e:
        result["errors"].append({
            "code": "PROCESSING_ERROR",
            "message": str(e),
            "type": type(e).__name__
        })
    
    return result


def process_review_with_analysis(
    db,
    organization_id: str,
    review_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Complete review processing with integrated analysis pipeline.
    """
    from .review_system import ReviewPipeline
    from .models import Review
    
    result = {
        "success": False,
        "review_id": None,
        "pipeline_result": None,
        "response_generated": False,
        "opportunity_created": False,
        "errors": [],
    }
    
    try:
        # Create review record
        review = Review(
            id=f"rev_{datetime.now(timezone.utc).timestamp()}_{hash(str(review_data))[:8]}",
            organization_id=organization_id,
            customer_id=review_data.get("customer_id"),
            external_id=review_data.get("external_id"),
            source=review_data.get("source", "unknown"),
            rating=review_data.get("rating", 0),
            text=review_data.get("text", ""),
            topic=review_data.get("topic"),
            severity=review_data.get("severity"),
            source_url=review_data.get("source_url"),
        )
        
        db.add(review)
        db.flush()
        
        result["review_id"] = review.id
        
        # Process through review pipeline
        pipeline = ReviewPipeline(db, organization_id)
        pipeline_result = pipeline.process_review(review)
        
        result["pipeline_result"] = pipeline_result
        result["response_generated"] = pipeline_result.get("outcomes", {}).get("response_created", False)
        result["opportunity_created"] = pipeline_result.get("outcomes", {}).get("opportunity_created", False)
        
        # Commit changes
        db.commit()
        
        result["success"] = True
        
    except Exception as e:
        db.rollback()
        result["errors"].append({
            "code": "REVIEW_PROCESSING_ERROR",
            "message": str(e),
            "type": type(e).__name__
        })
    
    return result


def get_safety_dashboard_data(
    db,
    organization_id: str,
    days: int = 7
) -> Dict[str, Any]:
    """
    Get dashboard data for safety and compliance metrics.
    """
    from datetime import timedelta
    from sqlalchemy import func, select
    from .models import AuditLog, Review, ReviewResponse

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))
    audits = db.scalars(select(AuditLog).where(
        AuditLog.organization_id == organization_id,
        AuditLog.created_at >= cutoff,
        AuditLog.action.in_(["safety_validation", "compliance_validation"]),
    )).all()
    safety = [a for a in audits if a.action == "safety_validation"]
    compliance = [a for a in audits if a.action == "compliance_validation"]
    safety_scores = []
    violation_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    compliance_issues = {}
    for row in audits:
        try:
            report = json.loads(row.metadata_json or "{}").get("safety_report") or json.loads(row.metadata_json or "{}").get("compliance_report") or {}
        except json.JSONDecodeError:
            report = {}
        if row.action == "safety_validation" and report.get("safety_score") is not None:
            safety_scores.append(float(report["safety_score"]))
        for violation in report.get("violations", []):
            severity = violation.get("severity", "medium")
            if row.action == "safety_validation":
                violation_counts[severity] = violation_counts.get(severity, 0) + 1
            else:
                code = violation.get("code", "unknown")
                compliance_issues[code] = compliance_issues.get(code, 0) + 1

    reviews = db.scalars(select(Review).where(Review.organization_id == organization_id, Review.created_at >= cutoff)).all()
    responses = db.scalar(select(func.count(ReviewResponse.id)).where(
        ReviewResponse.organization_id == organization_id,
        ReviewResponse.created_at >= cutoff,
        ReviewResponse.status.in_(["approved", "sent"]),
    )) or 0
    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    for review in reviews:
        key = "positive" if review.rating >= 4 else "neutral" if review.rating == 3 else "negative"
        sentiment[key] += 1
    return {
        "time_period_days": days,
        "safety_metrics": {
            "total_actions_validated": len(safety),
            "safety_pass_rate": round(sum(1 for row in safety if not json.loads(row.metadata_json or "{}").get("safety_report", {}).get("violations")) / len(safety) * 100, 2) if safety else 0.0,
            "average_safety_score": round(sum(safety_scores) / len(safety_scores), 2) if safety_scores else 0.0,
            "violations_by_severity": violation_counts,
            "top_violation_codes": sorted({v.get("code", "unknown") for row in safety for v in json.loads(row.metadata_json or "{}").get("safety_report", {}).get("violations", [])}),
        },
        "compliance_metrics": {
            "total_comms_validated": len(compliance),
            "compliance_pass_rate": round(sum(1 for row in compliance if not json.loads(row.metadata_json or "{}").get("compliance_report", {}).get("violations")) / len(compliance) * 100, 2) if compliance else 0.0,
            "top_compliance_issues": sorted(compliance_issues, key=compliance_issues.get, reverse=True)[:10],
        },
        "review_metrics": {
            "total_reviews": len(reviews),
            "average_rating": round(sum(review.rating for review in reviews) / len(reviews), 2) if reviews else 0.0,
            "response_rate": round(responses / len(reviews) * 100, 2) if reviews else 0.0,
            "sentiment_distribution": sentiment,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Example usage in FastAPI endpoint
"""
@app.post("/api/opportunities/{opportunity_id}/recovery-actions")
async def create_recovery_action(
    opportunity_id: str,
    request: RecoveryActionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):
    # Process with full safety and compliance
    result = process_recovery_action_with_safety(
        db=db,
        organization_id=user.organization_id,
        opportunity_id=opportunity_id,
        action_type=request.action_type,
        channel=request.channel,
        payload=request.payload,
        customer_id=request.customer_id,
        agent_run_id=request.agent_run_id,
        autonomy_mode=request.autonomy_mode
    )
    
    if result["success"]:
        return {
            "action_id": result["action_id"],
            "needs_approval": result["needs_approval"],
            "safety_score": result["safety_report"]["safety_score"],
            "approval_workflow": result.get("approval_workflow"),
        }
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "errors": result["errors"],
                "safety_report": result["safety_report"],
                "compliance_report": result.get("compliance_report"),
            }
        )
"""

