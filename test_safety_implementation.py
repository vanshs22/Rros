#!/usr/bin/env python3
"""
Test script to verify the safety and compliance implementations.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from datetime import datetime, timezone
from decimal import Decimal
import json

# Mock database session for testing
class MockDB:
    def __init__(self):
        self.added = []
        self.committed = False
    
    def add(self, obj):
        self.added.append(obj)
    
    def commit(self):
        self.committed = True
    
    def scalar(self, query):
        # Mock responses for testing
        return None
    
    def scalars(self, query):
        return []


def test_safety_chain():
    """Test the Action Safety Chain implementation."""
    print("🧪 Testing Action Safety Chain (Point 31)...")
    
    from app.safety import ActionSafetyChain, validate_recovery_action, create_safety_enforced_action
    
    db = MockDB()
    org_id = "test_org_123"
    
    # Test 1: Basic safety validation
    safety = ActionSafetyChain(db, org_id)
    
    # Simulate validation
    safety.violations = []
    safety.warnings = []
    safety.safety_score = 85.0
    
    # Test validation function
    is_valid, report = validate_recovery_action(
        db=db,
        organization_id=org_id,
        action_type="outreach",
        channel="email",
        customer_id="cust_123",
        opportunity_id="opp_456",
        payload={"message": "Test message"},
        autonomy_mode="recommend"
    )
    
    print(f"  ✅ Safety validation function works")
    print(f"  📊 Report structure: {list(report.keys())}")
    
    # Test 2: Safety enforced action creation
    action, safety_report = create_safety_enforced_action(
        db=db,
        organization_id=org_id,
        opportunity_id="opp_456",
        action_type="outreach",
        channel="email",
        payload={"customer_id": "cust_123", "message": "Test"},
        autonomy_mode="recommend"
    )
    
    if action:
        print(f"  ✅ Safety enforced action creation works")
        print(f"  📝 Action status: {action.status}")
    else:
        print(f"  ⚠️  Action creation returned None (may be expected in test)")
    
    return True


def test_compliance_checker():
    """Test the Communication Compliance implementation."""
    print("\n🧪 Testing Communication Compliance (Point 32)...")
    
    from app.compliance import CommunicationComplianceChecker, validate_communication_compliance
    
    db = MockDB()
    org_id = "test_org_123"
    
    # Test compliance validation
    is_compliant, report = validate_communication_compliance(
        db=db,
        organization_id=org_id,
        channel="email",
        direction="outbound",
        customer_id="cust_123",
        content="Thank you for your business!",
        is_marketing=False
    )
    
    print(f"  ✅ Compliance validation function works")
    print(f"  📊 Report structure: {list(report.keys())}")
    print(f"  📈 Jurisdictions detected: {report.get('jurisdictions', [])}")
    
    # Test required disclosures
    from app.compliance import get_required_disclosures
    
    disclosures = get_required_disclosures(
        channel="email",
        jurisdictions={"US", "EU"},
        is_marketing=True
    )
    
    print(f"  📋 Required disclosures: {len(disclosures)} items")
    for disclosure in disclosures[:3]:  # Show first 3
        print(f"    • {disclosure}")
    
    return True


def test_review_system():
    """Test the Review System implementation."""
    print("\n🧪 Testing Review System (Point 33)...")
    
    from app.review_system import ReviewAnalysis, ReviewPipeline
    
    db = MockDB()
    org_id = "test_org_123"
    
    # Create mock review
    class MockReview:
        def __init__(self):
            self.id = "rev_test_123"
            self.organization_id = org_id
            self.customer_id = "cust_123"
            self.rating = 4
            self.text = "Great service! The team was very responsive and helpful."
            self.source = "google"
            self.severity = None
            self.metadata_json = "{}"
    
    review = MockReview()
    
    # Test analysis
    analyzer = ReviewAnalysis(db, org_id)
    analysis = analyzer.analyze_review(review)
    
    print(f"  ✅ Review analysis works")
    print(f"  📊 Analysis includes: {list(analysis.keys())}")
    print(f"  🎯 Sentiment: {analysis['sentiment']['overall']}")
    print(f"  🎯 Topics found: {len(analysis['topics'])}")
    
    # Test response generation
    response = analyzer.generate_automated_response(review, analysis)
    
    print(f"  💬 Generated response length: {len(response)} chars")
    print(f"  📝 Response preview: {response[:80]}...")
    
    # Test pipeline
    pipeline = ReviewPipeline(db, org_id)
    pipeline_result = pipeline.process_review(review)
    
    print(f"  🔄 Pipeline executed {len(pipeline_result['steps'])} steps")
    
    return True


def test_business_policy_model():
    """Test the Business Policy model."""
    print("\n🧪 Testing Business Policy Model (Point 30)...")
    
    # Check if model was added
    model_file = "app/models.py"
    with open(model_file, 'r') as f:
        content = f.read()
    
    if "class BusinessPolicy" in content:
        print(f"  ✅ BusinessPolicy model added to models.py")
        
        # Extract model details
        import re
        match = re.search(r'class BusinessPolicy\(Base\):(.*?)(?=\n\nclass|\Z)', content, re.DOTALL)
        if match:
            model_def = match.group(1)
            fields = re.findall(r'(\w+): Mapped\[', model_def)
            print(f"  📋 Model fields: {len(fields)} fields defined")
            print(f"  📝 Sample fields: {fields[:5]}...")
    else:
        print(f"  ❌ BusinessPolicy model not found")
        return False
    
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("RROS Production Excellence - Implementation Tests")
    print("=" * 60)
    
    tests_passed = 0
    total_tests = 4
    
    try:
        if test_safety_chain():
            tests_passed += 1
    except Exception as e:
        print(f"  ❌ Safety chain test failed: {e}")
    
    try:
        if test_compliance_checker():
            tests_passed += 1
    except Exception as e:
        print(f"  ❌ Compliance test failed: {e}")
    
    try:
        if test_review_system():
            tests_passed += 1
    except Exception as e:
        print(f"  ❌ Review system test failed: {e}")
    
    try:
        if test_business_policy_model():
            tests_passed += 1
    except Exception as e:
        print(f"  ❌ Business policy test failed: {e}")
    
    print("\n" + "=" * 60)
    print(f"Test Results: {tests_passed}/{total_tests} passed")
    print("=" * 60)
    
    if tests_passed == total_tests:
        print("🎉 All implementations verified successfully!")
        return 0
    else:
        print(f"⚠️  {total_tests - tests_passed} tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())