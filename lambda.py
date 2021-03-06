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
from delete_configuration import (
    delete_index,
    delete_document
)


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
    destination_index=None,
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

    if not old_count:
        if set_new_index(endpoint, awsauth, source_index, mappings=mappings):
            return True

    elif old_count:
        new_index = set_new_index(endpoint, awsauth, destination_index, mappings=mappings)
        reindex = set_reindex(endpoint, awsauth, source_index, destination_index)

        if new_index and reindex:
            for x in range(1, retry + 1):
                update_count = get_document_count(endpoint, awsauth, destination_index, filter_header)
                if update_count and old_count == update_count:
                    delete_index(endpoint, awsauth, source_index)
                    return True
                else:
                    time.sleep(pow(x, 2))

    print('Notice (remap_index): neither reindex action implemented')

    return False


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
    monitor_name             = properties.get('MonitorName', ''). strip()
    monitor_interval         = int(properties.get('MonitorInterval', '5'). strip())
    monitor_unit             = properties.get('MonitorUnit', 'MINUTES'). strip()
    monitor_condition        = properties.get('MonitorCondition', 'ctx.results[0].hits.total.value > 5'). strip()
    monitor_range_field      = properties.get('MonitorRangeField', 'timestamp'). strip()
    monitor_range_from       = properties.get('MonitorRangeFrom', 'now-1h'). strip()
    monitor_range_to         = properties.get('MonitorRangeTo', 'now'). strip()
    monitor_query_terms      = json.loads(properties.get('MonitorQueryTerms', '{}'). strip())
    monitor_trigger_subject  = properties.get('MonitorTriggerSubject', 'Monitor Triggered'). strip()
    monitor_trigger_message  = properties.get('MonitorTriggerMessage', 'Monitor detected {} satisfying {} within {}'.format(
        monitor_query_terms,
        monitor_condition,
        monitor_interval
    )).strip()
    mappings                 = json.loads(properties.get('Mappings', '{}').strip())
    initialize_dashboard     = bool(strtobool(properties.get('InitalizeDashboard', 'False').strip().capitalize()))
    document_delete_range    = properties.get('DocumentDeleteRange', {})
    executions               = []

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
        print('Notice: changing {} request_type to {} to skip logic'.format(
            request_type,
            None
        ))
        request_type = None

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
        # reindex: using index field mapping
        #
        if mappings:
            if get_document_count(endpoint, awsauth, index, 'index,docs.count'):
                if remap_index(endpoint, awsauth, index, '{}_temporary'.format(index)):
                    r = remap_index(
                        endpoint,
                        awsauth,
                        '{}_temporary'.format(index),
                        index,
                        mappings=mappings
                    )
                    executions.append(True if r else False)

                else:
                    executions.append(False)

            else:
                r = remap_index(endpoint, awsauth, index, mappings=mappings)
                executions.append({'set_reindex': True} if r else {'set_reindex': False})

        if initialize_dashboard:
            #
            # create index pattern: used by dashboard
            #
            index_id = index.replace('*', '').rstrip('-').rstrip('_')
            current_id = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)

            if current_id != index_id:
                r = set_index_pattern(endpoint, awsauth, index_id=index_id, title=index)
                current_id = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)
                executions.append({'set_index_pattern': True} if r else {'set_index_pattern': False})

            #
            # create dashboard: if index and index pattern exists
            #
            if (
                current_id and
                check_index(endpoint, awsauth, index) and
                not check_dashboard(endpoint, awsauth, index)
            ):
                r = set_dashboard(endpoint, awsauth, index)
                executions.append({'set_dashboard': True} if r else {'set_dashboard': False})

            else:
                executions.append({'set_dashboard': False})

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

                r = None
                if not destination:
                    r = set_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name,
                        sns_topic_arn,
                        sns_role_arn
                    )

                executions.append({'set_destination': True} if r else {'set_destination': False})

            except Exception as e:
                print('Error (set_alert_destination): attempt failed with {}'.format(e))
                executions.append({'set_destination': False})

        ##
        ## delete document: using provided range
        ##
        if document_delete_range:
            r = delete_document(endpoint, awsauth, index, document_delete_range)
            executions.append({'delete_document': True} if r else {'delete_document': False})

        ##
        ## monitor: used to setup alerting using exist sns topic
        ##
        if monitor_name and sns_alert_name and index:
            destination_id = get_alert_destination(endpoint, awsauth, sns_alert_name)

            if destination_id:
                r = set_monitor(
                    endpoint,
                    awsauth,
                    monitor_name,
                    destination_id=destination_id,
                    indices=[index],
                    schedule_interval=monitor_interval,
                    schedule_unit=monitor_unit,
                    post_date_field=monitor_range_field,
                    post_date_from=monitor_range_from,
                    post_date_to=monitor_range_to,
                    monitor_query_terms=monitor_query_terms,
                    trigger_condition_source=monitor_condition,
                    trigger_action_subject=monitor_trigger_subject,
                    trigger_action_message=monitor_trigger_message
                )
                executions.append({'set_alert': True} if r else {'set_alert': False})

    elif request_type == 'Update':
        if initialize_dashboard:
            #
            # create index pattern: used by dashboard
            #
            index_id = index.replace('*', '').rstrip('-').rstrip('_')
            current_id = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)

            if current_id and current_id != index_id:
                r = set_index_pattern(endpoint, awsauth, index_id=index_id, title=index)
                current_id = check_index_pattern(endpoint, awsauth, index_id=index_id, title=index)
                executions.append({'set_index_pattern': True} if r else {'set_index_pattern': False})

            #
            # create dashboard: if index and index pattern exists
            #
            if (
                current_id and
                check_index(endpoint, awsauth, index) and
                not check_dashboard(endpoint, awsauth, index)
            ):
                r = set_dashboard(endpoint, awsauth, index)
                executions.append({'set_dashboard': True} if r else {'set_dashboard': False})

            else:
                executions.append({'set_dashboard': False})

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

                r = None
                if not destination:
                    r = set_alert_destination(
                        endpoint,
                        awsauth,
                        sns_alert_name,
                        sns_topic_arn,
                        sns_role_arn,
                        update=True
                    )

                executions.append({'set_destination': True} if r else {'set_destination': False})

            except Exception as e:
                print('Error (set_alert_destination): attempt failed with {}'.format(e))
                executions.append({'set_destination': False})

        ##
        ## delete document: using provided range
        ##
        if document_delete_range:
            r = delete_document(endpoint, awsauth, index, document_delete_range)
            executions.append({'delete_document': True} if r else {'delete_document': False})

        ##
        ## monitor: used to setup alerting using exist sns topic
        ##
        if monitor_name and sns_alert_name and index:
            destination_id = get_alert_destination(endpoint, awsauth, sns_alert_name)

            if destination_id:
                monitor_id = ''
                monitor = get_monitor(endpoint, awsauth, monitor_name)

                if 'hits' in monitor and 'hits' in monitor['hits']:
                    monitor_id = monitor['hits']['hits'][0]['_index']

                r = set_monitor(
                    endpoint,
                    awsauth,
                    monitor_name,
                    destination_id=destination_id,
                    monitor_id=monitor_id,
                    indices=[index],
                    schedule_interval=monitor_interval,
                    schedule_unit=monitor_unit,
                    post_date_field=monitor_range_field,
                    post_date_from=monitor_range_from,
                    post_date_to=monitor_range_to,
                    monitor_query_terms=monitor_query_terms,
                    trigger_condition_source=monitor_condition,
                    trigger_action_subject=monitor_trigger_subject,
                    trigger_action_message=monitor_trigger_message
                )
                executions.append({'set_alert': True} if r else {'set_alert': False})

    elif request_type == 'Delete':
        executions.append({'delete': True})
        pass

    else:
        print('Error: request_type={} is not valid'.format(request_type))

    #
    # return condition: lambda invoked by cloudformation
    #
    if 'StackId' in event:
        response_url = event['ResponseURL']
        response_body = {}

        print('Notice: overall execution {}'.format(executions))
        if all(list(y.values())[0] for y in [x for x in executions]):
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
            response_body['Data'] = {'executions': executions}

        else:
            response_body['Data'] = {'executions': executions}

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
        print('Notice: overall execution {}'.format(executions))
        if request_type == 'Create' or request_type == 'Update':
            if all(list(y.values())[0] for y in [x for x in executions]):
                return True
            return False


if __name__ == '__main__':
    lambda_handler()
