# -*- coding: utf-8 -*-
from datetime import datetime
import dateutil.parser
from dateutil.tz import tzlocal
from base64 import urlsafe_b64encode, urlsafe_b64decode
from sys import exc_info
import requests
from themis.finals.api.auth import issue_checker_token
import os
from themis.finals.checker.result import Result
from imp import load_source
import logging
import raven
import jwt


class Metadata(object):
    def __init__(self, options):
        self._timestamp = options.get('timestamp', None)
        self._round = options.get('round', None)
        self._team_name = options.get('team_name', u'')
        self._service_name = options.get('service_name', u'')

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def round(self):
        return self._round

    @property
    def team_name(self):
        return self._team_name

    @property
    def service_name(self):
        return self._service_name


logger = logging.getLogger(__name__)

checker_module_name = os.getenv(
    'THEMIS_FINALS_CHECKER_MODULE',
    os.path.join(os.getcwd(), 'checker.py')
)
checker_module = load_source('', checker_module_name)

raven_enabled = os.getenv('SENTRY_DSN', None) is not None
raven_client = None
if raven_enabled:
    raven_client = raven.Client(dsn=os.getenv('SENTRY_DSN'))


def internal_push(endpoint, capsule, label, metadata):
    result = Result.INTERNAL_ERROR
    updated_label = label
    message = None
    try:
        raw_result = checker_module.push(endpoint, capsule, label, metadata)
        if isinstance(raw_result, tuple):
            if len(raw_result) > 0:
                result = raw_result[0]
            if len(raw_result) > 1:
                updated_label = raw_result[1]
            if len(raw_result) > 2:
                message = raw_result[2]
        else:
            result = raw_result
    except Exception:
        if raven_enabled:
            raven_client.captureException()
        logger.exception('An exception occurred', exc_info=exc_info())
    return result, updated_label, message


def internal_pull(endpoint, capsule, label, metadata):
    result = Result.INTERNAL_ERROR
    message = None
    try:
        raw_result = checker_module.pull(endpoint, capsule, label, metadata)
        if isinstance(raw_result, tuple):
            if len(raw_result) > 0:
                result = raw_result[0]
            if len(raw_result) > 1:
                message = raw_result[1]
        else:
            result = raw_result
    except Exception:
        if raven_enabled:
            raven_client.captureException()
        logger.exception('An exception occurred', exc_info=exc_info())
    return result, message


def decode_capsule(capsule):
    wrap_prefix = os.getenv('THEMIS_FINALS_FLAG_WRAP_PREFIX')
    wrap_suffix = os.getenv('THEMIS_FINALS_FLAG_WRAP_SUFFIX')
    token_start = len(wrap_prefix)
    token_end = -len(wrap_suffix)
    encoded_payload = capsule[token_start:token_end]

    key = os.getenv('THEMIS_FINALS_FLAG_SIGN_KEY_PUBLIC').replace('\\n', "\n")

    payload = jwt.decode(
        encoded_payload,
        algorithms=['ES256', 'RS256'],
        key=key
    )
    return payload['flag']


def queue_push(job_data):
    params = job_data['params']
    metadata = Metadata(job_data['metadata'])
    timestamp_created = dateutil.parser.parse(metadata.timestamp)
    timestamp_delivered = datetime.now(tzlocal())

    flag = decode_capsule(params['capsule'])

    status, updated_label, message = internal_push(
        params['endpoint'],
        params['capsule'],
        urlsafe_b64decode(params['label'].encode('utf-8')),
        metadata
    )

    timestamp_processed = datetime.now(tzlocal())

    job_result = dict(
        status=status.value,
        flag=flag,
        label=urlsafe_b64encode(updated_label),
        message=message
    )

    delivery_time = (timestamp_delivered - timestamp_created).total_seconds()
    processing_time = (
        timestamp_processed - timestamp_delivered
    ).total_seconds()

    log_message = (u'PUSH flag `{0}` /{1:d} to `{2}`@`{3}` ({4}) - status {5},'
                   u' label `{6}` [delivery {7:.2f}s, processing '
                   u'{8:.2f}s]').format(
        flag,
        metadata.round,
        metadata.service_name,
        metadata.team_name,
        params['endpoint'],
        status.name,
        job_result['label'],
        delivery_time,
        processing_time
    )

    if raven_enabled:
        short_log_message = u'PUSH `{0}...` /{1:d} to `{2}` - status {3}'.format(
            flag[0:8],
            metadata.round,
            metadata.team_name,
            status.name
        )

        raven_client.captureMessage(
            short_log_message,
            level=logging.INFO,
            tags={
                'tf_operation': 'push',
                'tf_status': status.name,
                'tf_team': metadata.team_name,
                'tf_service': metadata.service_name,
                'tf_round': metadata.round
            },
            extra={
                'endpoint': params['endpoint'],
                'flag': flag,
                'label': job_result['label'],
                'message': job_result['message'],
                'delivery_time': delivery_time,
                'processing_time': processing_time
            }
        )

    logger.info(log_message)

    uri = job_data['report_url']
    headers = {}
    headers[os.getenv('THEMIS_FINALS_AUTH_TOKEN_HEADER')] = \
        issue_checker_token()
    r = requests.post(uri, headers=headers, json=job_result)
    if r.status_code != requests.codes.ok:
        logger.error(r.status_code)
        logger.error(r.reason)


def queue_pull(job_data):
    params = job_data['params']
    metadata = Metadata(job_data['metadata'])
    timestamp_created = dateutil.parser.parse(metadata.timestamp)
    timestamp_delivered = datetime.now(tzlocal())

    flag = decode_capsule(params['capsule'])

    status, message = internal_pull(
        params['endpoint'],
        params['capsule'],
        urlsafe_b64decode(params['label'].encode('utf-8')),
        metadata
    )

    timestamp_processed = datetime.now(tzlocal())

    job_result = dict(
        request_id=params['request_id'],
        status=status.value,
        message=message
    )

    delivery_time = (timestamp_delivered - timestamp_created).total_seconds()
    processing_time = (
        timestamp_processed - timestamp_delivered
    ).total_seconds()

    log_message = (u'PULL flag `{0}` /{1:d} from `{2}`@`{3}` ({4}) with '
                   u'label `{5}` - status {6} [delivery {7:.2f}s, '
                   u'processing {8:.2f}s]').format(
        flag,
        metadata.round,
        metadata.service_name,
        metadata.team_name,
        params['endpoint'],
        params['label'],
        status.name,
        delivery_time,
        processing_time
    )

    if raven_enabled:
        short_log_message = u'PULL `{0}...` /{1:d} from `{2}` - status {3}'.format(
            flag[0:8],
            metadata.round,
            metadata.team_name,
            status.name
        )

        raven_client.captureMessage(
            short_log_message,
            level=logging.INFO,
            tags={
                'tf_operation': 'pull',
                'tf_status': status.name,
                'tf_team': metadata.team_name,
                'tf_service': metadata.service_name,
                'tf_round': metadata.round
            },
            extra={
                'endpoint': params['endpoint'],
                'flag': flag,
                'label': params['label'],
                'message': message,
                'delivery_time': delivery_time,
                'processing_time': processing_time
            }
        )

    logger.info(log_message)

    uri = job_data['report_url']
    headers = {}
    headers[os.getenv('THEMIS_FINALS_AUTH_TOKEN_HEADER')] = \
        issue_checker_token()
    r = requests.post(uri, headers=headers, json=job_result)
    if r.status_code != requests.codes.ok:
        logger.error(r.status_code)
        logger.error(r.reason)
