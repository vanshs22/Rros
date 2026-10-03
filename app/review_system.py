"""
Review System with Automated Analysis Pipeline - Point 33 Implementation

Advanced review management system with:
- Multi-source review collection
- Sentiment and topic analysis
- Automated response generation
- Quality scoring and trend detection
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from .models import (
    Review,
    ReviewResponse,
    Customer,
    Organization,
    AgentRun,
    AgentDefinition,
    AuditLog,
    OpportunityEvent,
    RevenueOpportunity,
    Transaction,
)


class ReviewAnalysis:
    """Review analysis and automated response generation."""
    
    def __init__(self, db: Session, organization_id: str):
        self.db = db
        self.org_id = organization_id
        self.sentiment_thresholds = {
            "positive": 4.5,
            "neutral": 3.5,
            "negative": 2.5,
        }
    
    def analyze_review(self, review: Review) -> Dict[str, Any]:
        """
        Analyze a review for sentiment, topics, and actionable insights.
        """
        analysis = {
            "sentiment": self._analyze_sentiment(review),
            "topics": self._extract_topics(review),
            "urgency": self._calculate_urgency(review),
            "action_required": self._determine_action_required(review),
            "automated_response_possible": self._can_auto_respond(review),
            "quality_score": self._calculate_quality_score(review),
        }
        
        review.analysis_json = json.dumps(analysis)
        
        return analysis
    
    def _analyze_sentiment(self, review: Review) -> Dict[str, Any]:
        """Analyze review sentiment and emotional tone."""
        
        text = review.text.lower()
        rating = review.rating
        
        # Basic sentiment from rating
        if rating >= self.sentiment_thresholds["positive"]:
            sentiment = "positive"
        elif rating >= self.sentiment_thresholds["neutral"]:
            sentiment = "neutral"
        else:
            sentiment = "negative"
        
        # Emotional tone analysis
        emotional_tones = {
            "anger": len(re.findall(r"\b(mad|angry|furious|rage|hate)\b", text)),
            "frustration": len(re.findall(r"\b(frustrat|annoy|disappoint|upset)\b", text)),
            "satisfaction": len(re.findall(r"\b(happy|pleased|satisfied|great|excellent)\b", text)),
            "urgency": len(re.findall(r"\b(urgent|immediate|asap|emergency)\b", text)),
            "gratitude": len(re.findall(r"\b(thank|appreciate|grateful|awesome)\b", text)),
        }
        
        # Detect key phrases
        key_phrases = []
        
        # Service quality phrases
        service_phrases = [
            (r"excellent service", "excellent_service"),
            (r"poor service", "poor_service"),
            (r"slow response", "slow_response"),
            (r"quick help", "quick_help"),
            (r"professional", "professional"),
            (r"unprofessional", "unprofessional"),
        ]
        
        for pattern, label in service_phrases:
            if re.search(pattern, text):
                key_phrases.append(label)
        
        # Product quality phrases
        product_phrases = [
            (r"high quality", "high_quality"),
            (r"low quality", "low_quality"),
            (r"broken", "broken"),
            (r"defective", "defective"),
            (r"exceeds expectations", "exceeds_expectations"),
        ]
        
        for pattern, label in product_phrases:
            if re.search(pattern, text):
                key_phrases.append(label)
        
        # Price/value phrases
        price_phrases = [
            (r"good value", "good_value"),
            (r"overpriced", "overpriced"),
            (r"expensive", "expensive"),
            (r"worth it", "worth_it"),
        ]
        
        for pattern, label in price_phrases:
            if re.search(pattern, text):
                key_phrases.append(label)
        
        return {
            "overall": sentiment,
            "rating": rating,
            "emotional_tones": emotional_tones,
            "key_phrases": key_phrases,
            "confidence": 0.85,  # Placeholder for ML model confidence
        }
    
    def _extract_topics(self, review: Review) -> List[str]:
        """Extract topics from review text."""
        text = review.text.lower()
        topics = []
        
        # Topic patterns
        topic_patterns = {
            "customer_service": [
                r"customer service", r"support", r"help desk", r"representative",
                r"agent", r"rep", r"assistance", r"response time",
            ],
            "product_quality": [
                r"quality", r"durability", r"reliable", r"broken", r"defect",
                r"workmanship", r"material", r"construction",
            ],
            "price_value": [
                r"price", r"cost", r"value", r"expensive", r"cheap", 
                r"overpriced", r"worth", r"affordable",
            ],
            "delivery_shipping": [
                r"delivery", r"shipping", r"arrived", r"package", r"ship",
                r"transit", r"delivered", r"late", r"early",
            ],
            "communication": [
                r"communication", r"update", r"notified", r"informed",
                r"transparent", r"clear", r"confusing", r"vague",
            ],
            "technical_issues": [
                r"bug", r"crash", r"error", r"glitch", r"technical",
                r"software", r"hardware", r"malfunction",
            ],
        }
        
        for topic, patterns in topic_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    topics.append(topic)
                    break
        
        # Add severity as a topic if present
        if review.severity:
            topics.append(f"severity_{review.severity}")
        
        # Add source as a topic
        if review.source:
            topics.append(f"source_{review.source}")
        
        return list(set(topics))
    
    def _calculate_urgency(self, review: Review) -> str:
        """Calculate urgency level for response."""
        
        rating = review.rating
        text = review.text.lower()
        
        # High urgency for low ratings with emotional language
        if rating <= 2:
            urgency_keywords = ["emergency", "urgent", "immediate", "asap", "critical"]
            if any(keyword in text for keyword in urgency_keywords):
                return "high"
            return "medium"
        
        # Medium urgency for neutral ratings with issues
        elif rating <= 3:
            issue_keywords = ["issue", "problem", "concern", "complaint", "disappointed"]
            if any(keyword in text for keyword in issue_keywords):
                return "medium"
        
        # Low urgency for positive reviews
        return "low"
    
    def _determine_action_required(self, review: Review) -> Dict[str, Any]:
        """Determine what actions are required based on review analysis."""
        
        rating = review.rating
        text = review.text.lower()
        
        actions = {
            "human_review": False,
            "automated_response": False,
            "escalation": False,
            "follow_up": False,
            "opportunity_creation": False,
        }
        
        # Low ratings always need human review
        if rating <= 2:
            actions["human_review"] = True
            actions["opportunity_creation"] = True
        
        # High ratings with specific mentions might get automated response
        elif rating >= 4:
            actions["automated_response"] = True
        
        # Check for escalation triggers
        escalation_triggers = [
            r"legal", r"lawyer", r"sue", r"court", r"complaint",
            r"regulator", r"bbb", r"better business bureau",
            r"social media", r"facebook", r"twitter", r"instagram",
            r"news", r"journalist", r"reporter",
        ]
        
        for trigger in escalation_triggers:
            if re.search(trigger, text):
                actions["escalation"] = True
                actions["human_review"] = True
                break
        
        # Check for follow-up needs
        follow_up_triggers = [
            r"call me", r"contact me", r"reach out", r"follow up",
            r"get in touch", r"let me know", r"update me",
        ]
        
        for trigger in follow_up_triggers:
            if re.search(trigger, text):
                actions["follow_up"] = True
                break
        
        return actions
    
    def _can_auto_respond(self, review: Review) -> bool:
        """Determine if automated response is possible."""
        
        # Don't auto-respond to low ratings
        if review.rating <= 3:
            return False
        
        # Check for complex issues that need human attention
        complex_indicators = [
            r"complex", r"complicated", r"detailed", r"specific",
            r"technical", r"legal", r"financial", r"billing",
            r"refund", r"return", r"exchange", r"warranty",
        ]
        
        text = review.text.lower()
        for indicator in complex_indicators:
            if re.search(indicator, text):
                return False
        
        return True
    
    def _calculate_quality_score(self, review: Review) -> float:
        """Calculate quality score for review (helpfulness, detail, etc.)."""
        score = 0.0
        
        # Length score (longer reviews are often more helpful)
        text_length = len(review.text)
        if text_length > 200:
            score += 0.3
        elif text_length > 100:
            score += 0.2
        elif text_length > 50:
            score += 0.1
        
        # Detail score (mentions specific aspects)
        detail_indicators = [
            r"specifically", r"specifically", r"in particular",
            r"for example", r"such as", r"including",
        ]
        
        text_lower = review.text.lower()
        for indicator in detail_indicators:
            if re.search(indicator, text_lower):
                score += 0.2
                break
        
        # Constructive score (provides suggestions)
        constructive_indicators = [
            r"suggest", r"recommend", r"advise", r"could improve",
            r"would be better", r"consider", r"maybe",
        ]
        
        for indicator in constructive_indicators:
            if re.search(indicator, text_lower):
                score += 0.2
                break
        
        # Clarity score (well-written)
        clarity_indicators = [
            r"\. ", r", ", r"; ",  # Proper punctuation
            r"first", r"second", r"third",  # Organized thoughts
            r"however", r"therefore", r"consequently",  # Transition words
        ]
        
        clarity_count = 0
        for indicator in clarity_indicators:
            clarity_count += len(re.findall(indicator, text_lower))
        
        if clarity_count > 3:
            score += 0.3
        elif clarity_count > 1:
            score += 0.1
        
        return min(score, 1.0)
    
    def generate_automated_response(self, review: Review, analysis: Dict[str, Any]) -> str:
        """
        Generate an automated response based on review analysis.
        """
        sentiment = analysis["sentiment"]["overall"]
        rating = review.rating
        
        # Response templates
        templates = {
            "positive": {
                5: [
                    "Thank you so much for your perfect 5-star review! We're thrilled to hear about your excellent experience with us. Your feedback means the world to our team and motivates us to keep delivering outstanding service.",
                    "We're overjoyed to receive your 5-star rating! It's customers like you who make our work so rewarding. Thank you for taking the time to share your positive experience.",
                ],
                4: [
                    "Thank you for your 4-star review! We're glad to hear you had a great experience with us. We're always working to improve, and your feedback helps us get even better.",
                    "We appreciate your 4-star rating and positive feedback! It's wonderful to know we're meeting your expectations, and we'll continue striving for that 5-star experience.",
                ],
            },
            "neutral": {
                3: [
                    "Thank you for your review and for sharing your experience with us. We appreciate all feedback as it helps us identify areas where we can improve our service.",
                    "We value your 3-star review and the opportunity to learn from your experience. If there's anything specific we could have done better, please don't hesitate to reach out.",
                ],
            },
            "negative": {
                2: [
                    "Thank you for bringing this to our attention. We're sorry to hear about your experience and take your feedback very seriously. A member of our team will be reviewing your concerns shortly.",
                    "We apologize for falling short of your expectations. Your feedback is important to us, and we're committed to making things right. Our team will be in touch to address your concerns.",
                ],
                1: [
                    "We're deeply sorry to hear about your disappointing experience. This is not the standard we strive for, and we take your feedback very seriously. Our team is already looking into this matter and will reach out to you directly.",
                    "Please accept our sincere apologies for the poor experience you've described. We're addressing this immediately with our team and will contact you personally to resolve this situation.",
                ],
            },
        }
        
        # Select template based on sentiment and rating
        if sentiment in templates and rating in templates[sentiment]:
            import random
            template = random.choice(templates[sentiment][rating])
        else:
            # Default template
            template = "Thank you for your review. We value your feedback and will use it to improve our services."
        
        # Personalize with customer name if available
        if review.customer_id:
            customer = self.db.scalar(
                select(Customer).where(
                    Customer.organization_id == self.org_id,
                    Customer.id == review.customer_id
                )
            )
            if customer and customer.name:
                template = template.replace("Thank you", f"Thank you, {customer.name.split()[0]}")
        
        # Add organization signature
        org = self.db.scalar(
            select(Organization).where(Organization.id == self.org_id)
        )
        
        if org and org.brand_name:
            template += f"\n\nBest regards,\nThe {org.brand_name} Team"
        else:
            template += "\n\nBest regards,\nOur Team"
        
        return template
    
    def create_revenue_opportunity_from_review(self, review: Review, analysis: Dict[str, Any]) -> Optional[RevenueOpportunity]:
        """
        Create a revenue opportunity from a negative review.
        """
        if review.rating >= 3:  # Only create opportunities for negative reviews
            return None
        
        # Check if opportunity already exists
        existing = self.db.scalar(
            select(RevenueOpportunity).where(
                RevenueOpportunity.organization_id == self.org_id,
                RevenueOpportunity.detection_key == f"review:{review.id}"
            )
        )
        
        if existing:
            return existing
        
        # Calculate opportunity value based on customer value
        customer_value = Decimal("0")
        if review.customer_id:
            from sqlalchemy import func
            customer_value = self.db.scalar(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.organization_id == self.org_id,
                    Transaction.customer_id == review.customer_id,
                    Transaction.status == "paid",
                )
            ) or Decimal("0")
        
        # Create opportunity
        opp = RevenueOpportunity(
            id=f"opp_rev_{int(time.time())}_{hash(str(review.id))[:8]}",
            organization_id=self.org_id,
            customer_id=review.customer_id,
            detection_key=f"review:{review.id}",
            type="negative_review_recovery",
            status="open",
            reason=f"Negative review ({review.rating}/5): {review.text[:100]}...",
            severity="high" if review.rating <= 2 else "medium",
            estimated_value=customer_value * Decimal("0.1"),  # 10% of customer value at risk
            probability=0.8,
            expected_value=customer_value * Decimal("0.08"),  # 8% expected recovery
            confidence=0.9,
            urgency=0.7,
            recoverability=0.6,
            customer_importance=0.8 if customer_value > Decimal("1000") else 0.5,
            priority_score=0.0,  # Will be calculated
            evidence_json=json.dumps([
                {"source": "review", "id": review.id, "rating": review.rating},
                {"source": "analysis", "sentiment": analysis["sentiment"]["overall"]},
            ]),
            recovered_amount=Decimal("0"),
            recommended_action="Personal outreach and service recovery",
            attribution_status="estimated",
        )
        
        # Calculate priority score
        from .services import score_priority
        opp.priority_score = score_priority(
            opp.expected_value,
            opp.urgency,
            opp.recoverability,
            opp.customer_importance
        )
        
        self.db.add(opp)
        
        # Record event
        event = OpportunityEvent(
            organization_id=self.org_id,
            opportunity_id=opp.id,
            event_type="opportunity_created",
            actor="review_system",
            metadata_json=json.dumps({
                "review_id": review.id,
                "rating": review.rating,
                "trigger": "negative_review",
            })
        )
        self.db.add(event)
        
        return opp


class ReviewPipeline:
    """End-to-end review processing pipeline."""
    
    def __init__(self, db: Session, organization_id: str):
        self.db = db
        self.org_id = organization_id
        self.analyzer = ReviewAnalysis(db, organization_id)
    
    def process_review(self, review: Review) -> Dict[str, Any]:
        """
        Process a review through the complete pipeline.
        """
        pipeline_result = {
            "review_id": review.id,
            "processing_start": datetime.now(timezone.utc).isoformat(),
            "steps": {},
            "outcomes": {},
        }
        
        # Step 1: Analysis
        pipeline_result["steps"]["analysis"] = {
            "start": datetime.now(timezone.utc).isoformat(),
        }
        analysis = self.analyzer.analyze_review(review)
        pipeline_result["steps"]["analysis"]["end"] = datetime.now(timezone.utc).isoformat()
        pipeline_result["steps"]["analysis"]["result"] = analysis
        
        # Step 2: Response generation
        pipeline_result["steps"]["response_generation"] = {
            "start": datetime.now(timezone.utc).isoformat(),
        }
        
        response_generated = False
        response_text = None
        
        if analysis["automated_response_possible"]:
            response_text = self.analyzer.generate_automated_response(review, analysis)
            response_generated = True
            
            # Create draft response
            draft_response = ReviewResponse(
                id=f"resp_{int(time.time())}_{hash(str(review.id))[:8]}",
                organization_id=self.org_id,
                review_id=review.id,
                draft_text=response_text,
                status="draft",
            )
            self.db.add(draft_response)
            pipeline_result["outcomes"]["response_created"] = True
            pipeline_result["outcomes"]["response_id"] = draft_response.id
        
        pipeline_result["steps"]["response_generation"]["end"] = datetime.now(timezone.utc).isoformat()
        pipeline_result["steps"]["response_generation"]["response_generated"] = response_generated
        
        # Step 3: Opportunity creation
        pipeline_result["steps"]["opportunity_creation"] = {
            "start": datetime.now(timezone.utc).isoformat(),
        }
        
        opportunity_created = False
        if analysis["action_required"]["opportunity_creation"]:
            opportunity = self.analyzer.create_revenue_opportunity_from_review(review, analysis)
            if opportunity:
                opportunity_created = True
                pipeline_result["outcomes"]["opportunity_created"] = True
                pipeline_result["outcomes"]["opportunity_id"] = opportunity.id
        
        pipeline_result["steps"]["opportunity_creation"]["end"] = datetime.now(timezone.utc).isoformat()
        pipeline_result["steps"]["opportunity_creation"]["opportunity_created"] = opportunity_created
        
        # Step 4: Audit and logging
        pipeline_result["steps"]["audit"] = {
            "start": datetime.now(timezone.utc).isoformat(),
        }
        
        audit = AuditLog(
            organization_id=self.org_id,
            action="review_processed",
            resource_type="review",
            resource_id=review.id,
            metadata_json=json.dumps({
                "pipeline_result": pipeline_result,
                "analysis": analysis,
                "rating": review.rating,
                "source": review.source,
            })
        )
        self.db.add(audit)
        
        pipeline_result["steps"]["audit"]["end"] = datetime.now(timezone.utc).isoformat()
        pipeline_result["processing_end"] = datetime.now(timezone.utc).isoformat()
        
        return pipeline_result
    
    def get_review_analytics(self, days: int = 30) -> Dict[str, Any]:
        """
        Get analytics for reviews in the specified time period.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        
        # Query reviews
        reviews = self.db.scalars(
            select(Review).where(
                Review.organization_id == self.org_id,
                Review.created_at >= cutoff
            )
        ).all()
        
        if not reviews:
            return {
                "total_reviews": 0,
                "average_rating": 0,
                "sentiment_distribution": {},
                "topic_distribution": {},
                "response_rate": 0,
                "trends": {},
            }
        
        # Calculate statistics
        total_reviews = len(reviews)
        average_rating = sum(r.rating for r in reviews) / total_reviews
        
        # Sentiment distribution
        sentiment_counts = {"positive": 0, "neutral": 0, "negative": 0}
        for review in reviews:
            analysis = self.analyzer.analyze_review(review)
            sentiment = analysis["sentiment"]["overall"]
            sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1
        
        # Topic distribution
        topic_counts = {}
        for review in reviews:
            analysis = self.analyzer.analyze_review(review)
            for topic in analysis["topics"]:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
        
        # Response rate
        responded_reviews = self.db.scalar(
            select(func.count(Review.id)).where(
                Review.organization_id == self.org_id,
                Review.created_at >= cutoff,
                Review.id.in_(
                    select(ReviewResponse.review_id).where(
                        ReviewResponse.organization_id == self.org_id,
                        ReviewResponse.status.in_(["sent", "approved"])
                    )
                )
            )
        ) or 0
        
        response_rate = (responded_reviews / total_reviews * 100) if total_reviews > 0 else 0
        
        # Trends (rating over time)
        # Group by day
        daily_ratings = {}
        for review in reviews:
            day = review.created_at.date().isoformat()
            if day not in daily_ratings:
                daily_ratings[day] = []
            daily_ratings[day].append(review.rating)
        
        daily_averages = {
            day: sum(ratings) / len(ratings)
            for day, ratings in daily_ratings.items()
        }
        
        return {
            "total_reviews": total_reviews,
            "average_rating": round(average_rating, 2),
            "sentiment_distribution": sentiment_counts,
            "topic_distribution": dict(sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
            "response_rate": round(response_rate, 1),
            "trends": {
                "daily_average_rating": daily_averages,
                "review_volume_by_day": {day: len(ratings) for day, ratings in daily_ratings.items()},
            },
            "time_period_days": days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }