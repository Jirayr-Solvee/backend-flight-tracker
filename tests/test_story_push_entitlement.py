import sqlite3
import unittest

from core.services.story_push_entitlement import has_subscription_access
from tests.story_push_fixtures import create_subscription_tables, add_subscription, add_lifecycle


class StoryPushEntitlementTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        create_subscription_tables(self.db)
        self.now=1792873079543

    def tearDown(self):self.db.close()

    def allowed(self,user='owner'):
        return has_subscription_access(self.db,user,self.now)

    def test_verified_trial_or_paid_period_needs_no_api_lease(self):
        add_subscription(self.db,self.now)
        self.assertTrue(self.allowed())
        # No user table/cache is read, and the function does not write anything.
        before=self.db.total_changes
        self.db.commit();self.db.execute('PRAGMA query_only=ON')
        self.assertTrue(self.allowed());self.assertEqual(self.db.total_changes,before)

    def test_no_link_or_wrong_owner_cannot_receive(self):
        add_subscription(self.db,self.now)
        self.assertFalse(self.allowed('other'))
        self.db.execute('DELETE FROM usersubscriptionlink')
        self.assertFalse(self.allowed())

    def test_sandbox_unknown_environment_and_future_purchase_cannot_grant(self):
        for env,purchase in [('Sandbox',self.now-1000),('Xcode',self.now-1000),
                             (None,self.now-1000),('Production',self.now+1000)]:
            with self.subTest(environment=env,purchase=purchase):
                self.db.execute('DELETE FROM "transaction"')
                add_subscription(self.db,self.now,environment=env,purchase_date=purchase)
                self.assertFalse(self.allowed())

    def test_expiry_boundary_missing_expiry_and_upgrade_fail_closed(self):
        for changes in ({'expires_date':self.now},{'expires_date':self.now-1},
                        {'expires_date':None},{'revoked_date':0},{'revoked_date':self.now-1},
                        {'is_upgraded':1}):
            with self.subTest(changes=changes):
                self.db.execute('DELETE FROM "transaction"');add_subscription(self.db,self.now,**changes)
                self.assertFalse(self.allowed())

    def test_cancellation_keeps_access_to_verified_expiry(self):
        add_subscription(self.db,self.now);add_lifecycle(self.db,self.now)
        self.assertTrue(self.allowed())

    def test_future_renewal_receipt_does_not_replace_current_period(self):
        add_subscription(self.db,self.now)
        add_subscription(self.db,self.now,tx='future',purchase_date=self.now+1000,
                         expires_date=self.now+86400000,revoked_date=self.now)
        self.assertTrue(self.allowed())
        self.assertFalse(has_subscription_access(self.db,'owner',self.now+1000))

    def test_later_revoked_transaction_does_not_fall_back_to_older_period(self):
        add_subscription(self.db,self.now)
        add_subscription(self.db,self.now,tx='new',purchase_date=self.now-1000,revoked_date=self.now)
        self.assertFalse(self.allowed())

    def test_valid_separate_subscription_survives_another_revoked_chain(self):
        add_subscription(self.db,self.now,revoked_date=self.now)
        add_subscription(self.db,self.now,chain='second',tx='second')
        self.assertTrue(self.allowed())

    def test_grace_survives_transaction_resigning_and_informational_notice(self):
        add_subscription(self.db,self.now,expires_date=self.now-1000,signed_date=self.now)
        add_lifecycle(self.db,self.now,event_kind='grace_period_started',subscription_status=4,
                      grace_period_expires_date_ms=self.now+86400000)
        add_lifecycle(self.db,self.now,identity='information',event_kind='other',
                      renewal_signed_date_ms=None,notification_signed_date_ms=self.now,
                      subscription_status=None)
        self.assertTrue(self.allowed())

    def test_authoritative_end_overrides_future_jws_expiry_or_grace(self):
        add_subscription(self.db,self.now)
        for kind in ('expiration','grace_period_expired','refund','revocation','billing_failure'):
            with self.subTest(kind=kind):
                self.db.execute('DELETE FROM appstoresubscriptionlifecycleevent')
                add_lifecycle(self.db,self.now,event_kind=kind,renewal_signed_date_ms=None,
                              grace_period_expires_date_ms=self.now+86400000)
                self.assertFalse(self.allowed())

    def test_expiration_after_grace_ends_access(self):
        add_subscription(self.db,self.now,expires_date=self.now-1000)
        add_lifecycle(self.db,self.now,event_kind='grace_period_started',subscription_status=4,
                      grace_period_expires_date_ms=self.now+86400000)
        add_lifecycle(self.db,self.now,identity='expiry',event_kind='expiration',
                      notification_signed_date_ms=self.now,renewal_signed_date_ms=None)
        self.assertFalse(self.allowed())

    def test_expired_grace_and_billing_retry_are_not_paid_access(self):
        add_subscription(self.db,self.now,expires_date=self.now-1000)
        add_lifecycle(self.db,self.now,event_kind='grace_period_started',subscription_status=4,
                      grace_period_expires_date_ms=self.now)
        self.assertFalse(self.allowed())
        self.db.execute('UPDATE appstoresubscriptionlifecycleevent SET subscription_status=3,grace_period_expires_date_ms=?',(self.now+1000,))
        self.assertFalse(self.allowed())

    def test_old_refund_and_other_environment_do_not_hide_current_period(self):
        add_subscription(self.db,self.now)
        add_lifecycle(self.db,self.now,tx='old-transaction',event_kind='refund')
        add_lifecycle(self.db,self.now,identity='sandbox',event_kind='refund',purchase_environment='Sandbox')
        self.assertTrue(self.allowed())

    def test_null_transaction_notice_requires_date_after_current_purchase(self):
        add_subscription(self.db,self.now)
        add_lifecycle(self.db,self.now,tx=None,event_kind='expiration',notification_signed_date_ms=self.now-2*86400000)
        self.assertTrue(self.allowed())
        self.db.execute('UPDATE appstoresubscriptionlifecycleevent SET notification_signed_date_ms=?',(self.now,))
        self.assertFalse(self.allowed())

    def test_equal_date_conflict_fails_closed_independent_of_id_order(self):
        add_subscription(self.db,self.now)
        add_lifecycle(self.db,self.now,identity='z-active')
        add_lifecycle(self.db,self.now,identity='a-revoked',event_kind='revocation')
        self.assertFalse(self.allowed())

    def test_expired_revoked_retry_or_unknown_status_fails_closed(self):
        add_subscription(self.db,self.now);add_lifecycle(self.db,self.now)
        for status in (2,3,5,99):
            self.db.execute('UPDATE appstoresubscriptionlifecycleevent SET subscription_status=?',(status,))
            self.assertFalse(self.allowed())

    def test_unknown_status_wins_equal_date_conflict(self):
        add_subscription(self.db,self.now)
        add_lifecycle(self.db,self.now,identity='z-active')
        add_lifecycle(self.db,self.now,identity='a-unknown',subscription_status=99)
        self.assertFalse(self.allowed())

    def test_missing_schema_raises_instead_of_cache_fallback(self):
        self.db.execute('DROP TABLE "transaction"')
        with self.assertRaises(sqlite3.OperationalError):self.allowed()


if __name__=='__main__':unittest.main()
