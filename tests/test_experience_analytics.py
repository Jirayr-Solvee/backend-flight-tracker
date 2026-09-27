"""Schema-23 interaction projection; synthetic in-memory storage only."""
import unittest
from uuid import uuid4
from pydantic import ValidationError
from tests.test_universal_analytics_contract import UniversalAnalyticsContractTests
from core.routers.experiment_diagnostics import DiagnosticProperties

class ExperienceAnalyticsTests(UniversalAnalyticsContractTests):
    def test_airport_and_message_interactions_persist_and_deduplicate(self):
        event=self.event('experience_action',uuid4(),schema=23,properties={
            'screen':'subscription','action':'viewed','step_key':'checkout','source':'onboarding',
            'paywall_surface':'airport','experience_presentation_id':str(uuid4()),
            'message_id':'abcdef0123456789','message_category':'Weather','message_count':20,
            'app_language':'ar'},presentation=uuid4())
        first=self.send(event)
        second=self.send(event)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        from sqlmodel import select
        from core.models.experiment import ExperimentDiagnosticEvent
        rows=self.session.exec(select(ExperimentDiagnosticEvent).where(ExperimentDiagnosticEvent.id==str(event.event_id))).all()
        self.assertEqual(len(rows),1)

    def test_rejects_raw_message_bodies_and_invalid_counts(self):
        for properties in ({'message_body':'private transmission'}, {'message_count':-1},
                           {'message_count':True}, {'message_category':'arbitrary'},
                           {'experience_presentation_id':'not-a-uuid'}):
            with self.assertRaises(ValidationError):DiagnosticProperties(**properties)
