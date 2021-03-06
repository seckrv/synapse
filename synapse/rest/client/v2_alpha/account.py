# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2017 Vector Creations Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

from twisted.internet import defer

from synapse.api.auth import has_access_token
from synapse.api.constants import LoginType
from synapse.api.errors import Codes, LoginError, SynapseError
from synapse.http.servlet import (
    RestServlet, assert_params_in_request,
    parse_json_object_from_request,
)
from synapse.util.async import run_on_reactor
from synapse.util.msisdn import phone_number_to_msisdn
from ._base import client_v2_patterns

logger = logging.getLogger(__name__)


class EmailPasswordRequestTokenRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/password/email/requestToken$")

    def __init__(self, hs):
        super(EmailPasswordRequestTokenRestServlet, self).__init__()
        self.hs = hs
        self.identity_handler = hs.get_handlers().identity_handler

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        assert_params_in_request(body, [
            'id_server', 'client_secret', 'email', 'send_attempt'
        ])

        existingUid = yield self.hs.get_datastore().get_user_id_by_threepid(
            'email', body['email']
        )

        if existingUid is None:
            raise SynapseError(400, "Email not found", Codes.THREEPID_NOT_FOUND)

        ret = yield self.identity_handler.requestEmailToken(**body)
        defer.returnValue((200, ret))


class MsisdnPasswordRequestTokenRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/password/msisdn/requestToken$")

    def __init__(self, hs):
        super(MsisdnPasswordRequestTokenRestServlet, self).__init__()
        self.hs = hs
        self.datastore = self.hs.get_datastore()
        self.identity_handler = hs.get_handlers().identity_handler

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        assert_params_in_request(body, [
            'id_server', 'client_secret',
            'country', 'phone_number', 'send_attempt',
        ])

        msisdn = phone_number_to_msisdn(body['country'], body['phone_number'])

        existingUid = yield self.datastore.get_user_id_by_threepid(
            'msisdn', msisdn
        )

        if existingUid is None:
            raise SynapseError(400, "MSISDN not found", Codes.THREEPID_NOT_FOUND)

        ret = yield self.identity_handler.requestMsisdnToken(**body)
        defer.returnValue((200, ret))


class PasswordRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/password$")

    def __init__(self, hs):
        super(PasswordRestServlet, self).__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        self.datastore = self.hs.get_datastore()

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        body = parse_json_object_from_request(request)

        authed, result, params, _ = yield self.auth_handler.check_auth([
            [LoginType.PASSWORD],
            [LoginType.EMAIL_IDENTITY],
            [LoginType.MSISDN],
        ], body, self.hs.get_ip_from_request(request))

        if not authed:
            defer.returnValue((401, result))

        user_id = None
        requester = None

        if LoginType.PASSWORD in result:
            # if using password, they should also be logged in
            requester = yield self.auth.get_user_by_req(request)
            user_id = requester.user.to_string()
            if user_id != result[LoginType.PASSWORD]:
                raise LoginError(400, "", Codes.UNKNOWN)
        elif LoginType.EMAIL_IDENTITY in result:
            threepid = result[LoginType.EMAIL_IDENTITY]
            if 'medium' not in threepid or 'address' not in threepid:
                raise SynapseError(500, "Malformed threepid")
            if threepid['medium'] == 'email':
                # For emails, transform the address to lowercase.
                # We store all email addreses as lowercase in the DB.
                # (See add_threepid in synapse/handlers/auth.py)
                threepid['address'] = threepid['address'].lower()
            # if using email, we must know about the email they're authing with!
            threepid_user_id = yield self.datastore.get_user_id_by_threepid(
                threepid['medium'], threepid['address']
            )
            if not threepid_user_id:
                raise SynapseError(404, "Email address not found", Codes.NOT_FOUND)
            user_id = threepid_user_id
        else:
            logger.error("Auth succeeded but no known type!", result.keys())
            raise SynapseError(500, "", Codes.UNKNOWN)

        if 'new_password' not in params:
            raise SynapseError(400, "", Codes.MISSING_PARAM)
        new_password = params['new_password']

        yield self.auth_handler.set_password(
            user_id, new_password, requester
        )

        defer.returnValue((200, {}))

    def on_OPTIONS(self, _):
        return 200, {}


class DeactivateAccountRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/deactivate$")

    def __init__(self, hs):
        self.hs = hs
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        super(DeactivateAccountRestServlet, self).__init__()

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        # if the caller provides an access token, it ought to be valid.
        requester = None
        if has_access_token(request):
            requester = yield self.auth.get_user_by_req(
                request,
            )  # type: synapse.types.Requester

        # allow ASes to dectivate their own users
        if requester and requester.app_service:
            yield self.auth_handler.deactivate_account(
                requester.user.to_string()
            )
            defer.returnValue((200, {}))

        authed, result, params, _ = yield self.auth_handler.check_auth([
            [LoginType.PASSWORD],
        ], body, self.hs.get_ip_from_request(request))

        if not authed:
            defer.returnValue((401, result))

        if LoginType.PASSWORD in result:
            user_id = result[LoginType.PASSWORD]
            # if using password, they should also be logged in
            if requester is None:
                raise SynapseError(
                    400,
                    "Deactivate account requires an access_token",
                    errcode=Codes.MISSING_TOKEN
                )
            if requester.user.to_string() != user_id:
                raise LoginError(400, "", Codes.UNKNOWN)
        else:
            logger.error("Auth succeeded but no known type!", result.keys())
            raise SynapseError(500, "", Codes.UNKNOWN)

        yield self.auth_handler.deactivate_account(user_id)
        defer.returnValue((200, {}))


class EmailThreepidRequestTokenRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/3pid/email/requestToken$")

    def __init__(self, hs):
        self.hs = hs
        super(EmailThreepidRequestTokenRestServlet, self).__init__()
        self.identity_handler = hs.get_handlers().identity_handler
        self.datastore = self.hs.get_datastore()

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        required = ['id_server', 'client_secret', 'email', 'send_attempt']
        absent = []
        for k in required:
            if k not in body:
                absent.append(k)

        if absent:
            raise SynapseError(400, "Missing params: %r" % absent, Codes.MISSING_PARAM)

        existingUid = yield self.datastore.get_user_id_by_threepid(
            'email', body['email']
        )

        if existingUid is not None:
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        ret = yield self.identity_handler.requestEmailToken(**body)
        defer.returnValue((200, ret))


class MsisdnThreepidRequestTokenRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/3pid/msisdn/requestToken$")

    def __init__(self, hs):
        self.hs = hs
        super(MsisdnThreepidRequestTokenRestServlet, self).__init__()
        self.identity_handler = hs.get_handlers().identity_handler
        self.datastore = self.hs.get_datastore()

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        required = [
            'id_server', 'client_secret',
            'country', 'phone_number', 'send_attempt',
        ]
        absent = []
        for k in required:
            if k not in body:
                absent.append(k)

        if absent:
            raise SynapseError(400, "Missing params: %r" % absent, Codes.MISSING_PARAM)

        msisdn = phone_number_to_msisdn(body['country'], body['phone_number'])

        existingUid = yield self.datastore.get_user_id_by_threepid(
            'msisdn', msisdn
        )

        if existingUid is not None:
            raise SynapseError(400, "MSISDN is already in use", Codes.THREEPID_IN_USE)

        ret = yield self.identity_handler.requestMsisdnToken(**body)
        defer.returnValue((200, ret))


class ThreepidRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/3pid$")

    def __init__(self, hs):
        super(ThreepidRestServlet, self).__init__()
        self.hs = hs
        self.identity_handler = hs.get_handlers().identity_handler
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        self.datastore = self.hs.get_datastore()

    @defer.inlineCallbacks
    def on_GET(self, request):
        yield run_on_reactor()

        requester = yield self.auth.get_user_by_req(request)

        threepids = yield self.datastore.user_get_threepids(
            requester.user.to_string()
        )

        defer.returnValue((200, {'threepids': threepids}))

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        body = parse_json_object_from_request(request)

        threePidCreds = body.get('threePidCreds')
        threePidCreds = body.get('three_pid_creds', threePidCreds)
        if threePidCreds is None:
            raise SynapseError(400, "Missing param", Codes.MISSING_PARAM)

        requester = yield self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        threepid = yield self.identity_handler.threepid_from_creds(threePidCreds)

        if not threepid:
            raise SynapseError(
                400, "Failed to auth 3pid", Codes.THREEPID_AUTH_FAILED
            )

        for reqd in ['medium', 'address', 'validated_at']:
            if reqd not in threepid:
                logger.warn("Couldn't add 3pid: invalid response from ID server")
                raise SynapseError(500, "Invalid response from ID Server")

        yield self.auth_handler.add_threepid(
            user_id,
            threepid['medium'],
            threepid['address'],
            threepid['validated_at'],
        )

        if 'bind' in body and body['bind']:
            logger.debug(
                "Binding threepid %s to %s",
                threepid, user_id
            )
            yield self.identity_handler.bind_threepid(
                threePidCreds, user_id
            )

        defer.returnValue((200, {}))


class ThreepidDeleteRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/3pid/delete$", releases=())

    def __init__(self, hs):
        super(ThreepidDeleteRestServlet, self).__init__()
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        body = parse_json_object_from_request(request)

        required = ['medium', 'address']
        absent = []
        for k in required:
            if k not in body:
                absent.append(k)

        if absent:
            raise SynapseError(400, "Missing params: %r" % absent, Codes.MISSING_PARAM)

        requester = yield self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        yield self.auth_handler.delete_threepid(
            user_id, body['medium'], body['address']
        )

        defer.returnValue((200, {}))


class WhoamiRestServlet(RestServlet):
    PATTERNS = client_v2_patterns("/account/whoami$")

    def __init__(self, hs):
        super(WhoamiRestServlet, self).__init__()
        self.auth = hs.get_auth()

    @defer.inlineCallbacks
    def on_GET(self, request):
        requester = yield self.auth.get_user_by_req(request)

        defer.returnValue((200, {'user_id': requester.user.to_string()}))


def register_servlets(hs, http_server):
    EmailPasswordRequestTokenRestServlet(hs).register(http_server)
    MsisdnPasswordRequestTokenRestServlet(hs).register(http_server)
    PasswordRestServlet(hs).register(http_server)
    DeactivateAccountRestServlet(hs).register(http_server)
    EmailThreepidRequestTokenRestServlet(hs).register(http_server)
    MsisdnThreepidRequestTokenRestServlet(hs).register(http_server)
    ThreepidRestServlet(hs).register(http_server)
    ThreepidDeleteRestServlet(hs).register(http_server)
    WhoamiRestServlet(hs).register(http_server)
