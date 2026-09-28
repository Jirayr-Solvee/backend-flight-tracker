"""Authenticated registration remains backwards compatible and fail-closed."""
import unittest
from tests.test_cockpit_api import _environment
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine
from core.models.user import User
from core.models.device import Device
from core.models.story_push import StoryPushDevice
from core.routers.users import RefreshApnToken, refresh_apn_token


class StoryRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session=Session(self.engine)
        self.user=User(id='owner')
        self.session.add(self.user)
        self.session.add(Device(id='phone',user_id='owner'))
        self.session.commit()

    def tearDown(self):
        self.session.close(); self.engine.dispose()

    def test_updated_client_then_old_client_clears_capability(self):
        refresh_apn_token(RefreshApnToken(device_id='phone',apn_token='test-token',
            app_version='3.9.3',build_number=142,story_push_capability=1,story_push_enabled=True,
            app_language='fr',time_zone='Europe/Paris',analytics_environment='production'),self.user,self.session)
        record=self.session.get(StoryPushDevice,'phone')
        self.assertEqual(record.build_number,142)
        self.assertTrue(record.enabled)
        refresh_apn_token(RefreshApnToken(device_id='phone',apn_token='test-token'),self.user,self.session)
        self.session.expire_all()
        record=self.session.get(StoryPushDevice,'phone')
        self.assertEqual(record.capability,0)
        self.assertFalse(record.enabled)

    def test_invalid_metadata_rejected(self):
        for extra in ({'time_zone':'../../private'},{'build_number':'142'},
                      {'story_push_capability':2},{'analytics_environment':'anything'}):
            with self.assertRaises(ValueError):
                RefreshApnToken(device_id='phone',apn_token='test-token',**extra)


if __name__ == '__main__': unittest.main()
