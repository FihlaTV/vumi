import json
from urllib import urlencode

from twisted.internet.defer import inlineCallbacks
from twisted.web import http

from vumi.transports.tests.utils import TransportTestCase
from vumi.transports.airtel import AirtelUSSDTransport
from vumi.message import TransportUserMessage
from vumi.utils import http_request_full


class TestAirtelUSSDTransportTestCase(TransportTestCase):

    transport_class = AirtelUSSDTransport
    airtel_username = None
    airtel_password = None
    session_id = 'session-id'

    @inlineCallbacks
    def setUp(self):
        yield super(TestAirtelUSSDTransportTestCase, self).setUp()
        self.config = {
            'web_port': 0,
            'web_path': '/api/v1/airtel/ussd/',
            'airtel_username': self.airtel_username,
            'airtel_password': self.airtel_password,
            'validation_mode': 'permissive',
        }
        self.transport = yield self.get_transport(self.config)
        self.session_manager = self.transport.session_manager
        self.transport_url = self.transport.get_transport_url(
            self.config['web_path'])
        yield self.session_manager.redis._purge_all()  # just in case

    @inlineCallbacks
    def tearDown(self):
        yield super(TestAirtelUSSDTransportTestCase, self).tearDown()
        yield self.session_manager.stop()

    def mk_full_request(self, **params):
        return http_request_full('%s?%s' % (self.transport_url,
            urlencode(params)), data='', method='GET')

    def mk_request(self, **params):
        defaults = {
            'MSISDN': '27761234567',
        }
        if all([self.airtel_username, self.airtel_password]):
            defaults.update({
                'userid': self.airtel_username,
                'password': self.airtel_password,
            })

        defaults.update(params)
        return self.mk_full_request(**defaults)

    def mk_ussd_request(self, content, **kwargs):
        defaults = {
            'MSC': 'msc',
            'input': content,
            'SessionID': self.session_id,
        }
        defaults.update(kwargs)
        return self.mk_request(**defaults)

    def mk_cleanup_request(self, **kwargs):
        defaults = {
            'clean': 'clean-session',
            'error': 522,
            'SessionID': self.session_id,
        }
        defaults.update(kwargs)
        return self.mk_request(**defaults)

    @inlineCallbacks
    def test_inbound_begin(self):
        # Second connect is the actual start of the session
        deferred = self.mk_ussd_request('121')
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['content'], '')
        self.assertEqual(msg['to_addr'], '*121#')
        self.assertEqual(msg['from_addr'], '27761234567'),
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_NEW)
        self.assertEqual(msg['transport_metadata'], {
            'airtel': {
                'MSC': 'msc',
            },
        })

        reply = TransportUserMessage(**msg.payload).reply("ussd message")
        self.dispatch(reply)
        response = yield deferred
        self.assertEqual(response.delivered_body, 'ussd message')
        self.assertEqual(response.headers.getRawHeaders('Freeflow'), ['FC'])
        self.assertEqual(response.headers.getRawHeaders('charge'), ['N'])
        self.assertEqual(response.headers.getRawHeaders('amount'), ['0'])

    @inlineCallbacks
    def test_inbound_resume_and_reply_with_end(self):
        # first pre-populate the redis datastore to simulate prior BEG message
        yield self.session_manager.create_session(self.session_id,
                to_addr='*167*7#', from_addr='27761234567',
                session_event=TransportUserMessage.SESSION_RESUME)

        # Safaricom gives us the history of the full session in the USSD_PARAMS
        # The last submitted bit of content is the last value delimited by '*'
        deferred = self.mk_ussd_request('c')

        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['content'], 'c')
        self.assertEqual(msg['to_addr'], '*167*7#')
        self.assertEqual(msg['from_addr'], '27761234567')
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_RESUME)

        reply = TransportUserMessage(**msg.payload).reply("hello world",
            continue_session=False)
        self.dispatch(reply)
        response = yield deferred
        self.assertEqual(response.delivered_body, 'hello world')
        self.assertEqual(response.headers.getRawHeaders('Freeflow'), ['FB'])

    @inlineCallbacks
    def test_inbound_resume_with_failed_to_addr_lookup(self):
        deferred = self.mk_request(MSISDN='123456',
                                   input='7*a', SessionID='foo')
        response = yield deferred
        self.assertEqual(json.loads(response.delivered_body), {
            'missing_parameter': ['MSC'],
        })

    @inlineCallbacks
    def test_to_addr_handling(self):
        d1 = self.mk_ussd_request('167*7*1')
        [msg1] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg1['to_addr'], '*167*7*1#')
        self.assertEqual(msg1['content'], '')
        self.assertEqual(msg1['session_event'],
            TransportUserMessage.SESSION_NEW)
        reply = TransportUserMessage(**msg1.payload).reply("hello world",
            continue_session=True)
        yield self.dispatch(reply)
        yield d1

        # follow up with the user submitting 'a'
        d2 = self.mk_ussd_request('a')
        [msg1, msg2] = yield self.wait_for_dispatched_messages(2)
        self.assertEqual(msg2['to_addr'], '*167*7*1#')
        self.assertEqual(msg2['content'], 'a')
        self.assertEqual(msg2['session_event'],
            TransportUserMessage.SESSION_RESUME)
        reply = TransportUserMessage(**msg2.payload).reply("hello world",
            continue_session=False)
        self.dispatch(reply)
        yield d2

    @inlineCallbacks
    def test_hitting_url_twice_without_content(self):
        d1 = self.mk_ussd_request('167*7*3')
        [msg1] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg1['to_addr'], '*167*7*3#')
        self.assertEqual(msg1['content'], '')
        self.assertEqual(msg1['session_event'],
            TransportUserMessage.SESSION_NEW)
        reply = TransportUserMessage(**msg1.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield d1

        # make the exact same request again
        d2 = self.mk_ussd_request('')
        [msg1, msg2] = yield self.wait_for_dispatched_messages(2)
        self.assertEqual(msg2['to_addr'], '*167*7*3#')
        self.assertEqual(msg2['content'], '')
        self.assertEqual(msg2['session_event'],
            TransportUserMessage.SESSION_RESUME)
        reply = TransportUserMessage(**msg2.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield d2

    @inlineCallbacks
    def test_submitting_asterisks_as_values(self):
        yield self.session_manager.create_session(self.session_id,
                to_addr='*167*7#', from_addr='27761234567')
        # we're submitting a bunch of *s
        deferred = self.mk_ussd_request('****')

        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['content'], '****')

        reply = TransportUserMessage(**msg.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield deferred

    @inlineCallbacks
    def test_submitting_asterisks_as_values_after_asterisks(self):
        yield self.session_manager.create_session(self.session_id,
                to_addr='*167*7#', from_addr='27761234567')
        # we're submitting a bunch of *s
        deferred = self.mk_ussd_request('**')

        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['content'], '**')

        reply = TransportUserMessage(**msg.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield deferred

    @inlineCallbacks
    def test_submitting_with_base_code_empty_ussd_params(self):
        d1 = self.mk_ussd_request('167')
        [msg1] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg1['to_addr'], '*167#')
        self.assertEqual(msg1['content'], '')
        self.assertEqual(msg1['session_event'],
            TransportUserMessage.SESSION_NEW)
        reply = TransportUserMessage(**msg1.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield d1

        # ask for first menu
        d2 = self.mk_ussd_request('1')
        [msg1, msg2] = yield self.wait_for_dispatched_messages(2)
        self.assertEqual(msg2['to_addr'], '*167#')
        self.assertEqual(msg2['content'], '1')
        self.assertEqual(msg2['session_event'],
            TransportUserMessage.SESSION_RESUME)
        reply = TransportUserMessage(**msg2.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield d2

        # ask for second menu
        d3 = self.mk_ussd_request('1')
        [msg1, msg2, msg3] = yield self.wait_for_dispatched_messages(3)
        self.assertEqual(msg3['to_addr'], '*167#')
        self.assertEqual(msg3['content'], '1')
        self.assertEqual(msg3['session_event'],
            TransportUserMessage.SESSION_RESUME)
        reply = TransportUserMessage(**msg3.payload).reply('Hello',
            continue_session=True)
        self.dispatch(reply)
        yield d3

    @inlineCallbacks
    def test_cleanup_unknown_session(self):
        response = yield self.mk_cleanup_request(msisdn='foo')
        self.assertEqual(response.code, http.OK)
        self.assertEqual(response.delivered_body, 'Unknown Session')

    @inlineCallbacks
    def test_cleanup_session(self):
        yield self.session_manager.create_session(self.session_id,
            to_addr='*167*7#', from_addr='27761234567')
        response = yield self.mk_cleanup_request(msisdn='27761234567')
        self.assertEqual(response.code, http.OK)
        self.assertEqual(response.delivered_body, '')
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['session_event'],
            TransportUserMessage.SESSION_CLOSE)
        self.assertEqual(msg['to_addr'], '*167*7#')
        self.assertEqual(msg['from_addr'], '27761234567')
        self.assertEqual(msg['transport_metadata'], {
            'airtel': {
                'error': '522',
                'clean': 'clean-session',
            }
            })

    @inlineCallbacks
    def test_cleanup_session_missing_params(self):
        response = yield self.mk_request(clean='clean-session')
        self.assertEqual(response.code, http.BAD_REQUEST)
        json_response = json.loads(response.delivered_body)
        self.assertEqual(set(json_response['missing_parameter']),
                         set(['msisdn', 'SessionID', 'error']))

    @inlineCallbacks
    def test_cleanup_as_seen_in_production(self):
        """what's a technical spec between friends?"""
        yield self.session_manager.create_session('13697502734175597',
            to_addr='*167*7#', from_addr='254XXXXXXXXX')
        query_string = ("msisdn=254XXXXXXXXX&clean=cleann&error=523"
                        "&SessionID=13697502734175597&MSC=254XXXXXXXXX"
                        "&=&=en&=9031510005344&=&=&=postpaid"
                        "&=20130528171235405&=200220130528171113956582")
        response = yield http_request_full(
            '%s?%s' % (self.transport_url, query_string),
            data='', method='GET')
        self.assertEqual(response.code, http.OK)
        self.assertEqual(response.delivered_body, '')
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_CLOSE)
        self.assertEqual(msg['to_addr'], '*167*7#')
        self.assertEqual(msg['from_addr'], '254XXXXXXXXX')
        self.assertEqual(msg['transport_metadata'], {
            'airtel': {
                'clean': 'cleann',
                'error': '523',
            }
        })


class TestAirtelUSSDTransportTestCaseWithAuth(TestAirtelUSSDTransportTestCase):

    transport_class = AirtelUSSDTransport
    airtel_username = 'userid'
    airtel_password = 'password'

    @inlineCallbacks
    def test_cleanup_session_invalid_auth(self):
        response = yield self.mk_cleanup_request(userid='foo', password='bar')
        self.assertEqual(response.code, http.FORBIDDEN)
        self.assertEqual(response.delivered_body, 'Forbidden')

    @inlineCallbacks
    def test_cleanup_as_seen_in_production(self):
        """what's a technical spec between friends?"""
        yield self.session_manager.create_session('13697502734175597',
            to_addr='*167*7#', from_addr='254XXXXXXXXX')
        query_string = ("msisdn=254XXXXXXXXX&clean=cleann&error=523"
                        "&SessionID=13697502734175597&MSC=254XXXXXXXXX"
                        "&=&=en&=9031510005344&=&=&=postpaid"
                        "&=20130528171235405&=200220130528171113956582"
                        "&userid=%s&password=%s" % (self.airtel_username,
                                                    self.airtel_password))
        response = yield http_request_full(
            '%s?%s' % (self.transport_url, query_string),
            data='', method='GET')
        self.assertEqual(response.code, http.OK)
        self.assertEqual(response.delivered_body, '')
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_CLOSE)
        self.assertEqual(msg['to_addr'], '*167*7#')
        self.assertEqual(msg['from_addr'], '254XXXXXXXXX')
        self.assertEqual(msg['transport_metadata'], {
            'airtel': {
                'clean': 'cleann',
                'error': '523',
            }
        })

class LoadBalancedAirtelUSSDTransportTestCase(TransportTestCase):

    transport_class = AirtelUSSDTransport

    @inlineCallbacks
    def setUp(self):
        yield super(LoadBalancedAirtelUSSDTransportTestCase, self).setUp()
        self.default_config = {
            'web_port': 0,
            'web_path': '/api/v1/airtel/ussd/',
            'validation_mode': 'permissive',
            'session_key_prefix': 'foo',
        }

        config1 = self.default_config.copy()
        config1['transport_name'] = 'transport_1'

        config2 = self.default_config.copy()
        config2['transport_name'] = 'transport_2'

        self.transport1 = yield self.get_transport(config1)
        self.transport2 = yield self.get_transport(config2)

    @inlineCallbacks
    def test_sessions_in_sync(self):
        session1 = yield self.transport1.session_manager.load_session('user1')
        session1['foo'] = 'bar'
        yield self.transport1.session_manager.save_session('user1', session1)

        session2 = yield self.transport2.session_manager.load_session('user1')
        self.assertEqual(session2['foo'], 'bar')
