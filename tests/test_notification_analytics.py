"""First-party copy isolation, retry, authorization and report contracts."""
import json
from uuid import uuid4
from pydantic import ValidationError
from fastapi import HTTPException
from tests.test_universal_analytics_contract import UniversalAnalyticsContractTests
from core.models.experiment import ExperimentDiagnosticEvent
from core.routers.experiment_diagnostics import DiagnosticEvent, get_notification_engagement_report, get_diagnostic_report

class NotificationAnalyticsTests(UniversalAnalyticsContractTests):
    def notification(self, installation=None):
        event = self.event('push_opened', installation or uuid4(), schema=24, properties={
            'source':'remote','action':'default_tap','notification_id':str(uuid4()),
            'notification_copy_id':'a'*64,'notification_type':'gate','app_language':'ar',
            'notification_open_delay':'1m_to_1h'})
        return DiagnosticEvent.model_validate({**event.model_dump(), 'notification_content':{
            'title':'بوابتك الجديدة', 'subtitle':'Gate update', 'body':'Go to gate B12. Email person@example.com'}})

    def test_copy_encrypted_retry_and_reports(self):
        event = self.notification()
        self.send(event)
        self.send(event)
        row = self.session.get(ExperimentDiagnosticEvent, str(event.event_id))
        self.assertNotIn('Go to gate', row.properties_json)
        self.assertNotIn('person@example.com', row.properties_json)
        report = get_notification_engagement_report(analytics_environment='testflight',limit=100,session=self.session)
        self.assertEqual(report['opens'],1)
        self.assertEqual(report['groups'][0]['unique_installations'],1)
        self.assertEqual(report['groups'][0]['content']['body'],'Go to gate B12. Email [redacted]')
        self.assertIsNone(report['open_rate'])
        generic = get_diagnostic_report(analytics_environment='testflight',limit=100,session=self.session)
        self.assertNotIn('_notification_copy_ciphertext',json.dumps(generic))
        self.assertIn('notification_content',generic['events'][0])

    def test_conflicting_copy_does_not_overwrite_original(self):
        event=self.notification(); self.send(event)
        changed=event.model_copy(deep=True); changed.notification_content.body='Changed title content'
        with self.assertRaises(HTTPException) as error:self.send(changed)
        self.assertEqual(error.exception.status_code,409)

    def test_bounds_event_and_environment_isolation(self):
        event=self.notification()
        for changes in ({'event_name':'app_launched'}, {'notification_content':{'title':'x'*513,'body':'a'}},
                        {'notification_content':{'title':'a','body':'x'*4097}},
                        {'notification_content':{'title':'a','body':'b','raw_query':'private'}}):
            with self.assertRaises(ValidationError):DiagnosticEvent.model_validate({**event.model_dump(),**changes})
        self.send(event)
        report=get_notification_engagement_report(analytics_environment='production',limit=100,session=self.session)
        self.assertEqual(report['opens'],0)

    def test_report_marks_truncation_and_unique_installations(self):
        first=self.notification(); second=self.notification(first.installation_id)
        self.send(first,second)
        report=get_notification_engagement_report(analytics_environment='testflight',limit=100,session=self.session)
        self.assertEqual(report['groups'][0]['opens'],2)
        self.assertEqual(report['groups'][0]['unique_installations'],1)
        self.assertTrue(get_notification_engagement_report(analytics_environment='testflight',limit=1,session=self.session)['truncated'])

    def test_http_authentication_and_wire_delivery(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.routers.experiment_diagnostics import router
        from core.models import get_session
        from core.dependency import get_current_user
        from core.config import settings
        app=FastAPI(); app.include_router(router)
        app.dependency_overrides[get_session]=lambda:self.session
        with TestClient(app) as client:
            self.assertIn(client.get('/notifications/engagement/report').status_code,(401,403))
            self.assertEqual(client.get('/notifications/engagement/report',headers={'Authorization':'Bearer wrong'}).status_code,401)
            event=self.notification().model_dump(mode='json')
            self.assertIn(client.post('/experiments/events',json={'events':[event]}).status_code,(401,403))
            app.dependency_overrides[get_current_user]=lambda:self.user
            for _ in range(2):self.assertEqual(client.post('/experiments/events',json={'events':[event]}).status_code,200)
            response=client.get('/notifications/engagement/report?analytics_environment=testflight',headers={'Authorization':'Bearer '+settings.LAMBDA_FUNCTION_AUTH_TOKEN})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['opens'],1)

    def test_account_deletion_cleanup_is_scoped_and_transactional(self):
        from core.services.notification_analytics import remove_notification_diagnostics
        notification=self.notification(); self.send(notification)
        other=self.event('app_launched',uuid4()); self.send(other)
        remove_notification_diagnostics(self.session,self.user.id)
        self.session.rollback()
        self.assertIsNotNone(self.session.get(ExperimentDiagnosticEvent,str(notification.event_id)))
        remove_notification_diagnostics(self.session,self.user.id)
        self.session.commit()
        self.assertIsNone(self.session.get(ExperimentDiagnosticEvent,str(notification.event_id)))
        self.assertIsNotNone(self.session.get(ExperimentDiagnosticEvent,str(other.event_id)))
        from pathlib import Path
        source=(Path(__file__).resolve().parents[1]/'core/routers/users.py').read_text()
        self.assertIn('remove_notification_diagnostics(session, user.id)',source)
