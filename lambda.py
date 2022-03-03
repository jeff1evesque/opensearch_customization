import os
import json
import time
import boto3
import requests
from requests_aws4auth import AWS4Auth
from distutils.util import strtobool
from get_configuration import (
    get_indices,
    get_index_pattern,
    get_alert_destination,
    get_dashboard,
    get_document_count,
    get_monitor
)
from set_configuration import (
    set_index_pattern,
    set_alert_destination,
    set_new_index,
    set_reindex,
    set_dashboard,
    set_monitor
)
from delete_configuration import delete_index


def check_index(endpoint, awsauth, index):
    '''

    check opensearch index exists

    '''

    r = get_indices(endpoint, awsauth)
    found_index = None

    for x in r:
        found_index = next((y for y in x.split() if y.decode('utf-8') == index), None)
        if found_index:
            return True

    return False


def check_index_pattern(endpoint, awsauth, index_id, title):
    '''

    check opensearch index pattern exists

    '''

    r = get_index_pattern(endpoint, awsauth, index_id, title)

    if r and 'id' in r:
        return r['id']

    return r


def check_dashboard(endpoint, awsauth, title):
    '''

    check opensearch dashboard exists

    '''

    r = get_dashboard(endpoint, awsauth, title)

    if r and 'id' in r:
        return r['id']

    return r


def remap_index(
    endpoint,
    awsauth,
    source_index,
    destination_index,
    mappings={},
    retry=15,
    filter_header='index,docs.count'
):
    '''

    create new index with optional mapping, reindex old index into new index,
    finally delete old index

    @retry, depending on index size (i.e. document count), the requested remap
        process may take longer than either the exponential back-off, or overall
        lambda timeout definition

    Note: this function is designed to be executed in the early stages of
          index deployment, mainly to enhance cloudformation deployments

    '''

    old_count = get_document_count(endpoint, awsauth, source_index, filter_header)
    set_new_index(endpoint, awsauth, destination_index, mappings=mappings)
    set_reindex(endpoint, awsauth, source_index, destination_index)

    for x in range(1, retry + 1):
        update_count = get_document_count(endpoint, awsauth, destination_index, filter_header)
        if old_count and update_count and old_count == update_count:
            delete_index(endpoint, awsauth, source_index)
            print('Notice: old index {} deleted'.format(source_index))
            return True
        else:
            time.sleep(pow(x, 2))

    print('Error: old index {} not deleted'.format(source_index))
    return False


def create_alarm(
    endpoint,
    awsauth,
    monitor_name,
    sns_alert_name,
    indices,
    trigger_action_message,
    trigger_action_subject
):
    '''

    create index monitoring alarm using provided 'destination_id', which
    corresponds to an associated SNS topic

    '''

    monitor_id = ''
    monitor = get_monitor(endpoint, awsauth, monitor_name)
    destination_id = get_alert_destination(endpoint, awsauth, sns_alert_name)

    if 'hits' in monitor and 'hits' in monitor['hits']:
        monitor_id = monitor['hits']['hits'][0]['_index']

    return set_monitor(
        endpoint,
        awsauth,
        monitor_name,
        destination_id=destination_id,
        monitor_id=monitor_id,
        indices=indices,
        trigger_action_name=trigger_action_message,
        trigger_action_message=trigger_action_subject
    )


def lambda_handler(event, context, physicalResourceId=None, noEcho=False):
    '''

    configure opensearch domain with trigger/notification, and dashboard(s)

    @index, must be all lowercase, and cannot start with hyphen or underscore

    '''

    tracing_enabled          = bool(strtobool(os.getenv('TracingEnabled', 'True').strip().capitalize()))
    properties               = event.get('ResourceProperties', {})
    request_type             = event.get('RequestType', None)
    region                   = properties.get('Region', os.environ['AWS_REGION']).strip()
    endpoint                 = properties.get('OpenSearchDomain', '').strip()
    index                    = properties.get('OpenSearchIndex', '').strip()
    headers                  = json.loads(properties.get('Headers', '{"Content-Type": "application/json"}').strip())
    sns_alert_name           = properties.get('SnsAlertName', ''). strip()
    sns_topic_arn            = properties.get('SnsTopicArn', ''). strip()
    sns_role_arn             = properties.get('SnsRoleArn', ''). strip()
    mappings                 = json.loads(properties.get('Mappings', '{}').strip())
    executions               = []

    response_sns_destination = None

    #
    # version 4 authentication for the python requests
    #
    credentials = boto3.Session().get_credentials()

    try:
        awsauth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            region,
            'es',
            session_token=credentials.token
        )

    except Exception as e:
        print('Error (AWS4Auth): {}'.format(str(e)))
        return False

    #
    # x-ray tracing
    #
    if tracing_enabled:
        from aws_xray_sdk.core import xray_recorder
        from aws_xray_sdk.core import patch_all
        patch_all()

    #
    # Note: 'StackId' in 'event' signify cloudformation execution
    #
    if request_type == 'Create':
        #
        # sns destination
        #
        if sns_alert_name and sns_topic_arn and sns_role_arn:
            try:
                destination = get_alert_destination(
                    endpoint,
                    awsauth,
                    sns_alert_name
                )

                if not destination:
                    response_sns_destination = set_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name,
                        sns_topic_arn,
                        sns_role_arn
                    )

                    destination = get_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name
                    )

                executions.append(True if response_sns_destination else False)

            except Exception as e:
                print('Error (set_alert_destination): attempt failed with {}'.format(e))
                executions.append(False)

        #
        # reindex: using index field mapping
        #
        if mappings:
            if remap_index(endpoint, awsauth, index, '{}_temporary'.format(index)):
                remap_index(
                    endpoint,
                    awsauth,
                    '{}_temporary'.format(index),
                    index,
                    mappings=mappings
                )

        #
        # create index pattern: used by dashboard
        #
        index_id = index.replace('*', '').rstrip('-').rstrip('_')
        r = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)

        if r != index_id:
            set_index_pattern(endpoint, awsauth, index_id=index_id, title=index)

        #
        # create dashboard: if index and index pattern exists
        #
        if (
            check_index(endpoint, awsauth, index) and
            check_index_pattern(endpoint, awsauth, index_id=index_id, title=index) and
            not check_dashboard(endpoint, awsauth, index)
        ):
            set_dashboard(endpoint, awsauth, index)

    elif request_type == 'Update':
        #
        # sns destination
        #
        if sns_alert_name and sns_topic_arn and sns_role_arn:
            try:
                destination = get_alert_destination(
                    endpoint,
                    awsauth,
                    sns_alert_name
                )

                if not destination:
                    response_sns_destination = set_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name,
                        sns_topic_arn,
                        sns_role_arn,
                        update=True
                    )

                    destination = get_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name
                    )

                executions.append(True if response_sns_destination else False)

            except Exception as e:
                print('Error (set_alert_destination): attempt failed with {}'.format(e))
                executions.append(False)

        #
        # create index pattern: used by dashboard
        #
        index_id = index.replace('*', '').rstrip('-').rstrip('_')
        r = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)

        if r != index_id:
            set_index_pattern(endpoint, awsauth, index_id=index_id, title=index, update=True)

        #
        # create dashboard: if index and index pattern exists
        #
        if (
            check_index(endpoint, awsauth, index) and
            check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)
        ):
            set_dashboard(endpoint, awsauth, index, update=True)

    elif request_type == 'Delete':
        pass

    else:
        print('Error: request_type={} is not valid'.format(request_type))

    #
    # return condition: lambda invoked by cloudformation
    #
    if 'StackId' in event:
        response_url = event['ResponseURL']

        print(response_url)

        response_body = {}

        if all(x for x in executions):
            response_body['Status'] = 'SUCCESS'
        else:
            response_body['Status'] = 'FAILED'

        response_body['Reason'] = '{a}: {b}'.format(
            a='See the details in CloudWatch Log Stream',
            b=context.log_stream_name
        )
        response_body['PhysicalResourceId'] = physicalResourceId or context.log_stream_name
        response_body['StackId'] = event['StackId']
        response_body['RequestId'] = event['RequestId']
        response_body['LogicalResourceId'] = event['LogicalResourceId']
        response_body['NoEcho'] = noEcho

        if request_type == 'Create' or request_type == 'Update':
            response_body['Data'] = {
                'response_sns_destination': response_sns_destination
            }

        else:
            response_body['Data'] = {
                'response_sns_destination': response_sns_destination
            }

        response_json = json.dumps(response_body)

        print('Response body: {}'.format(response_json))

        headers = {
            'content-type': '',
            'content-length': str(len(response_json))
        }

        try:
            response = requests.put(
                response_url,
                data=response_json,
                headers=headers
            )
            print('Status code: {}'.format(response.reason))

        except Exception as e:
            print('send(..) failed executing requests.put(..): {}'.format(e))

    #
    # return condition: lambda invoked by something else
    #
    else:
        if request_type == 'Create' or request_type == 'Update':
            return {
                'response_sns_destination': response_sns_destination
            }

        else:
            return {
                'response_sns_destination': response_sns_destination
            }

if __name__ == '__main__':
    lambda_handler()