import boto3
import json
import os
from datetime import datetime, timezone

bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')
bedrock_runtime = boto3.client('bedrock-runtime')
s3 = boto3.client('s3')

LOG_BUCKET = os.environ["DATA_BUCKET"]

KB_ID = os.environ["KNOWLEDGE_BASE_ID"]
GUARDRAIL_ID = "8kow3zjhftvg"
GUARDRAIL_VERSION = "1"

SYSTEM_PROMPT = s3.get_object(Bucket=LOG_BUCKET, Key='system_prompt.txt')['Body'].read().decode('utf-8')

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'POST, OPTIONS'
}

def apply_guardrail(text, source):
    result = bedrock_runtime.apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VERSION,
        source=source,
        content=[{'text': {'text': text}}]
    )
    if result['action'] == 'GUARDRAIL_INTERVENED':
        return result['outputs'][0]['text']
    return None

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")
    body = json.loads(event.get('body', '{}'))
    print(f"Event body: {json.dumps(body)}")
    user_query = body.get('question', '')

    # blocked = apply_guardrail(user_query, 'INPUT')
    # if blocked:
    #     return {
    #         'statusCode': 200,
    #         'headers': CORS_HEADERS,
    #         'body': json.dumps({'answer': blocked, 'sources': []})
    #     }

    response = bedrock_agent_runtime.agentic_retrieve_stream(
        messages=[{
            'content': {'text': f"{SYSTEM_PROMPT}\n{user_query}"},
            'role': 'user'
        }],
        retrievers=[{
            'configuration': {
                'knowledgeBase': {
                    'knowledgeBaseId': KB_ID
                }
            }
        }],
        agenticRetrieveConfiguration={
            'foundationModelType': 'MANAGED',
            'rerankingModelType': 'MANAGED'
        },
        generateResponse=True
    )

    all_results = []
    generated = {}

    for stream_event in response['stream']:
        if 'result' in stream_event:
            evt = stream_event['result']
            if evt.get('results'):
                all_results.extend(evt['results'])
            if evt.get('generatedResponse'):
                generated = evt['generatedResponse']

    results = all_results
    answer = generated.get('answer', '')
    citations = generated.get('citations', [])

    def get_source_metadata(result):
        meta = result.get('metadata', {})
        member_url = meta.get('memberUrl', '')
        if not member_url:
            return None
        return {
            'memberUrl': member_url,
            'productUrl': meta.get('productUrl', ''),
            'title': meta.get('title', ''),
            'productType': meta.get('productType', ''),
            'airDate': meta.get('airDate', ''),
            'speaker': meta.get('speaker', ''),
            'productImage': meta.get('productImage', ''),
        }

    unique_sources = []
    url_to_num = {}
    insertions = []

    for citation in citations:
        end = citation.get('endIndex', 0)
        nums = []
        for ref in citation.get('references', []):
            idx = ref.get('resultIndex')
            if idx is not None and idx < len(results):
                source = get_source_metadata(results[idx])
                if source:
                    member_url = source['memberUrl']
                    if member_url not in url_to_num:
                        unique_sources.append(source)
                        url_to_num[member_url] = len(unique_sources)
                    nums.append(url_to_num[member_url])
        if nums and end:
            insertions.append((end, sorted(set(nums))))

    for pos, nums in sorted(insertions, key=lambda x: x[0], reverse=True):
        markers = ''.join(f'[{n}]' for n in nums)
        answer = answer[:pos] + markers + answer[pos:]

    # blocked = apply_guardrail(answer, 'OUTPUT')
    # if blocked:
    #     answer = blocked
    #     unique_sources = []

    print(answer)

    try:
        now = datetime.now(timezone.utc)
        log_record = {
            'timestamp': now.isoformat(),
            'request_id': context.aws_request_id,
            'question': user_query,
            'answer': answer,
            'sources': unique_sources,
        }
        key = f"logs/{now.strftime('%Y/%m/%d')}/{context.aws_request_id}.json"
        s3.put_object(
            Bucket=LOG_BUCKET,
            Key=key,
            Body=json.dumps(log_record, indent=2),
            ContentType='application/json',
        )
    except Exception as e:
        print(f"S3 log failed: {e}")

    return {
        'statusCode': 200,
        'headers': CORS_HEADERS,
        'body': json.dumps({'answer': answer, 'sources': unique_sources})
    }
