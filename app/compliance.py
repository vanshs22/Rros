"""
Communication Compliance System - Point 32 Implementation

Advanced compliance checking beyond basic consent:
- Regulatory compliance (TCPA, CAN-SPAM, GDPR, CCPA)
- Content analysis and risk assessment
- Channel-specific rules and best practices
- Customer preference enforcement
"""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import (
    ChannelConsent,
    Customer,
    CommunicationEvent,
    RecoveryAction,
    Organization,
    Location,
    BusinessPolicy,
    AuditLog,
)


class ComplianceViolation(Exception):
    """Raised when a compliance check fails."""
    
    def __init__(self, code: str, message: str, regulation: str, severity: str = "high"):
        self.code = code
        self.message = message
        self.regulation = regulation
        self.severity = severity
        super().__init__(f"[{code}] {message} ({regulation})")


class CommunicationComplianceChecker:
    """Advanced compliance checking for all communications."""
    
    def __init__(self, db: Session, organization_id: str):
        self.db = db
        self.org_id = organization_id
        self.violations: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.compliance_score = 100.0
        
        # Load organization details
        self.organization = self.db.scalar(
            select(Organization).where(Organization.id == self.org_id)
        )
        
        # Regulatory jurisdictions
        self.jurisdictions = self._detect_jurisdictions()
    
    def validate_communication(
        self,
        channel: str,
        direction: str,
        customer_id: Optional[str] = None,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        is_marketing: bool = False
    ) -> Dict[str, Any]:
        """
        Validate communication for compliance across multiple regulations.
        """
        self.violations = []
        self.warnings = []
        self.compliance_score = 100.0
        
        start_time = time.time()
        
        # Layer 1: Basic channel and direction validation
        self._validate_channel_direction(channel, direction)
        
        # Layer 2: Customer-specific checks
        if customer_id:
            customer = self._get_customer(customer_id)
            if customer:
                self._check_customer_compliance(channel, direction, customer, is_marketing)
        
        # Layer 3: Content analysis
        if content:
            self._analyze_content(channel, content, is_marketing)
        
        # Layer 4: Regulatory compliance
        self._check_regulatory_compliance(channel, direction, customer_id, content, is_marketing)
        
        # Layer 5: Business policy enforcement
        self._enforce_business_policies(channel, direction, customer_id, content, metadata, is_marketing)
        
        # Layer 6: Rate limiting and frequency controls
        if customer_id:
            self._check_communication_frequency(channel, customer_id)
        
        validation_time = time.time() - start_time
        
        # Compile compliance report
        report = {
            "passed": len(self.violations) == 0,
            "compliance_score": self.compliance_score,
            "violations": self.violations,
            "warnings": self.warnings,
            "jurisdictions": self.jurisdictions,
            "validation_time_ms": round(validation_time * 1000, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Record compliance audit
        self._record_compliance_audit(channel, direction, report, customer_id)
        
        return report
    
    def _detect_jurisdictions(self) -> Set[str]:
        """Detect applicable regulatory jurisdictions."""
        jurisdictions = set()
        
        # Default to US regulations
        jurisdictions.add("US")
        
        # Check organization location for additional jurisdictions
        if self.organization and self.organization.brand_name:
            # Simple detection based on brand/domain
            brand_lower = self.organization.brand_name.lower()
            
            # EU detection
            eu_keywords = ["eu", "europe", ".de", ".fr", ".es", ".it", ".nl", ".pl"]
            if any(keyword in brand_lower for keyword in eu_keywords):
                jurisdictions.add("EU")
            
            # California detection
            ca_keywords = ["california", "san francisco", "los angeles", "silicon valley"]
            if any(keyword in brand_lower for keyword in ca_keywords):
                jurisdictions.add("CA_US")

            # Canada detection
            canada_keywords = ["canada", ".ca", "toronto", "vancouver", "montreal"]
            if any(keyword in brand_lower for keyword in canada_keywords):
                jurisdictions.add("CANADA")

        return jurisdictions

    def _validate_channel_direction(self, channel: str, direction: str):
        """Validate channel and direction combinations."""
        
        valid_channels = {"sms", "email", "whatsapp", "voice"}
        if channel not in valid_channels:
            self._add_violation(
                "INVALID_CHANNEL",
                f"Invalid communication channel: {channel}",
                "internal",
                "high"
            )
        
        valid_directions = {"inbound", "outbound"}
        if direction not in valid_directions:
            self._add_violation(
                "INVALID_DIRECTION",
                f"Invalid communication direction: {direction}",
                "internal",
                "high"
            )
    
    def _get_customer(self, customer_id: str) -> Optional[Customer]:
        """Get customer with location information."""
        return self.db.scalar(
            select(Customer).where(
                Customer.organization_id == self.org_id,
                Customer.id == customer_id
            )
        )
    
    def _check_customer_compliance(
        self,
        channel: str,
        direction: str,
        customer: Customer,
        is_marketing: bool
    ):
        """Check customer-specific compliance requirements."""
        
        # Check consent
        consent = self.db.scalar(
            select(ChannelConsent).where(
                ChannelConsent.organization_id == self.org_id,
                ChannelConsent.customer_id == customer.id,
                ChannelConsent.channel == channel,
                ChannelConsent.allowed == True
            )
        )
        
        if not consent:
            self._add_violation(
                "CONSENT_REQUIRED",
                f"Customer has not consented to {channel} communications",
                "TCPA/CASL/GDPR",
                "critical"
            )
        
        # Check for explicit marketing consent if this is marketing communication
        if is_marketing and consent:
            # Check if consent source indicates marketing consent
            if consent.source not in ["opt_in", "double_opt_in", "explicit_marketing"]:
                self._add_violation(
                    "MARKETING_CONSENT_REQUIRED",
                    "Marketing communications require explicit marketing consent",
                    "CAN-SPAM/GDPR",
                    "critical"
                )
        
        # Check Do Not Call list
        if channel == "voice" and customer.lifecycle_status == "dnc":
            self._add_violation(
                "DNC_VIOLATION",
                "Customer is on Do Not Call list",
                "TCPA",
                "critical"
            )
        
        # Check customer preferences from metadata if available
        if customer.email and channel == "email":
            # Check for unsubscribe requests
            recent_unsub = self.db.scalar(
                select(CommunicationEvent).where(
                    CommunicationEvent.organization_id == self.org_id,
                    CommunicationEvent.customer_id == customer.id,
                    CommunicationEvent.channel == "email",
                    CommunicationEvent.direction == "inbound",
                    CommunicationEvent.subject.ilike("%unsubscribe%")
                ).order_by(CommunicationEvent.occurred_at.desc()).limit(1)
            )
            if recent_unsub:
                hours_since = (datetime.now(timezone.utc) - recent_unsub.occurred_at).total_seconds() / 3600
                if hours_since < 24:
                    self._add_violation(
                        "RECENT_UNSUBSCRIBE",
                        "Customer recently requested to unsubscribe",
                        "CAN-SPAM",
                        "critical"
                    )
    
    def _analyze_content(self, channel: str, content: str, is_marketing: bool):
        """Analyze communication content for compliance issues."""
        
        content_lower = content.lower()
        
        # Check for deceptive subject lines (CAN-SPAM)
        if channel == "email" and is_marketing:
            # Extract subject if available
            subject_match = re.search(r"subject:\s*(.+)", content_lower)
            if subject_match:
                subject = subject_match.group(1)
                
                # Check for misleading subject lines
                misleading_patterns = [
                    r"re:\s*",  # Fake replies
                    r"fwd:\s*",  # Fake forwards
                    r"urgent",  # Fake urgency
                    r"final notice",  # Fake final notices
                ]
                
                for pattern in misleading_patterns:
                    if re.search(pattern, subject, re.IGNORECASE):
                        self._add_violation(
                            "DECEPTIVE_SUBJECT",
                            "Subject line may be deceptive or misleading",
                            "CAN-SPAM",
                            "high"
                        )
                        break
        
        # Check for required disclosures
        if is_marketing:
            required_disclosures = {
                "email": [
                    ("unsubscribe", "Missing unsubscribe mechanism"),
                    ("physical address", "Missing physical postal address"),
                ],
                "sms": [
                    ("stop", "Missing STOP keyword instructions"),
                    ("help", "Missing HELP keyword instructions"),
                ]
            }
            
            if channel in required_disclosures:
                for keyword, message in required_disclosures[channel]:
                    if keyword not in content_lower:
                        self._add_violation(
                            "MISSING_DISCLOSURE",
                            message,
                            "CAN-SPAM/TCPA",
                            "high"
                        )
        
        # Check for prohibited content
        prohibited_patterns = [
            (r"guarantee.*profit", "Promises of guaranteed profits"),
            (r"risk.*free", "Misleading risk-free claims"),
            (r"no.*investment", "Claims of no investment required"),
            (r"get.*rich.*quick", "Get rich quick schemes"),
        ]
        
        for pattern, description in prohibited_patterns:
            if re.search(pattern, content_lower):
                self._add_violation(
                    "PROHIBITED_CONTENT",
                    f"Content contains prohibited claims: {description}",
                    "FTC",
                    "critical"
                )
        
        # Check for sensitive information
        sensitive_patterns = [
            (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "Potential phone number"),
            (r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b", "Potential SSN"),
            (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "Email address in content"),
        ]
        
        for pattern, description in sensitive_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                self._add_warning(
                    "SENSITIVE_INFO",
                    f"Content may contain {description}"
                )
    
    def _check_regulatory_compliance(
        self,
        channel: str,
        direction: str,
        customer_id: Optional[str],
        content: Optional[str],
        is_marketing: bool
    ):
        """Check regulatory compliance based on detected jurisdictions."""
        
        # US Regulations
        if "US" in self.jurisdictions:
            self._check_us_compliance(channel, direction, is_marketing)
        
        # EU Regulations (GDPR)
        if "EU" in self.jurisdictions:
            self._check_eu_compliance(channel, direction, customer_id, is_marketing)
        
        # California Regulations (CCPA)
        if "CA_US" in self.jurisdictions:
            self._check_ca_compliance(channel, direction, customer_id)
        
        # Canada Regulations (CASL)
        if "CANADA" in self.jurisdictions:
            self._check_canada_compliance(channel, direction, is_marketing)
    
    def _check_us_compliance(self, channel: str, direction: str, is_marketing: bool):
        """Check US-specific regulations."""
        
        # TCPA for voice and SMS
        if channel in ["voice", "sms"] and direction == "outbound" and is_marketing:
            # TCPA requires express written consent for telemarketing
            self._add_violation(
                "TCPA_CONSENT_REQUIRED",
                "TCPA requires express written consent for telemarketing calls/SMS",
                "TCPA",
                "critical"
            )
        
        # CAN-SPAM for email
        if channel == "email" and direction == "outbound" and is_marketing:
            # CAN-SPAM specific requirements checked in content analysis
            pass
    
    def _check_eu_compliance(
        self,
        channel: str,
        direction: str,
        customer_id: Optional[str],
        is_marketing: bool
    ):
        """Check EU GDPR compliance."""
        
        if direction == "outbound" and is_marketing and customer_id:
            # GDPR requires explicit opt-in for marketing
            self._add_violation(
                "GDPR_EXPLICIT_CONSENT",
                "GDPR requires explicit opt-in consent for marketing communications",
                "GDPR",
                "critical"
            )
        
        # Check for privacy policy reference
        if channel == "email" and direction == "outbound":
            self._add_warning(
                "GDPR_PRIVACY_NOTICE",
                "GDPR requires privacy notice in commercial communications"
            )
    
    def _check_ca_compliance(self, channel: str, direction: str, customer_id: Optional[str]):
        """Check California CCPA compliance."""
        
        if direction == "outbound" and customer_id:
            # CCPA requires "Do Not Sell My Personal Information" option
            self._add_warning(
                "CCPA_DNS_OPTION",
                "CCPA requires 'Do Not Sell My Personal Information' option"
            )
    
    def _check_canada_compliance(self, channel: str, direction: str, is_marketing: bool):
        """Check Canada CASL compliance."""
        
        if direction == "outbound" and is_marketing:
            # CASL requires explicit consent and identification
            self._add_violation(
                "CASL_CONSENT_REQUIRED",
                "CASL requires express consent for commercial electronic messages",
                "CASL",
                "critical"
            )
    
    def _enforce_business_policies(
        self,
        channel: str,
        direction: str,
        customer_id: Optional[str],
        content: Optional[str],
        metadata: Optional[Dict[str, Any]],
        is_marketing: bool
    ):
        """Enforce organization-specific business policies."""
        
        policies = self.db.scalars(
            select(BusinessPolicy).where(
                BusinessPolicy.organization_id == self.org_id,
                BusinessPolicy.active == True,
                BusinessPolicy.policy_type.in_(["compliance", "communication", "safety"]),
                BusinessPolicy.effective_from <= datetime.now(timezone.utc),
                (BusinessPolicy.expires_at.is_(None) | (BusinessPolicy.expires_at > datetime.now(timezone.utc)))
            )
        ).all()
        
        for policy in policies:
            if self._communication_policy_applies(policy, channel, direction, is_marketing):
                if not self._evaluate_policy_conditions(policy, customer_id, content, metadata):
                    self._add_violation(
                        "BUSINESS_POLICY_VIOLATION",
                        f"Violates business policy: {policy.name}",
                        "internal",
                        policy.severity
                    )
    
    def _communication_policy_applies(
        self,
        policy: BusinessPolicy,
        channel: str,
        direction: str,
        is_marketing: bool
    ) -> bool:
        """Check if a policy applies to this communication."""
        try:
            config = json.loads(policy.config_json)
        except (TypeError, json.JSONDecodeError):
            return False
        
        # Check channel restrictions
        if "channels" in config and channel not in config["channels"]:
            return False
        
        # Check direction restrictions
        if "directions" in config and direction not in config["directions"]:
            return False
        
        # Check marketing flag
        if "marketing_only" in config and config["marketing_only"] != is_marketing:
            return False
        
        return True
    
    def _evaluate_policy_conditions(
        self,
        policy: BusinessPolicy,
        customer_id: Optional[str],
        content: Optional[str],
        metadata: Optional[Dict[str, Any]]
    ) -> bool:
        """Evaluate only declarative, bounded communication policy controls."""
        try:
            config = json.loads(policy.config_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if config.get("deny") is True:
            return False
        normalized = (content or "").lower()
        blocked_terms = [str(x).lower() for x in config.get("blocked_terms", [])]
        if any(term and term in normalized for term in blocked_terms):
            return False
        required_metadata = config.get("required_metadata", [])
        if any(not metadata or not metadata.get(str(key)) for key in required_metadata):
            return False
        return True
    
    def _check_communication_frequency(self, channel: str, customer_id: str):
        """Check communication frequency limits."""
        
        # Check last 24 hours
        day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
        
        recent_count = self.db.scalar(
            select(func.count(CommunicationEvent.id)).where(
                CommunicationEvent.organization_id == self.org_id,
                CommunicationEvent.customer_id == customer_id,
                CommunicationEvent.channel == channel,
                CommunicationEvent.direction == "outbound",
                CommunicationEvent.occurred_at >= day_ago,
                CommunicationEvent.status.in_(["sent", "delivered"])
            )
        ) or 0
        
        # Channel-specific limits
        limits = {
            "sms": 3,
            "email": 5,
            "whatsapp": 10,
            "voice": 2,
        }
        
        limit = limits.get(channel, 3)
        
        if recent_count >= limit:
            self._add_violation(
                "FREQUENCY_LIMIT_EXCEEDED",
                f"Communication frequency limit exceeded for {channel} ({limit}/24h)",
                "internal",
                "medium"
            )
        elif recent_count >= limit * 0.8:
            self._add_warning(
                "FREQUENCY_LIMIT_APPROACHING",
                f"Communication frequency approaching limit for {channel}"
            )
    
    def _record_compliance_audit(
        self,
        channel: str,
        direction: str,
        report: Dict[str, Any],
        customer_id: Optional[str]
    ):
        """Record compliance audit trail."""
        audit = AuditLog(
            organization_id=self.org_id,
            action="compliance_validation",
            resource_type="communication",
            resource_id=customer_id or "system",
            metadata_json=json.dumps({
                "channel": channel,
                "direction": direction,
                "compliance_report": report,
                "customer_id": customer_id,
                "violation_count": len(self.violations),
                "warning_count": len(self.warnings),
            }, default=str)
        )
        self.db.add(audit)
    
    def _add_violation(self, code: str, message: str, regulation: str, severity: str = "high"):
        """Add a compliance violation."""
        self.violations.append({
            "code": code,
            "message": message,
            "regulation": regulation,
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        # Deduct from compliance score
        deductions = {
            "low": 10.0,
            "medium": 25.0,
            "high": 50.0,
            "critical": 75.0,
        }
        self.compliance_score -= deductions.get(severity, 25.0)
    
    def _add_warning(self, code: str, message: str):
        """Add a compliance warning."""
        self.warnings.append({
            "code": code,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        self.compliance_score -= 5.0


def validate_communication_compliance(
    db: Session,
    organization_id: str,
    channel: str,
    direction: str,
    customer_id: Optional[str] = None,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    is_marketing: bool = False
) -> Tuple[bool, Dict[str, Any]]:
    """
    Convenience function to validate communication compliance.
    Returns (is_compliant, compliance_report)
    """
    checker = CommunicationComplianceChecker(db, organization_id)
    report = checker.validate_communication(
        channel=channel,
        direction=direction,
        customer_id=customer_id,
        content=content,
        metadata=metadata,
        is_marketing=is_marketing
    )
    
    is_compliant = report["passed"] and report["compliance_score"] >= 70.0
    
    return is_compliant, report


def get_required_disclosures(channel: str, jurisdictions: Set[str], is_marketing: bool) -> List[str]:
    """
    Get required disclosures for a communication based on channel and jurisdictions.
    """
    disclosures = []
    
    if not is_marketing:
        return disclosures
    
    # US disclosures
    if "US" in jurisdictions:
        if channel == "email":
            disclosures.extend([
                "Clear and conspicuous unsubscribe mechanism",
                "Valid physical postal address",
                "Accurate header information",
                "Non-deceptive subject line",
            ])
        elif channel == "sms":
            disclosures.extend([
                "Clear instructions for opting out (e.g., 'Reply STOP to unsubscribe')",
                "Identification of sender",
            ])
        elif channel == "voice":
            disclosures.extend([
                "Clear identification of caller",
                "Purpose of call",
                "Option to opt-out",
            ])
    
    # EU disclosures
    if "EU" in jurisdictions:
        disclosures.extend([
            "Privacy notice",
            "Data controller identity",
            "Purpose of processing",
        ])
    
    # California disclosures
    if "CA_US" in jurisdictions and "US" in jurisdictions:
        disclosures.extend([
            "California privacy rights notice",
            "'Do Not Sell My Personal Information' link",
        ])
    
    return disclosures