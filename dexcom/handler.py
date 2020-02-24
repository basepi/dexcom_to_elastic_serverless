import json
import logging
import os
import time
from datetime import datetime, timedelta

import boto3
import elasticsearch
import elasticsearch.helpers
import requests

import elasticapm

dynamodb = boto3.resource("dynamodb")

base_url = os.environ["DEXCOM_BASE_URL"]
client_id = os.environ["DEXCOM_CLIENT_ID"]
client_secret = os.environ["DEXCOM_CLIENT_SECRET"]
redirect_uri = os.environ["DEXCOM_REDIRECT_URI"]
es_index = os.environ["ES_INDEX"]
es_user = os.environ["ES_USER"]
es_password = os.environ["ES_PASSWORD"]
es_endpoints = [os.environ["ES_ENDPOINT"]]
topic_arn = os.environ["SNS_TOPIC"]

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
timestr = "%Y-%m-%dT%H:%M:%S"
time_window = timedelta(hours=6)

# Could we do cognito or something here?
user_id = "1234567890"
access_key = os.environ["ACCESS_KEY"]


@elasticapm.capture_serverless()
def auth(event, context):
    """
    Redirect to Dexcom o-auth

    Callback will be to auth_callback()
    """
    if event["queryStringParameters"]["access_key"] != access_key:
        response = {}
        response["statusCode"] = 401
        data = {}
        response["body"] = json.dumps(data)
        return response

    auth_url = f"{base_url}/v2/oauth2/login?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=offline_access"  # noqa: E501
    response = {}
    response["statusCode"] = 302
    response["headers"] = {"Location": auth_url}
    data = {}
    response["body"] = json.dumps(data)
    return response


@elasticapm.capture_serverless()
def auth_callback(event, context):
    """
    Use the oauth code to create a token for the user

    This is a callback from the redirect in auth()
    """
    auth_code = event["queryStringParameters"]["code"]

    params = {
        "client_secret": client_secret,
        "client_id": client_id,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    headers = {"content-type": "application/x-www-form-urlencoded", "cache-control": "no-cache"}
    r = requests.post(base_url + "/v2/oauth2/token", data=params, headers=headers)
    r.raise_for_status()
    data = r.json()

    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires = int(time.time() + data["expires_in"] - 10)  # 10 second grace period

    # Save to dynamodb
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])

    # Get user info if available
    response = table.get_item(Key={"id": user_id}, ConsistentRead=True)
    if "Item" in response:
        item = response["Item"]
    else:
        item = {"id": user_id}

    item["access_token"] = access_token
    item["refresh_token"] = refresh_token
    item["expires"] = expires

    # write the user to the database
    table.put_item(Item=item)
    log.info("Token fetch and save successful.")

    response = {}
    response["statusCode"] = 200
    data = {"result": "Success!"}
    response["body"] = json.dumps(data)
    return response


@elasticapm.capture_span()
def _refresh(user_id):
    """
    Refresh the token for a user
    """
    log.info("Refreshing token.")

    # Get user info from dynamodb
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    response = table.get_item(Key={"id": user_id}, ConsistentRead=True)
    item = response["Item"]
    refresh = item["refresh_token"]

    params = {
        "client_secret": client_secret,
        "client_id": client_id,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
        "redirect_uri": redirect_uri,
    }
    headers = {"content-type": "application/x-www-form-urlencoded", "cache-control": "no-cache"}
    r = requests.post(base_url + "/v2/oauth2/token", data=params, headers=headers)
    r.raise_for_status()
    data = r.json()

    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires = int(time.time() + data["expires_in"] - 10)  # 10 second grace period

    # Save to dynamodb
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])

    item["access_token"] = access_token
    item["refresh_token"] = refresh_token
    item["expires"] = expires

    # write the user to the database
    table.put_item(Item=item)
    log.info("Token fetch and save successful.")

    return access_token, item, expires


@elasticapm.capture_serverless()
def fetch_all(event, context):
    """
    Iterates over users in the database, triggering fetch() (via SNS) for each
    """
    sns = boto3.client("sns")
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    users = table.scan(ProjectionExpression="id")["Items"]
    for user in users:
        user_id = user["id"]
        sns.publish(TopicArn=topic_arn, Message=user_id)

    response = {}
    response["statusCode"] = 200
    data = {"result": "Success!"}
    response["body"] = json.dumps(data)
    return response


@elasticapm.capture_serverless()
def fetch(event, context):
    """
    Fetch all of the records for a given user id, passed via SNS
    """
    fn_start_time = time.time()
    user_id = event["Records"][0]["Sns"]["Message"]
    log.info(f"Fetching egvs for {user_id}")

    # Get user info from dynamodb
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    response = table.get_item(Key={"id": user_id}, ConsistentRead=True)
    item = response["Item"]
    expires = item.get("expires")
    access_token = item.get("access_token")

    if not expires or time.time() > expires:
        access_token, item, expires = _refresh(user_id)

    # Set up elasticsearch
    es = elasticsearch.Elasticsearch(es_endpoints, http_auth=(es_user, es_password), scheme="https", port=443)

    # Check for previous cursor
    earliest_egv = None
    latest_egv = None
    cursor = datetime.fromtimestamp(0)
    if "cursor" in item:
        cursor = datetime.strptime(item["cursor"], timestr)

    while True:
        # Check if we need a new token
        if time.time() > expires:
            access_token, item, expires = _refresh(user_id)

        if not earliest_egv or cursor > latest_egv:
            # Get dataranges for the user
            try:
                headers = {"authorization": f"Bearer {access_token}"}
                r = requests.get(base_url + "/v2/users/self/dataRange", headers=headers)
                r.raise_for_status()
                data = r.json()
            except ConnectionError as e:
                log.error(f"Received a connection error from datarange endpoint: {e}")
                return False

            # Sometimes egv can have a decimal on the seconds. Throw it away.
            earliest_egv = datetime.strptime(data["egvs"]["start"]["systemTime"].split(".", 1)[0], timestr)
            latest_egv = datetime.strptime(data["egvs"]["end"]["systemTime"].split(".", 1)[0], timestr)

        if earliest_egv > cursor:
            cursor = earliest_egv
        elif cursor > latest_egv:
            # We've fetched all the records
            log.info("No new records found. Ending invocation.")
            return True

        # Fetch an hour of estimated glucose values (egv)
        finish = cursor + time_window

        startstr = cursor.strftime(timestr)
        finishstr = finish.strftime(timestr)

        try:
            headers = {"authorization": f"Bearer {access_token}"}
            r = requests.get(
                base_url + f"/v2/users/self/egvs?startDate={startstr}&endDate={finishstr}", headers=headers
            )
            r.raise_for_status()
            data = r.json()
        except ConnectionError as e:
            log.error(f"Received a ConnectionResetError querying egvs: {e}")
            return False

        data = _format_data(data, es_index + user_id) if data else {}

        if data:
            # Bulk send to elasticsearch
            elasticsearch.helpers.bulk(es, data)
            log.info(f"Indexed {len(data)} records from {startstr} to {finishstr}")

            # Record the time of the newest EGV we have (they are sorted from newest to oldest)
            last_egv = datetime.strptime(data[0]["_source"]["@timestamp"], timestr)
            # Update the cursor to one second past our last indexed event
            cursor = last_egv + timedelta(seconds=1)

        # We can skip ahead if the window was empty but there are already more
        # events post-window
        if finish < latest_egv:
            if not data:
                # No events in this window (and more events after this window),
                # likely due to sensor change.
                log.info(
                    "No events in the time window, but more events after. "
                    "This is likely due to a sensor change or malfunction. "
                    "Skipping time window."
                )
            cursor = finish
        else:
            # Finish is past our latest_egv, so set cursor to just past latest_egv
            cursor = latest_egv + timedelta(seconds=1)

            # Store the cursor
            item["cursor"] = cursor.strftime(timestr)
            table.put_item(Item=item)
            log.info("No new records found. Ending invocation.")
            return True

        # Store the cursor in case we get interrupted
        item["cursor"] = cursor.strftime(timestr)
        table.put_item(Item=item)

        if time.time() - fn_start_time > 800:
            log.info("Running out of time, ending invocation early.")
            return True


@elasticapm.capture_span()
def _format_data(data, es_index):
    """
    Format data for bulk indexing into elasticsearch
    """
    unit = data["unit"]
    rate_unit = data["rateUnit"]
    egvs = data["egvs"]
    docs = []

    for record in egvs:
        record["unit"] = unit
        record["rate_unit"] = rate_unit
        record["@timestamp"] = record.pop("systemTime")
        record.pop("displayTime")
        record["realtime_value"] = record.pop("realtimeValue")
        record["smoothed_value"] = record.pop("smoothedValue")
        record["trend_rate"] = record.pop("trendRate")
        docs.append({"_index": es_index, "_type": "document", "_source": record})

    return docs


@elasticapm.capture_serverless()
def delete(event, context):
    """
    Deletes a user from dynamodb
    """
    if event["queryStringParameters"]["access_key"] != access_key:
        response = {}
        response["statusCode"] = 401
        data = {}
        response["body"] = json.dumps(data)
        return response

    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    table.delete_item(Key={"id": user_id})

    response = {}
    response["statusCode"] = 200
    data = {"result": "Success!"}
    response["body"] = json.dumps(data)
    return response
