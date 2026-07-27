import { BedrockAgentRuntimeClient, RetrieveCommand } from "@aws-sdk/client-bedrock-agent-runtime";
import { BedrockRuntimeClient, ConverseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { S3Client, GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";

const KB_ID = process.env.KNOWLEDGE_BASE_ID;
const LOG_BUCKET = process.env.DATA_BUCKET;
const MODEL_ID = "us.anthropic.claude-sonnet-4-6";

const s3 = new S3Client({});
const bedrockAgent = new BedrockAgentRuntimeClient({});
const bedrockRuntime = new BedrockRuntimeClient({});

// Cold-start: load system prompt from S3
const { Body } = await s3.send(new GetObjectCommand({ Bucket: LOG_BUCKET, Key: "system_prompt.txt" }));
const SYSTEM_PROMPT = await Body.transformToString();

function getSourceMetadata(result) {
  const meta = result.metadata || {};
  const memberUrl = meta["memberUrl"];
  if (!memberUrl) return null;
  return {
    memberUrl,
    productUrl: meta["productUrl"] || "",
    title: meta["title"] || "",
    productType: meta["productType"] || "",
    airDate: meta["airDate"] || "",
    speaker: meta["speaker"] || "",
    productImage: meta["productImage"] || "",
  };
}

export const handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
  const httpStream = awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });

  const send = (data) => httpStream.write(`data: ${JSON.stringify(data)}\n\n`);

  const body = JSON.parse(event.body || "{}");
  const userQuery = body.question || "";
  let streamedText = "";

  try {
    // Step 1: Retrieve KB chunks (~1s, all at once)
    const retrieveResponse = await bedrockAgent.send(new RetrieveCommand({
      knowledgeBaseId: KB_ID,
      retrievalQuery: { text: userQuery },
    }));

    const retrievalResults = retrieveResponse.retrievalResults || [];

    // Build deduplicated source list and numbered context for the prompt
    const urlToNum = {};
    const uniqueSources = [];

    const contextLines = retrievalResults.map((result) => {
      const source = getSourceMetadata(result);
      let num;
      if (source) {
        const { memberUrl } = source;
        if (!(memberUrl in urlToNum)) {
          uniqueSources.push(source);
          urlToNum[memberUrl] = uniqueSources.length;
        }
        num = urlToNum[memberUrl];
      } else {
        num = "?";
      }
      return `[${num}] ${result.content?.text || ""}`;
    });

    const contextText = contextLines.join("\n\n");

    // Step 2: Stream tokens via ConverseStream (true progressive streaming)
    const converseResponse = await bedrockRuntime.send(new ConverseStreamCommand({
      modelId: MODEL_ID,
      system: [{ text: SYSTEM_PROMPT }],
      messages: [{
        role: "user",
        content: [{
          text: `Answer the question using ONLY the sources below. Cite sources inline with [n] markers matching the source numbers.\n\nSources:\n${contextText}\n\nQuestion: ${userQuery}`,
        }],
      }],
    }));

    for await (const streamEvent of converseResponse.stream) {
      if (streamEvent.contentBlockDelta?.delta?.text) {
        const text = streamEvent.contentBlockDelta.delta.text;
        send({ type: "chunk", text });
        streamedText += text;
      }
    }

    send({ type: "done", answer: streamedText, sources: uniqueSources });

    // Best-effort S3 audit log
    try {
      const now = new Date();
      const datePath = now.toISOString().slice(0, 10).replace(/-/g, "/");
      await s3.send(new PutObjectCommand({
        Bucket: LOG_BUCKET,
        Key: `logs/${datePath}/${context.awsRequestId}.json`,
        Body: JSON.stringify({
          timestamp: now.toISOString(),
          requestId: context.awsRequestId,
          question: userQuery,
          answer: streamedText,
          sources: uniqueSources,
        }),
        ContentType: "application/json",
      }));
    } catch (e) {
      console.error("S3 log failed:", e.message);
    }
  } catch (err) {
    console.error("Error:", err);
    send({ type: "error", message: err.message });
  } finally {
    httpStream.end();
  }
});
