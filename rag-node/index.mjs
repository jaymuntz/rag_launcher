import { BedrockAgentRuntimeClient, RetrieveCommand } from "@aws-sdk/client-bedrock-agent-runtime";
import { BedrockRuntimeClient, ConverseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

const KB_ID = process.env.KNOWLEDGE_BASE_ID;
const DATA_BUCKET = process.env.DATA_BUCKET;
const MODEL_ID = "us.anthropic.claude-sonnet-4-6";

const s3 = new S3Client({});
const bedrockAgent = new BedrockAgentRuntimeClient({});
const bedrockRuntime = new BedrockRuntimeClient({});

// Cold-start: load system prompt from S3
const { Body } = await s3.send(new GetObjectCommand({ Bucket: DATA_BUCKET, Key: "system_prompt.txt" }));
const SYSTEM_PROMPT = await Body.transformToString();

export const handler = awslambda.streamifyResponse(async (event, responseStream) => {
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
    const retrieveResponse = await bedrockAgent.send(new RetrieveCommand({
      knowledgeBaseId: KB_ID,
      retrievalQuery: { text: userQuery },
    }));

    const contextText = (retrieveResponse.retrievalResults || [])
      .map(r => r.content?.text || "")
      .join("\n\n");

    const converseResponse = await bedrockRuntime.send(new ConverseStreamCommand({
      modelId: MODEL_ID,
      system: [{ text: SYSTEM_PROMPT }],
      messages: [{
        role: "user",
        content: [{
          text: `Answer the question using ONLY the sources below.\n\nSources:\n${contextText}\n\nQuestion: ${userQuery}`,
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

    send({ type: "done", answer: streamedText });
  } catch (err) {
    console.error("Error:", err);
    send({ type: "error", message: err.message });
  } finally {
    httpStream.end();
  }
});
