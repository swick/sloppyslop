"""LLM provider integration with async-only interface."""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Union

from anthropic import AsyncAnthropic, Anthropic, AnthropicVertex

from llm_sandbox.config import AnthropicConfig, VertexAIConfig
from llm_sandbox.events import EventEmitter

if TYPE_CHECKING:
    from llm_sandbox.runner import Agent


# LLMProvider event types
@dataclass
class LLMIterationStarted:
    """Event: LLM iteration started."""

    iteration: int
    max_iterations: int


@dataclass
class LLMMessageSent:
    """Event: Message sent to LLM."""

    role: str
    content_preview: str
    tool_uses: List[str]


@dataclass
class LLMResponseReceived:
    """Event: Response received from LLM."""

    stop_reason: str
    usage: dict


@dataclass
class LLMToolsExecuting:
    """Event: Executing multiple tools concurrently."""

    tool_count: int
    tool_names: List[str]


@dataclass
class LLMToolCompleted:
    """Event: Tool execution completed."""

    tool_name: str
    success: bool


@dataclass
class LLMJSONParseError:
    """Event: Failed to parse JSON from LLM response."""

    error: str
    json_text: str


def extract_json_from_text(text: str) -> str:
    """
    Extract JSON from text that may contain markdown code blocks or extra content.

    Handles common patterns:
    - ```json\\n{...}\\n```
    - ```\\n{...}\\n```
    - {JSON} with surrounding text

    Args:
        text: Text that may contain JSON

    Returns:
        Extracted JSON string
    """
    # First, try to find JSON in markdown code blocks
    json_block_pattern = r'```(?:json)?\s*(.*)```'
    match = re.search(json_block_pattern, text, re.DOTALL)
    if match:
        extracted = match.group(1).strip()
        if extracted.startswith('{') or extracted.startswith('['):
            return extracted

    # Try to find JSON object boundaries
    first_brace = text.find('{')
    last_brace = text.rfind('}')

    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]

    # Also try array syntax
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')

    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        return text[first_bracket:last_bracket + 1]

    return text.strip()


class LLMProvider(ABC):
    """Base class for LLM providers with async-only interface."""

    def __init__(self):
        """Initialize LLM provider with conversation tracking."""
        self.base_system_prompt = ""
        self.conversation_history: List[Dict[str, Any]] = []
        self.verbose: bool = False

    def set_system_prompt(self, prompt: str) -> None:
        """
        Set the base system prompt.

        Args:
            prompt: Base system prompt describing the environment and capabilities
        """
        self.base_system_prompt = prompt

    def _generate_tools_description(self, mcp_server: MCPServer) -> str:
        """Generate a description of available tools from the MCP server."""
        tools = mcp_server.get_tools()

        if not tools:
            return "No tools available."

        lines = ["Available tools:", ""]
        for tool in tools:
            lines.append(f"- {tool.name}: {tool.description}")

        return "\n".join(lines)

    def _build_system_prompt(self, mcp_server: MCPServer, output_schema: Dict[str, Any] = None) -> str:
        """Build the complete system prompt including base prompt and tool descriptions."""
        parts = [self.base_system_prompt, "", self._generate_tools_description(mcp_server)]
        return "\n\n".join(parts)

    def _clear_conversation(self):
        """Clear conversation history."""
        self.conversation_history = []

    def _add_user_message(self, content: Any):
        """Add user message to conversation history."""
        self.conversation_history.append({"role": "user", "content": content})

    def _add_assistant_message(self, content: Any):
        """Add assistant message to conversation history."""
        self.conversation_history.append({"role": "assistant", "content": content})

    def _log_message(self, prefix: str, message: Dict[str, Any]):
        """Log a message if verbose mode is enabled.

        Note: Verbose message logging now handled by event system.
        This method kept for compatibility but does nothing.
        """
        # Verbose logging now handled by events in CLI layer
        pass

    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        agent: "Agent",
        output_schema: Dict[str, Any],
        events: EventEmitter,
        max_iterations: int = 25,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Args:
            prompt: User prompt
            agent: Agent instance (provides mcp_server for tool execution)
            output_schema: JSON schema for structured output
            events: EventEmitter for emitting LLM events
            max_iterations: Maximum tool use iterations
            verbose: Enable verbose output

        Returns:
            Structured output matching schema
        """
        pass

    @abstractmethod
    async def validate(self) -> Dict[str, Any]:
        """
        Validate that the provider is configured correctly and can connect.

        Returns:
            Dictionary with validation results
        """
        pass


class ClaudeProvider(LLMProvider):
    """Claude API provider with async-only interface.

    Supports both Anthropic's direct API and Google Cloud Vertex AI.
    Vertex AI uses asyncio.to_thread for async methods since no native async client exists.
    """

    def __init__(self, provider_config: Union[AnthropicConfig, VertexAIConfig]):
        """
        Initialize Claude provider with async client.

        Args:
            provider_config: Provider configuration (AnthropicConfig or VertexAIConfig)

        Raises:
            ValueError: If required configuration is missing
        """
        super().__init__()
        import os

        self.model = provider_config.model
        self.provider_config = provider_config

        if isinstance(provider_config, VertexAIConfig):
            # Vertex AI backend
            self.backend = "vertex-ai"

            if not provider_config.region:
                raise ValueError(
                    "Vertex AI backend requires 'region' configuration (e.g., 'us-east5')"
                )
            if not provider_config.project_id:
                raise ValueError(
                    "Vertex AI backend requires 'project_id' configuration (GCP project ID)"
                )

            # Vertex AI only has sync client, will use asyncio.to_thread for async methods
            self.sync_client = AnthropicVertex(
                region=provider_config.region,
                project_id=provider_config.project_id,
            )
            self.client = None  # No async client for Vertex AI

        elif isinstance(provider_config, AnthropicConfig):
            # Direct Anthropic API backend
            self.backend = "anthropic"

            api_key = os.getenv(provider_config.api_key_env)
            if not api_key:
                raise ValueError(
                    f"API key not found. Set {provider_config.api_key_env} environment variable."
                )

            self.api_key = api_key
            # Use both sync and async clients for Anthropic
            self.sync_client = Anthropic(api_key=api_key)
            self.client = AsyncAnthropic(api_key=api_key)

        else:
            raise ValueError(f"Unknown provider config type: {type(provider_config)}")

    async def generate_structured(
        self,
        prompt: str,
        agent: "Agent",
        output_schema: Dict[str, Any],
        events: EventEmitter,
        max_iterations: int = 200,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Executes tools concurrently when Claude requests multiple tools.

        Args:
            prompt: User prompt
            agent: Agent instance (provides mcp_server for tool execution)
            events: EventEmitter for emitting LLM events
            output_schema: JSON schema for structured output
            max_iterations: Maximum tool use iterations
            verbose: Enable verbose output

        Returns:
            Structured output matching schema
        """
        # Set verbose mode and clear conversation history
        self.verbose = verbose
        self._clear_conversation()

        # Get MCP server from agent
        mcp_server = agent.mcp_server

        # Get available tools
        tools = mcp_server.get_tools()
        tool_defs = [tool.to_dict() for tool in tools]

        # Build system prompt
        base_prompt = self._build_system_prompt(mcp_server, output_schema)

        # For Vertex AI, add schema to system prompt (no native structured output support)
        if self.backend == "vertex-ai":
            system_prompt = f"""{base_prompt}

When you're done analyzing, provide your final answer as a JSON object matching this exact schema:

{json.dumps(output_schema, indent=2)}

Return ONLY the JSON object, no other text."""
        else:
            # For Anthropic API, use native structured output
            system_prompt = f"""{base_prompt}

When you're done analyzing, provide your final answer in the structured JSON format."""

        # Add initial user message
        self._add_user_message(prompt)

        # Verbose logging handled by event handlers in CLI layer

        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            events.emit(LLMIterationStarted(
                iteration=iteration,
                max_iterations=max_iterations
            ))

            # Make API call
            if self.backend == "vertex-ai":
                # Vertex AI - use sync client with asyncio.to_thread
                # No structured output support, rely on prompt engineering
                response = await asyncio.to_thread(
                    self.sync_client.messages.create,
                    model=self.model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=self.conversation_history,
                    tools=tool_defs,
                )
            else:
                # Anthropic API - use async client with native structured output
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=self.conversation_history,
                    tools=tool_defs,
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": output_schema,
                        }
                    },
                )

            # Add assistant response to conversation
            self._add_assistant_message(response.content)

            events.emit(LLMResponseReceived(
                stop_reason=response.stop_reason,
                usage=response.usage.__dict__ if hasattr(response, 'usage') else {}
            ))

            # Log assistant message
            self._log_message("→", self.conversation_history[-1])

            # Check if we have a final answer
            if response.stop_reason == "end_turn":
                for block in response.content:
                    if block.type == "text":
                        text = block.text.strip()

                        # For Vertex AI, extract JSON from response (handles markdown code blocks, etc.)
                        # Anthropic API with output_config should guarantee valid JSON
                        if self.backend == "vertex-ai":
                            json_text = extract_json_from_text(text)
                        else:
                            json_text = text

                        try:
                            result = json.loads(json_text)
                            return result
                        except json.JSONDecodeError as e:
                            events.emit(LLMJSONParseError(
                                error=str(e),
                                json_text=json_text
                            ))
                            # Error details in exception message for CLI layer to handle
                            error_msg = f"Failed to parse JSON from LLM response: {e}\n\nAttempted JSON:\n{json_text[:500]}"
                            if self.backend == "vertex-ai" and json_text != text:
                                error_msg += f"\n\nOriginal response:\n{text[:500]}"
                            raise RuntimeError(error_msg)

                raise RuntimeError("Response ended without valid JSON output")

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                tool_blocks = [b for b in response.content if b.type == "tool_use"]

                events.emit(LLMToolsExecuting(
                    tool_count=len(tool_blocks),
                    tool_names=[b.name for b in tool_blocks]
                ))

                # Execute tools in parallel
                tool_results_data = await asyncio.gather(*[
                    self._execute_single_tool(agent, block)
                    for block in tool_blocks
                ], return_exceptions=True)

                # Build tool results for conversation
                tool_results = []
                for block, result_data in zip(tool_blocks, tool_results_data):
                    if isinstance(result_data, Exception):
                        events.emit(LLMToolCompleted(
                            tool_name=block.name,
                            success=False
                        ))
                        result = {"success": False, "error": str(result_data)}
                    else:
                        events.emit(LLMToolCompleted(
                            tool_name=block.name,
                            success=True
                        ))
                        result = result_data

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

                # Add tool results to conversation
                self._add_user_message(tool_results)
                self._log_message("←", self.conversation_history[-1])

                continue

            break

        raise RuntimeError(
            f"Failed to generate structured output after {max_iterations} iterations"
        )

    async def _execute_single_tool(self, agent: "Agent", block) -> Dict[str, Any]:
        """Execute a single tool."""
        result = await agent.mcp_server.execute_tool(block.name, block.input)
        return result

    async def validate(self) -> Dict[str, Any]:
        """Validate that the provider is configured correctly and can connect."""
        try:
            if self.client:
                # Anthropic async client
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "test"}],
                )
            else:
                # Vertex AI - use sync client with asyncio.to_thread
                response = await asyncio.to_thread(
                    self.sync_client.messages.create,
                    model=self.model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "test"}],
                )

            return {
                "success": True,
                "message": f"Successfully connected to {self.backend} backend",
                "details": {
                    "backend": self.backend,
                    "model": self.model,
                    "response_id": response.id,
                },
            }

        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)

            details = {
                "backend": self.backend,
                "model": self.model,
                "error_type": error_type,
                "error_message": error_message,
            }

            if "authentication" in error_message.lower() or "api key" in error_message.lower():
                guidance = "Check your API key or authentication credentials"
            elif "not found" in error_message.lower() or "404" in error_message:
                guidance = "Model may not be available or incorrectly specified"
            elif "permission" in error_message.lower() or "403" in error_message:
                guidance = "Check your permissions or project access"
            elif "network" in error_message.lower() or "connection" in error_message.lower():
                guidance = "Check your network connection"
            else:
                guidance = "See error details for more information"

            details["guidance"] = guidance

            return {
                "success": False,
                "message": f"Failed to connect to {self.backend} backend: {error_type}",
                "details": details,
            }


def create_llm_provider(
    provider_name: str,
    provider_config: Union[AnthropicConfig, VertexAIConfig],
) -> LLMProvider:
    """
    Create an LLM provider instance.

    Args:
        provider_name: Name of the provider (anthropic, vertex-ai)
        provider_config: Provider configuration

    Returns:
        LLM provider instance with async interface

    Raises:
        ValueError: If provider is not supported
    """
    if provider_name in ("anthropic", "vertex-ai"):
        return ClaudeProvider(provider_config)
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider_name}. "
            f"Supported providers: anthropic, vertex-ai"
        )
