"""LLM provider integration with async-only interface."""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Union

import click
from anthropic import AsyncAnthropic, Anthropic, AnthropicVertex

from llm_sandbox.config import AnthropicConfig, VertexAIConfig
from llm_sandbox.mcp_tools import MCPServer


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

    def __init__(self, base_system_prompt: str):
        """
        Initialize LLM provider with conversation tracking.

        Args:
            base_system_prompt: Base system prompt describing the environment and capabilities
        """
        self.base_system_prompt = base_system_prompt
        self.conversation_history: List[Dict[str, Any]] = []
        self.verbose: bool = False

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
        """Log a message if verbose mode is enabled."""
        if not self.verbose:
            return

        role = message.get("role", "unknown")
        content = message.get("content")

        click.echo(f"\n{prefix} {role.capitalize()} message:")
        click.echo(f"{'-'*60}")

        if isinstance(content, str):
            if len(content) > 500:
                click.echo(f"{content[:500]}...")
                click.echo(f"[{len(content)} characters total]")
            else:
                click.echo(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "unknown")
                    if block_type == "text":
                        text = block.get("text", "")
                        click.echo(f"[Text block]")
                        if len(text) > 500:
                            click.echo(f"{text[:500]}...")
                            click.echo(f"[{len(text)} characters total]")
                        else:
                            click.echo(text)
                    elif block_type == "tool_use":
                        click.echo(f"[Tool use: {block.get('name')}]")
                        click.echo(f"ID: {block.get('id')}")
                        click.echo(f"Input: {json.dumps(block.get('input', {}), indent=2)}")
                    elif block_type == "tool_result":
                        click.echo(f"[Tool result for: {block.get('tool_use_id')}]")
                        result_content = block.get("content", "")
                        if len(result_content) > 500:
                            click.echo(f"{result_content[:500]}...")
                            click.echo(f"[{len(result_content)} characters total]")
                        else:
                            click.echo(result_content)
                    else:
                        click.echo(f"[{block_type}]")
                        click.echo(json.dumps(block, indent=2))
                    click.echo()
                else:
                    # Handle anthropic content blocks (objects with .type attribute)
                    if hasattr(block, 'type'):
                        if block.type == "text":
                            click.echo(f"[Text block]")
                            if len(block.text) > 500:
                                click.echo(f"{block.text[:500]}...")
                                click.echo(f"[{len(block.text)} characters total]")
                            else:
                                click.echo(block.text)
                        elif block.type == "tool_use":
                            click.echo(f"[Tool use: {block.name}]")
                            click.echo(f"ID: {block.id}")
                            click.echo(f"Input: {json.dumps(block.input, indent=2)}")
                        click.echo()

        click.echo(f"{'-'*60}")

    @abstractmethod
    async def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
        """
        Generate plain text response.

        Args:
            prompt: User prompt
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        pass

    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        mcp_server: MCPServer,
        output_schema: Dict[str, Any],
        max_iterations: int = 25,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Args:
            prompt: User prompt
            mcp_server: MCP server instance for tool execution
            output_schema: JSON schema for structured output
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

    def __init__(self, provider_config: Union[AnthropicConfig, VertexAIConfig], base_system_prompt: str):
        """
        Initialize Claude provider with async client.

        Args:
            provider_config: Provider configuration (AnthropicConfig or VertexAIConfig)
            base_system_prompt: Base system prompt describing the environment

        Raises:
            ValueError: If required configuration is missing
        """
        super().__init__(base_system_prompt)
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

    async def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
        """Generate plain text response."""
        if self.client:
            # Anthropic async client
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        else:
            # Vertex AI - use sync client with asyncio.to_thread
            response = await asyncio.to_thread(
                self.sync_client.messages.create,
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        return response.content[0].text.strip()

    async def generate_structured(
        self,
        prompt: str,
        mcp_server: MCPServer,
        output_schema: Dict[str, Any],
        max_iterations: int = 200,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Executes tools concurrently when Claude requests multiple tools.

        Args:
            prompt: User prompt
            mcp_server: MCP server instance for tool execution
            output_schema: JSON schema for structured output
            max_iterations: Maximum tool use iterations
            verbose: Enable verbose output

        Returns:
            Structured output matching schema
        """
        # Set verbose mode and clear conversation history
        self.verbose = verbose
        self._clear_conversation()

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

        if self.verbose:
            click.echo(f"\n{'='*60}")
            click.echo(f"Initial user prompt:")
            click.echo(f"{'='*60}")
            if len(prompt) > 500:
                click.echo(f"{prompt[:500]}...")
                click.echo(f"[{len(prompt)} characters total]")
            else:
                click.echo(prompt)
            click.echo(f"{'='*60}")

        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            if self.verbose:
                click.echo(f"\n{'='*60}")
                click.echo(f"Iteration {iteration}/{max_iterations}")
                click.echo(f"{'='*60}")

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

            if self.verbose:
                click.echo(f"\nResponse stop reason: {response.stop_reason}")
                text_blocks = sum(1 for b in response.content if b.type == "text")
                tool_blocks = sum(1 for b in response.content if b.type == "tool_use")
                click.echo(f"Response content: {text_blocks} text block(s), {tool_blocks} tool use block(s)")

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
                            if self.verbose:
                                click.echo(f"\n✓ Successfully parsed JSON output")
                            return result
                        except json.JSONDecodeError as e:
                            click.echo(f"\n{'='*60}")
                            click.echo(f"ERROR: Failed to parse JSON from LLM response")
                            click.echo(f"{'='*60}")
                            click.echo(f"Error: {e}")
                            click.echo(f"\nAttempted to parse:")
                            click.echo(f"{'-'*60}")
                            click.echo(json_text)
                            click.echo(f"{'-'*60}")
                            if self.backend == "vertex-ai" and json_text != text:
                                click.echo(f"\nOriginal response:")
                                click.echo(f"{'-'*60}")
                                click.echo(text)
                                click.echo(f"{'-'*60}")
                            click.echo()
                            raise RuntimeError(
                                f"Failed to parse JSON from LLM response. "
                                f"See output above for details."
                            )

                raise RuntimeError("Response ended without valid JSON output")

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                tool_blocks = [b for b in response.content if b.type == "tool_use"]

                if self.verbose:
                    click.echo(f"\n→ Executing {len(tool_blocks)} tool(s) concurrently")

                # Execute tools in parallel
                tool_results_data = await asyncio.gather(*[
                    self._execute_single_tool(mcp_server, block)
                    for block in tool_blocks
                ], return_exceptions=True)

                # Build tool results for conversation
                tool_results = []
                for block, result_data in zip(tool_blocks, tool_results_data):
                    if isinstance(result_data, Exception):
                        if self.verbose:
                            click.echo(f"← Tool {block.name} failed: {result_data}")
                        result = {"success": False, "error": str(result_data)}
                    else:
                        result = result_data

                    if self.verbose:
                        click.echo(f"← Tool result for {block.name}:")
                        result_str = json.dumps(result, indent=2)
                        if len(result_str) > 500:
                            click.echo(f"  {result_str[:500]}...")
                            click.echo(f"  [{len(result_str)} characters total]")
                        else:
                            click.echo(f"  {result_str}")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

                # Add tool results to conversation
                self._add_user_message(tool_results)

                if self.verbose:
                    click.echo(f"\nTotal messages in conversation: {len(self.conversation_history)}")
                self._log_message("←", self.conversation_history[-1])

                continue

            break

        raise RuntimeError(
            f"Failed to generate structured output after {max_iterations} iterations"
        )

    async def _execute_single_tool(self, mcp_server, block) -> Dict[str, Any]:
        """Execute a single tool."""
        if self.verbose:
            click.echo(f"\n→ Tool call: {block.name}")
            click.echo(f"  Input: {json.dumps(block.input, indent=2)}")

        result = await mcp_server.execute_tool(block.name, block.input)
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
    base_system_prompt: str,
) -> LLMProvider:
    """
    Create an LLM provider instance.

    Args:
        provider_name: Name of the provider (anthropic, vertex-ai)
        provider_config: Provider configuration
        base_system_prompt: Base system prompt describing the environment

    Returns:
        LLM provider instance with async interface

    Raises:
        ValueError: If provider is not supported
    """
    if provider_name in ("anthropic", "vertex-ai"):
        return ClaudeProvider(provider_config, base_system_prompt)
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider_name}. "
            f"Supported providers: anthropic, vertex-ai"
        )
