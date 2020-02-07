# Dexcom to Elasticsearch Connector -- Serverless Edition

I thought it would be interesting to ingest my CGM data from my Dexcom G6
into Elasticsearch. Dexcom has a [surprisingly competent API](https://developer.dexcom.com/),
which certainly helped.

My [first attempt](https://github.com/basepi/dexcom_to_elasticsearch) was
just a simple script. For round two I decided to try out
[Serverless Framework](https://serverless.com/), and do the whole thing on
AWS Lambda.

Here is the result. I set it up using DynamoDB with the intention of being
able to handle multiple users. But Cognito turned out to be more of a hassle
than I wanted to deal with right now, so I made it single user with a
query parameter secret as a poor excuse for auth.

# Installation

1. Install docker (for `serverless-python-requirements`)

2. Install Serverless

```
brew install serverless
serverless plugin install -n serverless-python-requirements
serverless login
```

3. Create your parameters in your Serverless dashboard profile:

* DEXCOM_CLIENT_ID: provided by your dexcom app in the dexcom developer portal
* DEXCOM_CLIENT_SECRET: same as above
* ES_USER: elasticsearch user for auth
* ES_PASSWORD: elasticsearch password for auth
* ES_ENDPOINT: elasticsearch endpoint to which we will send the documents
* ES_INDEX: elasticsearch index prefix for the documents. The user_id will be appended to this for the final index name.
* ACCESS_KEY: this is your poor excuse for auth. Can be anything, you'll add this at the end of your auth URL: `/auth?access_key=myawesomekey`

4. Install and auth aws cli:

```
brew install aws-cli
aws configure
```

5. Do your first `serverless deploy`

6. Grab the API endpoint for `/auth_callback`, this is what you'll put into your dexcom app settings as redirect_uri

# Use

Navigate to your `/auth` endpoint, appending your access key:
`?access_key=myawesomekey`.

You'll sign into your dexcom account and then be sent back to the API, you
should see `Success`.

That's it! Hourly, the `/fetch_all` endpoint will run, iterating over all (1)
users in your dynamodb, triggering the `fetch` function via SNS. That
function will fetch all your glucose values and send them to elasticsearch.

If you ever want it to stop, you can delete your functions, or just hit
`/delete?access_key=myawesomekey` to delete yourself from dynamodb.

Finally, make some cool visualizations in Kibana! This dashboard is one
I made with my real data from my Dexcom G6 continuous glucose monitor:

<img width="1534" alt="image" src="https://user-images.githubusercontent.com/702318/74065924-37102500-49b3-11ea-9d6a-3b51300c81eb.png">