# Copyright (c) Microsoft. All rights reserved.

"""Reliable streaming for durable agents using Redis Streams.

This sample demonstrates how to implement reliable streaming for durable agents using Redis Streams.

Components used in this sample:
- AzureOpenAIChatClient to create the travel planner agent with tools.
- AgentFunctionApp with a Redis-based callback for persistent streaming.
- Custom HTTP endpoint to resume streaming from any point using cursor-based pagination.

Prerequisites:
- Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_CHAT_DEPLOYMENT_NAME
- Redis running (docker run -d --name redis -p 6379:6379 redis:latest)
- DTS and Azurite running (see parent README)
"""

import logging
import os
from datetime import timedelta
import json
from collections.abc import Mapping
from pydantic import BaseModel, ValidationError
import azure.functions as func
import redis.asyncio as aioredis
from agent_framework import AgentResponseUpdate,MCPStreamableHTTPTool
from agent_framework.azure import (
    AgentCallbackContext,
    AgentFunctionApp,
    AgentResponseCallbackProtocol,
    AzureOpenAIChatClient,
)
from typing import Any
from azure.durable_functions import DurableOrchestrationClient, DurableOrchestrationContext
from azurefunctions.extensions.http.fastapi import Request, StreamingResponse,JSONResponse

from azure.identity import AzureCliCredential
from redis_stream_response_handler import RedisStreamResponseHandler, StreamChunk
from httpx import AsyncClient

logger = logging.getLogger(__name__)

# Configuration
REDIS_CONNECTION_STRING = os.environ.get("REDIS_CONNECTION_STRING", "redis://localhost:6379")
REDIS_STREAM_TTL_MINUTES = int(os.environ.get("REDIS_STREAM_TTL_MINUTES", "10"))


async def get_stream_handler() -> RedisStreamResponseHandler:
    """Create a new Redis stream handler for each request.

    This avoids event loop conflicts in Azure Functions by creating
    a fresh Redis client in the current event loop context.
    """
    # Create a new Redis client in the current event loop
    redis_client = aioredis.from_url(
        REDIS_CONNECTION_STRING,
        encoding="utf-8",
        decode_responses=True,
    )

    return RedisStreamResponseHandler(
        redis_client=redis_client,
        stream_ttl=timedelta(minutes=REDIS_STREAM_TTL_MINUTES),
    )


class RedisStreamCallback(AgentResponseCallbackProtocol):
    """Callback that writes streaming updates to Redis Streams for reliable delivery.

    This enables clients to disconnect and reconnect without losing messages.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("durableagent.samples.redis_streaming")
        self._sequence_numbers = {}  # Track sequence per thread

    async def on_streaming_response_update(
        self,
        update: AgentResponseUpdate,
        context: AgentCallbackContext,
    ) -> None:
        """Write streaming update to Redis Stream.

        Args:
            update: The streaming response update chunk.
            context: The callback context with thread_id, agent_name, etc.
        """
        thread_id = context.thread_id
        if not thread_id:
            self._logger.warning("No thread_id available for streaming update")
            return

        if not update.text:
            return

        text = update.text

        # Get or initialize sequence number for this thread
        if thread_id not in self._sequence_numbers:
            self._sequence_numbers[thread_id] = 0

        sequence = self._sequence_numbers[thread_id]

        try:
            # Use context manager to ensure Redis client is properly closed
            async with await get_stream_handler() as stream_handler:
                # Write chunk to Redis Stream using public API
                await stream_handler.write_chunk(thread_id, text, sequence)

                self._sequence_numbers[thread_id] += 1

                self._logger.info(
                    "[%s][%s] Wrote chunk to Redis: seq=%d, text=%s",
                    context.agent_name,
                    thread_id[:8],
                    sequence,
                    text,
                )
        except Exception as ex:
            self._logger.error(f"Error writing to Redis stream: {ex}", exc_info=True)

    async def on_agent_response(self, response, context: AgentCallbackContext) -> None:
        """Write end-of-stream marker when agent completes.

        Args:
            response: The final agent response.
            context: The callback context.
        """
        thread_id = context.thread_id
        if not thread_id:
            return

        sequence = self._sequence_numbers.get(thread_id, 0)

        try:
            # Use context manager to ensure Redis client is properly closed
            async with await get_stream_handler() as stream_handler:
                # Write end-of-stream marker using public API
                await stream_handler.write_completion(thread_id, sequence)

                self._logger.info(
                    "[%s][%s] Agent completed, wrote end-of-stream marker",
                    context.agent_name,
                    thread_id[:8],
                )

                # Clean up sequence tracker
                self._sequence_numbers.pop(thread_id, None)
        except Exception as ex:
            self._logger.error(f"Error writing end-of-stream marker: {ex}", exc_info=True)


# Create the Redis streaming callback
redis_callback = RedisStreamCallback()

AGENT_NAME = "VideoScriptResearchAssistant"
HUMAN_APPROVAL_EVENT = "HumanApproval"
# Create the Video Script Research Assistant agent
def create_VideoScriptResearchAssistant_agent():
    """Create the Video Script Research Assistant agent with tools."""
    m_auth_headers = {
        "x-functions-key": str(os.environ.get("markitdownmcpazfunckey")),
    }

    # Create HTTP client with authentication headers
    m_http_client = AsyncClient(headers=m_auth_headers)
    
    markitdownmcp_server=MCPStreamableHTTPTool(
                name="markitdown", 
                url=f"{os.environ.get("markitdownmcpazfuncurl")}/mcp",
                http_client=m_http_client
                #headers ={"x-functions-key": os.environ.get("markitdownmcpazfunckey")},
            )
    a_auth_headers = {
        "x-functions-key": str(os.environ.get("arxivazfunckey")),
    }

    # Create HTTP client with authentication headers
    a_http_client = AsyncClient(headers=a_auth_headers)
    arxivmcp_server = MCPStreamableHTTPTool(
                    name="arxiv", 
                    url=f"{os.environ.get("arxivmcpazfuncurl")}/mcp",
                    http_client=a_http_client
                    #headers ={"x-functions-key": os.environ.get("arxivazfunckey")},
                )
    return AzureOpenAIChatClient().as_agent(
                name=AGENT_NAME,
                instructions="""You are a Video Script Research Assistant in an iterative workflow.

        Help brainstorm video concepts, angles, and approaches.  Gather relevant content using available tools (markitdown, arxiv). Extract key facts, examples, and visual opportunities with clear citations.

        Work iteratively:  ask questions, suggest directions, refine based on feedback, and dig deeper as needed.  Present findings organized and ready for the scriptwriter. 

        Balance creativity in ideation with rigor in research.""",
                tools=[markitdownmcp_server,arxivmcp_server],
                response_format=GeneratedContent
            )

class ContentGenerationInput(BaseModel):
    topic: str
    max_review_attempts: int = 3
    approval_timeout_hours: float = 72

class GeneratedContent(BaseModel):
    """Structured content produced by the agent.

    This model represents the final, structured output generated by the agent
    (for example, the content that is surfaced for human review or returned
    to callers) in the HITL content-generation workflow.
    """
    title: str
    content: str

class HumanApproval(BaseModel):
    approved: bool
    feedback: str = ""

# Create AgentFunctionApp with the Redis callback
app = AgentFunctionApp(
    agents=[create_VideoScriptResearchAssistant_agent()],
    enable_health_check=False,
    default_callback=redis_callback,
    enable_http_endpoints=False,
    max_poll_retries=100,  # Increase for longer-running agents
)


@app.route(route="hitl/run", methods=[func.HttpMethod.POST])
@app.durable_client_input(client_name="client")
async def start_content_generation(
    req: Request,
    client: DurableOrchestrationClient,
) -> JSONResponse:
    """
    Start a human-in-the-loop (HITL) content generation orchestration.

    This endpoint starts the durable function orchestration
    ``content_generation_hitl_orchestration`` to generate content with
    human approval steps.

    Request body (JSON):
      {
        "topic": "<string, required>",
        "max_review_attempts": <int, optional, defaults to 3>,
        "approval_timeout_hours": <int or float, optional, defaults to 72>,
        ... // additional fields may be provided and are passed through to the orchestration
      }

    - ``topic``: The main topic or subject for which content should be generated.
    - ``max_review_attempts``: Maximum number of human review/approval cycles
      before the orchestration terminates. Optional; if omitted, defaults to 3.
    - ``approval_timeout_hours``: Number of hours to wait for human approval
      before timing out each review step. Optional; if omitted, defaults to 72.

    Responses:
      202 Accepted:
        {
          "message": "HITL content generation orchestration started.",
          "topic": "<string>",
          "instanceId": "<durable orchestration instance id>",
          "statusQueryGetUri": "<URL to query orchestration status>"
        }

      400 Bad Request:
        {
          "error": "<description of the validation or JSON parsing error>"
        }
    """
    try:
        body = await req.json()
    except ValueError:
        body = None

    if not isinstance(body, Mapping):
        return JSONResponse(
            {"error": "Request body must be valid JSON."},
            status_code=400
        )

    try:
        payload = ContentGenerationInput.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            {"error": f"Invalid content generation input: {exc}"},
            status_code=400
        )

    instance_id = await client.start_new(
        orchestration_function_name="content_generation_hitl_orchestration",
        client_input=payload.model_dump(),
    )

    status_url = _build_status_url(str(req.url), instance_id, route="hitl")

    payload_json = {
        "message": "HITL content generation orchestration started.",
        "topic": payload.topic,
        "conversation_id": instance_id,
        "statusQueryGetUri": status_url,
    }

    return JSONResponse(
        payload_json
    )


def _parse_human_approval(raw: Any) -> HumanApproval:
    if isinstance(raw, Mapping):
        return HumanApproval.model_validate(raw)

    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return HumanApproval(approved=False, feedback="")
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, Mapping):
                return HumanApproval.model_validate(parsed)
        except json.JSONDecodeError:
            logger.debug(
                "[HITL] Approval payload is not valid JSON; using string heuristics.",
                exc_info=True,
            )

        affirmative = {"true", "yes", "approved", "y", "1"}
        negative = {"false", "no", "rejected", "n", "0"}
        lower = stripped.lower()
        if lower in affirmative:
            return HumanApproval(approved=True, feedback="")
        if lower in negative:
            return HumanApproval(approved=False, feedback="")
        return HumanApproval(approved=False, feedback=stripped)

    raise ValueError("Approval payload must be a JSON object or string.")


@app.orchestration_trigger(context_name="context")
def content_generation_hitl_orchestration(context: DurableOrchestrationContext):
    payload_raw = context.get_input()
    if not isinstance(payload_raw, Mapping):
        raise ValueError("Content generation input is required")

    try:
        payload = ContentGenerationInput.model_validate(payload_raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid content generation input: {exc}") from exc
    instance_id = context.instance_id
    researcher = app.get_agent(context, AGENT_NAME)
    researcher_thread = researcher.get_new_thread(service_thread_id=instance_id)

    context.set_custom_status(f"Starting content generation for topic: {payload.topic}")

    initial_raw = yield researcher.run(
        messages=f"Think step by step starting with step 1:'{payload.topic}'.",
        thread=researcher_thread,
        options={"response_format": GeneratedContent},
    )

    content = initial_raw.try_parse_value(GeneratedContent)
    logger.info("Type of content after extraction: %s", type(content))

    if content is None:
        raise ValueError("Agent returned no content after extraction.")

    attempt = 0
    while attempt < payload.max_review_attempts:
        attempt += 1
        context.set_custom_status(
            f"Requesting human feedback. Iteration #{attempt}. Timeout: {payload.approval_timeout_hours} hour(s)."
        )

        yield context.call_activity("notify_user_for_approval", content.model_dump())

        approval_task = context.wait_for_external_event(HUMAN_APPROVAL_EVENT)
        timeout_task = context.create_timer(
            context.current_utc_datetime + timedelta(hours=payload.approval_timeout_hours)
        )

        winner = yield context.task_any([approval_task, timeout_task])

        if winner == approval_task:
            timeout_task.cancel()  # type: ignore[attr-defined]
            approval_payload = _parse_human_approval(approval_task.result)

            if approval_payload.approved:
                context.set_custom_status("Content approved by human reviewer. Publishing content...")
                yield context.call_activity("publish_content", content.model_dump())
                context.set_custom_status(
                    f"Content published successfully at {context.current_utc_datetime:%Y-%m-%dT%H:%M:%S}"
                )
                return {"content": content.content}

            context.set_custom_status("Content rejected by human reviewer. Incorporating feedback and regenerating...")
            rewrite_prompt = (
                "Ask the user if they approve the content or if they want more research.\n\n"
                f"Human Feedback: {approval_payload.feedback or 'No feedback provided.'}"
            )
            rewritten_raw = yield researcher.run(
                messages=rewrite_prompt,
                thread=researcher_thread,
                options={"response_format": GeneratedContent},
            )

            content = rewritten_raw.try_parse_value(GeneratedContent)
            if content is None:
                raise ValueError("Agent returned no content after rewrite.")
        else:
            context.set_custom_status(
                f"Human approval timed out after {payload.approval_timeout_hours} hour(s). Treating as rejection."
            )
            raise TimeoutError(f"Human approval timed out after {payload.approval_timeout_hours} hour(s).")

    raise RuntimeError(f"Content could not be approved after {payload.max_review_attempts} iteration(s).")


def _build_status_url(request_url: str, instance_id: str, *, route: str) -> str:
    base_url, _, _ = request_url.partition("/api/")
    if not base_url:
        base_url = request_url.rstrip("/")
    return f"{base_url}/api/{route}/status/{instance_id}"


# Custom streaming endpoint for reading from Redis
@app.function_name("stream")
@app.route(route="agent/stream/{conversation_id}", methods=[func.HttpMethod.GET])
async def stream(req: Request) -> StreamingResponse:
    """Resume streaming from a specific cursor position for an existing session.

    This endpoint reads all currently available chunks from Redis for the given
    conversation ID, starting from the specified cursor (or beginning if no cursor).

    Use this endpoint to resume a stream after disconnection. Pass the conversation ID
    and optionally a cursor (Redis entry ID) to continue from where you left off.

    Query Parameters:
        cursor (optional): Redis stream entry ID to resume from. If not provided, starts from beginning.

    Response Headers:
        Content-Type: text/event-stream or text/plain based on Accept header
        x-conversation-id: The conversation/thread ID

    SSE Event Fields (when Accept: text/event-stream):
        id: Redis stream entry ID (use as cursor for resumption)
        event: "message" for content, "done" for completion, "error" for errors
        data: The text content or status message
    """
    try:
        conversation_id = req.path_params.get("conversation_id")
        if not conversation_id:
            return StreamingResponse(
                {"error": "Conversation ID is required. "},
                status_code=400
            )

        # Get optional cursor from query string
        cursor = req.query_params.get("cursor")

        logger.info(
            f"Resuming stream for conversation {conversation_id} from cursor: {cursor or '(beginning)'}"
        )

        # Check Accept header to determine response format
        accept_header = req.headers.get("Accept", "")
        use_sse_format = "text/plain" not in accept_header.lower()
        if use_sse_format:
            logger.info("Using SSE format for streaming response")
            return StreamingResponse(_streamsse_to_client(conversation_id, cursor, use_sse_format), media_type="text/event-stream",headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "x-conversation-id": conversation_id,
        })

       
    except Exception as ex:
        logger.error(f"Error in stream endpoint: {ex}", exc_info=True)
        return StreamingResponse(
            {"error": f"Internal server error: {str(ex)}"},
            status_code=500
        )

async def _streamsse_to_client(
    conversation_id: str,
    cursor: str | None,
    use_sse_format: bool,
) -> StreamingResponse:
    """Stream chunks from Redis to the HTTP response.

    Args:
        conversation_id: The conversation ID to stream from.
        cursor: Optional cursor to resume from. If None, streams from the beginning.
        use_sse_format: True to use SSE format, false for plain text.

    Returns:
        HTTP response with all currently available chunks.
    """
    
    # Use context manager to ensure Redis client is properly closed
    async with await get_stream_handler() as stream_handler:
        try:
            async for chunk in stream_handler.read_stream(conversation_id, cursor):
                if chunk.error:
                    logger.warning(f"Stream error for {conversation_id}: {chunk.error}")
                    yield _format_error(chunk.error, use_sse_format)
                    break
                   

                if chunk.is_done:
                    yield _format_end_of_stream(chunk.entry_id, use_sse_format)
                    break
                    

                if chunk.text:
                    yield _format_chunk(chunk, use_sse_format)

        except Exception as ex:
            logger.error(f"Error reading from Redis: {ex}", exc_info=True)
            yield _format_error(str(ex), use_sse_format)
            return



@app.activity_trigger(input_name="content")
def notify_user_for_approval(content: dict) -> None:
    model = GeneratedContent.model_validate(content)
    logger.info("NOTIFICATION: Please review the following content for approval:")
    logger.info("Title: %s", model.title or "(untitled)")
    logger.info("Content: %s", model.content)
    logger.info("Use the approval endpoint to approve or reject this content.")

@app.activity_trigger(input_name="content")
def publish_content(content: dict) -> None:
    model = GeneratedContent.model_validate(content)
    logger.info("PUBLISHING: Content has been published successfully:")
    logger.info("Title: %s", model.title or "(untitled)")
    logger.info("Content: %s", model.content)



def _format_chunk(chunk: StreamChunk, use_sse_format: bool) -> str:
    """Format a text chunk."""
    if use_sse_format:
        return _format_sse_event("message", chunk.text, chunk.entry_id)
    return chunk.text


def _format_end_of_stream(entry_id: str, use_sse_format: bool) -> str:
    """Format end-of-stream marker."""
    if use_sse_format:
        return _format_sse_event("done", "[DONE]", entry_id)
    return "\n"


def _format_error(error: str, use_sse_format: bool) -> str:
    """Format error message."""
    if use_sse_format:
        return _format_sse_event("error", error, None)
    return f"\n[Error: {error}]\n"


def _format_sse_event(event_type: str, data: str, event_id: str | None = None) -> str:
    """Format a Server-Sent Event."""
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    lines.append("")
    return "\n".join(lines) + "\n"
