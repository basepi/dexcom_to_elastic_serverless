import json
import logging
import os
import time
from datetime import datetime, timedelta

import boto3
import elasticsearch
import elasticsearch.helpers
import requests

dynamodb = boto3.resource("dynamodb")

base_url = os.environ["DEXCOM_BASE_URL"]
client_id = os.environ["DEXCOM_CLIENT_ID"]
client_secret = os.environ["DEXCOM_CLIENT_SECRET"]
redirect_uri = os.environ["DEXCOM_REDIRECT_URI"]
es_index = os.environ["ES_INDEX"]
es_user = os.environ["ES_USER"]
es_password = os.environ["ES_PASSWORD"]
es_endpoints = [os.environ["ES_ENDPOINT"]]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
timestr = "%Y-%m-%dT%H:%M:%S"
time_window = timedelta(hours=1)

# TODO: will be from cognito eventually
user_id = "1234567890"


def auth(event, context):
    """
    Redirect to Dexcom o-auth

    Callback will be to auth_callback()
    """
    auth_url = f"{base_url}/v2/oauth2/login?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=offline_access"  # noqa: E501
    response = {}
    response["statusCode"] = 302
    response["headers"] = {"Location": auth_url}
    data = {}
    response["body"] = json.dumps(data)
    return response


def auth_callback(event, context):
    """
    Use the oauth code to create a token for the user

    This is a callback from the redirect in auth()
    """
    auth_code = event["pathParameters"]["code"]

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
    expires = time.time() + data["expires_in"] - 10  # 10 second grace period

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


def refresh(event, context):
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
    expires = time.time() + data["expires_in"] - 10  # 10 second grace period

    # Save to dynamodb
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])

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
