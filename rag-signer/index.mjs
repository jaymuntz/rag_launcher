import { createHmac, createHash } from 'crypto';
import { request as httpsRequest } from 'https';

const REGION = 'us-east-1';
const FUNC_NAME = '__APP_NAME__-rag-node';

function hmac(key, msg) {
  return createHmac('sha256', typeof key === 'string' ? Buffer.from(key, 'utf-8') : key)
    .update(msg).digest();
}

function getSigningKey(service, secretKey, datestamp) {
  return hmac(hmac(hmac(hmac(`AWS4${secretKey}`, datestamp), REGION), service), 'aws4_request');
}

function invokeLambda(body, ak, sk, tok) {
  const now = new Date();
  const amzDate = now.toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z';
  const datestamp = amzDate.slice(0, 8);
  const payloadHash = createHash('sha256').update(body).digest('hex');
  const hostname = `lambda.${REGION}.amazonaws.com`;
  const path = `/2015-03-31/functions/${FUNC_NAME}/invocations`;
  const service = 'lambda';

  const signedHeadersList = ['host', 'x-amz-content-sha256', 'x-amz-date'];
  if (tok) signedHeadersList.push('x-amz-security-token');

  const headerMap = {
    'host': hostname, 'x-amz-content-sha256': payloadHash, 'x-amz-date': amzDate,
    ...(tok ? { 'x-amz-security-token': tok } : {}),
  };
  const canonicalHeaders = signedHeadersList.map(h => `${h}:${headerMap[h]}`).join('\n') + '\n';
  const signedHeaders = signedHeadersList.join(';');
  const canonicalRequest = ['POST', path, '', canonicalHeaders, signedHeaders, payloadHash].join('\n');
  const credentialScope = `${datestamp}/${REGION}/${service}/aws4_request`;
  const stringToSign = ['AWS4-HMAC-SHA256', amzDate, credentialScope,
    createHash('sha256').update(canonicalRequest).digest('hex')].join('\n');
  const signature = createHmac('sha256', getSigningKey(service, sk, datestamp)).update(stringToSign).digest('hex');
  const authorization = `AWS4-HMAC-SHA256 Credential=${ak}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;

  return new Promise((resolve) => {
    const req = httpsRequest({
      hostname, path, method: 'POST',
      headers: {
        'host': hostname, 'x-amz-date': amzDate, 'x-amz-content-sha256': payloadHash,
        'authorization': authorization, 'content-type': 'application/json',
        ...(tok ? { 'x-amz-security-token': tok } : {}),
      },
    }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, buf: Buffer.concat(chunks) }));
    });
    req.on('error', (e) => resolve({ status: 500, buf: Buffer.from(`{"error":"${e.message}"}`) }));
    req.write(body);
    req.end();
  });
}

function extractSseBody(buf) {
  // The awslambda.HttpResponseStream format:
  // [prelude JSON]\x00...\x00[SSE body]
  // Find the end of the prelude JSON and skip the null-byte delimiter.
  const str = buf.toString('utf-8');
  const preludeEnd = str.indexOf('}}');
  if (preludeEnd === -1) return str;
  // Skip past the prelude JSON and any trailing null/whitespace bytes
  let bodyStart = preludeEnd + 2;
  while (bodyStart < str.length && (str.charCodeAt(bodyStart) === 0 || str[bodyStart] === '\0')) {
    bodyStart++;
  }
  // Return just the SSE data portion
  return str.slice(bodyStart);
}

export const handler = async (event) => {
  const request = event.Records[0].cf.request;

  // Decode the viewer's request body
  let viewerBody = Buffer.alloc(0);
  if (request.body?.data) {
    viewerBody = request.body.encoding === 'base64'
      ? Buffer.from(request.body.data, 'base64')
      : Buffer.from(request.body.data, 'utf-8');
  }

  const ak  = process.env.AWS_ACCESS_KEY_ID;
  const sk  = process.env.AWS_SECRET_ACCESS_KEY;
  const tok = process.env.AWS_SESSION_TOKEN;

  // Wrap in the format rag-node expects when called via Lambda URL
  // (event.body is the raw HTTP body string)
  const lambdaEvent = { body: viewerBody.toString('utf-8') };
  const invokePayload = Buffer.from(JSON.stringify(lambdaEvent));

  const { status, buf } = await invokeLambda(invokePayload, ak, sk, tok);
  console.log('DBG invoke status:', status, 'buf len:', buf.length);

  if (status !== 200) {
    return {
      status: String(status),
      statusDescription: 'Error',
      headers: { 'content-type': [{ key: 'Content-Type', value: 'application/json' }] },
      body: buf.toString('utf-8', 0, 500),
    };
  }

  const sseBody = extractSseBody(buf);
  return {
    status: '200',
    statusDescription: 'OK',
    headers: {
      'content-type': [{ key: 'Content-Type', value: 'text/event-stream' }],
      'cache-control': [{ key: 'Cache-Control', value: 'no-cache' }],
    },
    body: sseBody,
  };
};
