# encoding: utf-8
from twisted.web import http
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.trial import unittest
from twisted.python import log, failure
from twisted.internet import defer, reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.test.test_web import DummyRequest
from twisted.web import http


from vumi.service import Worker
from vumi.tests.utils import get_stubbed_worker, TestQueue, StubbedAMQClient
from vumi.message import Message

from StringIO import StringIO
from urllib import urlencode
from uuid import uuid1
from datetime import datetime
from .transport import (ReceiveSMSResource, DeliveryReceiptResource, 
                        Vas2NetsTransport, validate_characters, 
                        Vas2NetsEncodingError, Vas2NetsTransportError,
                        normalize_outbound_msisdn)
import string

def create_request(dictionary={}, path='/', method='POST'):
    """Creates a dummy Vas2Nets request for testing our 
    resources with"""
    request = DummyRequest(path)
    request.method = method
    args = {
        'messageid': [str(uuid1())],
        'time': [datetime.utcnow().strftime('%Y.%m.%d %H:%M:%S')],
        'sender': ['0041791234567'],
        'destination': ['9292'],
        'provider': ['provider'],
        'keyword': [''],
        'header': [''],
        'text': [''],
        'keyword': [''],
    }
    args.update(dictionary)
    request.args = args
    return request

class TestResource(Resource):
    isLeaf = True
    def __init__(self, message_id, message):
        self.message_id = message_id
        self.message = message
    
    def render_POST(self, request):
        log.msg(request.content.read())
        request.setResponseCode(http.OK)
        required_fields = [
            'username', 'password', 'call-number', 'origin', 'text',
            'messageid', 'provider', 'tariff', 'owner', 'service',
            'subservice'
        ]
        log.msg('request.args', request.args)
        for key in required_fields:
            log.msg('checking for %s' % key)
            assert key in request.args
        
        if self.message_id:
            request.setHeader('X-Nth-Smsid', self.message_id)
        return self.message

class Vas2NetsTestWorker(Worker):
    
    def __init__(self, path, port, message_id, message, queue):
        Worker.__init__(self, StubbedAMQClient(queue))
        self.path = path
        self.port = port
        self.message_id = message_id
        self.message = message
    
    @inlineCallbacks
    def startWorker(self):
        self.receipt_resource = yield self.start_web_resources(
            [
                (TestResource(self.message_id, self.message), self.path),
            ],
            self.port
        )
    
    def stopWorker(self):
        self.receipt_resource.stopListening()

class Vas2NetsTransportTestCase(unittest.TestCase):
    
    @inlineCallbacks
    def setUp(self):
        self.config = {
            'transport_name': 'vas2nets'
        }
        self.worker = get_stubbed_worker(Vas2NetsTransport, {})
        self.publisher = yield self.worker.publish_to('some.routing.key')
        self.today = datetime.utcnow().date()
    
    def tearDown(self):
        pass

    @inlineCallbacks
    def test_receive_sms(self):
        resource = ReceiveSMSResource(self.config, self.publisher)
        request = create_request({
            'messageid': ['1'],
            'time': [self.today.strftime('%Y.%m.%d %H:%M:%S')],
            'text': ['hello world']
        })
        d = request.notifyFinish()
        response = resource.render(request)
        self.assertEquals(response, NOT_DONE_YET)
        yield d
        self.assertEquals('', ''.join(request.written))
        self.assertEquals(request.outgoingHeaders['content-type'], 'text/plain')
        self.assertEquals(request.responseCode, http.OK)
        msg = Message(**{
            'transport_message_id': '1',
            'transport_timestamp': self.today.strftime('%Y-%m-%dT%H:%M:%S'),
            'transport_network_id': 'provider',
            'transport_keyword': '',
            'to_msisdn': '9292',
            'from_msisdn': '+41791234567',
            'message': 'hello world'
        })
        
        channel = self.worker._amqp_client.channels[0]
        kwargs = channel.publish_log[0]
        content = kwargs['content']
        routing_key = kwargs['routing_key']
        self.assertEquals(Message.from_json(content.body), msg)
        self.assertEquals(routing_key, 'sms.inbound.vas2nets.9292')
    
    def test_delivery_receipt(self):
        request = create_request({
            'smsid': ['1'],
            'messageid': ['internal id'],
            'sender': ['+41791234567'],
            'time': [self.today.strftime('%Y.%m.%d %H:%M:%S')],
            'status': ['2'],
            'text': ['Message delivered to MSISDN.']
        })
        
        resource = DeliveryReceiptResource(self.config, self.publisher)
        response = resource.render(request)
        self.assertEquals(response, '')
        self.assertEquals(request.outgoingHeaders['content-type'], 'text/plain')
        self.assertEquals(request.responseCode, http.OK)
        msg = Message(**{
            'transport_message_id': '1',
            'transport_status': '2',
            'transport_network_id': 'provider',
            'to_msisdn': '+41791234567',
            'id': 'internal id',
            'transport_timestamp': self.today.strftime('%Y-%m-%dT%H:%M:%S'),
            'transport_status_message': 'Message delivered to MSISDN.'
        })
        channel = self.worker._amqp_client.channels[0]
        kwargs = channel.publish_log[0]
        content = kwargs['content']
        routing_key = kwargs['routing_key']
        self.assertEquals(Message.from_json(content.body), msg)
        self.assertEquals(routing_key, 'sms.receipt.vas2nets')
    
    def test_validate_characters(self):
        self.assertRaises(Vas2NetsEncodingError, validate_characters, 
                            u"ïøéå¬∆˚")
        self.assertTrue(validate_characters(string.ascii_lowercase))
        self.assertTrue(validate_characters(string.ascii_uppercase))
        self.assertTrue(validate_characters('0123456789'))
        self.assertTrue(validate_characters(u'äöü ÄÖÜ àùò ìèé §Ññ £$@'))
        self.assertTrue(validate_characters(u'/?!#%&()*+,-:;<=>.'))
        self.assertTrue(validate_characters(u'testing\ncarriage\rreturns'))
        self.assertTrue(validate_characters(u'testing "quotes"'))
        self.assertTrue(validate_characters(u"testing 'quotes'"))
    
    @inlineCallbacks
    def test_send_sms_success(self):
        """no clue yet how I'm going to test this."""
        path = '/api/v1/sms/vas2nets/receive/'
        port = 9999
        mocked_message_id = str(uuid1())
        mocked_message = "Result_code: 00, Message OK"
        
        # open an HTTP resource that mocks the Vas2Nets response for the
        # duration of this test
        stubbed_worker = Vas2NetsTestWorker(path, port, mocked_message_id,
                                            mocked_message, TestQueue([]))
        yield stubbed_worker.startWorker()
        
        transport = get_stubbed_worker(Vas2NetsTransport, {
            'transport_name': 'vas2nets',
            'url': 'http://localhost:%s%s' % (port, path),
            'username': 'username',
            'password': 'password',
            'owner': 'owner',
            'service': 'service',
            'subservice': 'subservice',
            'web_receive_path': '/receive',
            'web_receipt_path': '/receipt',
            'web_port': 9998
        })
        yield transport.startWorker()
        
        resp = yield transport.handle_outbound_message(Message(**{
            'to_msisdn': '+27761234567',
            'from_msisdn': '9292',
            'id': '1',
            'reply_to': '',
            'transport_network_id': 'network-id',
            'message': 'hello world'
        }))
        
        channel = transport._amqp_client.channels[0]
        kwargs = channel.publish_log[0]
        content = kwargs['content']
        routing_key = kwargs['routing_key']
        
        self.assertEquals(Message.from_json(content.body), Message(**{
            'id': '1',
            'transport_message_id': mocked_message_id
        }))
        self.assertEquals(routing_key, 'sms.ack.vas2nets')
        
        transport.stopWorker()
        stubbed_worker.stopWorker()
        
    @inlineCallbacks
    def test_send_sms_fail(self):
        """no clue yet how I'm going to test this."""
        path = '/api/v1/sms/vas2nets/receive/'
        port = 9999
        mocked_message_id = False
        mocked_message = "Result_code: 04, Internal system error occurred " \
                            "while processing message"
        stubbed_worker = Vas2NetsTestWorker(path, port, mocked_message_id,
                                            mocked_message, TestQueue([]))
        yield stubbed_worker.startWorker()

        transport = get_stubbed_worker(Vas2NetsTransport, {
            'transport_name': 'vas2nets',
            'url': 'http://localhost:%s%s' % (port, path),
            'username': 'username',
            'password': 'password',
            'owner': 'owner',
            'service': 'service',
            'subservice': 'subservice'
        })
        
        deferred = transport.handle_outbound_message(Message(**{
            'to_msisdn': '+27761234567',
            'from_msisdn': '9292',
            'id': '1',
            'reply_to': '',
            'transport_network_id': 'network-id',
            'message': 'hello world'
        }))
        self.assertFailure(deferred, Vas2NetsTransportError)
        yield deferred
        # nothing should've been published and no channels should've been made
        self.assertEquals({}, transport._amqp_client.channels)
        stubbed_worker.stopWorker()
    
    def test_normalize_outbound_msisdn(self):
        self.assertEquals(normalize_outbound_msisdn('+27761234567'), '0027761234567')
