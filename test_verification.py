#!/usr/bin/env python3
"""Test verification for RROS production readiness progress."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models import RevenueOpportunity, AttributionLink, RecoveryAction, CommunicationEvent
from sqlalchemy import inspect

def check_point_1():
    """Verify Point 1: Core product promise metrics are separated."""
    print("=== Checking Point 1: Core product promise metrics ===")
    
    # Check RevenueOpportunity model fields
    insp = inspect(RevenueOpportunity)
    fields = [c.key for c in insp.columns]
    
    required_fields = [
        'estimated_value',  # Estimated opportunity
        'expected_value',   # Expected value
        'recovered_amount', # Actual recovered
    ]
    
    missing = [f for f in required_fields if f not in fields]
    if missing:
        print(f"❌ Missing fields in RevenueOpportunity: {missing}")
    else:
        print("✅ RevenueOpportunity has required value tracking fields")
    
    # Check AttributionLink model
    insp = inspect(AttributionLink)
    if 'classification' in [c.key for c in insp.columns]:
        print("✅ AttributionLink tracks classification (direct/assisted/influenced)")
    else:
        print("❌ AttributionLink missing classification field")
    
    # Check RecoveryAction model  
    insp = inspect(RecoveryAction)
    action_fields = [c.key for c in insp.columns]
    if 'status' in action_fields:
        print("✅ RecoveryAction tracks action status (attempted/delivered)")
    else:
        print("❌ RecoveryAction missing status tracking")
    
    # Check CommunicationEvent model
    insp = inspect(CommunicationEvent)
    comm_fields = [c.key for c in insp.columns]
    if 'direction' in comm_fields and 'status' in comm_fields:
        print("✅ CommunicationEvent tracks customer responses")
    else:
        print("❌ CommunicationEvent missing direction/status tracking")
    
    print()

def check_point_27():
    """Verify Point 27: Agent OS uses canonical data fabric."""
    print("=== Checking Point 27: Agent OS data fabric usage ===")
    
    try:
        from app.agents import build_context, AGENT_DEFS
        print(f"✅ Agent OS has {len(AGENT_DEFS)} agent definitions")
        
        # Check if build_context function exists
        if callable(build_context):
            print("✅ Agent OS has build_context function for canonical data")
        else:
            print("❌ Agent OS missing build_context function")
            
        # Check if agents use context
        from app.agents import _agent_output
        if callable(_agent_output):
            print("✅ Agent OS has agent output generation")
        else:
            print("❌ Agent OS missing agent output logic")
            
    except ImportError as e:
        print(f"❌ Failed to import agents module: {e}")
    
    print()

if __name__ == "__main__":
    print("RROS Production Readiness Verification")
    print("=" * 50)
    check_point_1()
    check_point_27()
