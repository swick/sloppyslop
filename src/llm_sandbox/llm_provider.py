"""LLM provider integration."""

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Union

from anthropic import Anthropic, AnthropicVertex

from llm_sandbox.config import AnthropicConfig, VertexAIConfig
from llm_sandbox.mcp_tools import MCPServer


def extract_json_from_text(text: str) -> str:
    """
    Extract JSON from text that may contain markdown code blocks or extra content.

    Args:
        text: Text that may contain JSON

    Returns:
        Extracted JSON string
    """
    # First, try to find JSON in markdown code blocks
    json_block_pattern = r'```(?:json)?\s*\n(.*?)\n```'
    match = re.search(json_block_pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try to find JSON object boundaries
    # Look for content between first { and last }
    first_brace = text.find('{')
    last_brace = text.rfind('}')

    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]

    # Return as-is if no extraction patterns match
    return text.strip()


class LLMProvider(ABC):
    """Base class for LLM providers."""

    @abstractmethod
    def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
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
    def generate_structured(
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
    def validate(self) -> Dict[str, Any]:
        """
        Validate that the provider is configured correctly and can connect.

        Makes a minimal API call to verify:
        - Credentials are valid
        - API endpoint is reachable
        - Model is accessible

        Returns:
            Dictionary with validation results:
            {
                "success": bool,
                "message": str,
                "details": dict (optional error details)
            }

        Note: This method should not raise exceptions, but return error info in the dict.
        """
        pass


class ClaudeProvider(LLMProvider):
    """Claude API provider with MCP tool support and structured output.

    Supports both Anthropic's direct API and Google Cloud Vertex AI.
    """

    def __init__(self, provider_config: Union[AnthropicConfig, VertexAIConfig]):
        """
        Initialize Claude provider.

        Args:
            provider_config: Provider configuration (AnthropicConfig or VertexAIConfig)

        Raises:
            ValueError: If required configuration is missing
        """
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

            self.client = AnthropicVertex(
                region=provider_config.region,
                project_id=provider_config.project_id,
            )

        elif isinstance(provider_config, AnthropicConfig):
            # Direct Anthropic API backend
            self.backend = "anthropic"

            api_key = os.getenv(provider_config.api_key_env)
            if not api_key:
                raise ValueError(
                    f"API key not found. Set {provider_config.api_key_env} environment variable."
                )

            self.api_key = api_key
            self.client = Anthropic(api_key=api_key)

        else:
            raise ValueError(f"Unknown provider config type: {type(provider_config)}")

    def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
        """
        Generate plain text response.

        Args:
            prompt: User prompt
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        )

        # Extract text from response
        text = response.content[0].text.strip()
        return text

    def generate_structured(
        self,
        prompt: str,
        mcp_server: MCPServer,
        output_schema: Dict[str, Any],
        max_iterations: int = 200,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Uses Claude's native JSON schema output format.

        Args:
            prompt: User prompt
            mcp_server: MCP server instance for tool execution
            output_schema: JSON schema for structured output
            max_iterations: Maximum tool use iterations
            verbose: Enable verbose output (tool calls and messages)

        Returns:
            Structured output matching schema
        """
        # Get available tools
        tools = mcp_server.get_tools()
        tool_defs = [tool.to_dict() for tool in tools]

        # Build system prompt
        base_system_prompt = """You are working in an isolated container environment. You have access to tools for executing commands and git operations.

The container has two mounts:
- /project (read-only): The original project code
- /worktrees (read-write): A folder in which you can checkout specific commits/branches in sub-directories, using the checkout_commit tool

Your task is to analyze the project and provide the requested information.

Use the tools available to explore the project, run commands, and gather information as needed."""

        # For Vertex AI, add schema to system prompt (no native structured output support)
        if self.backend == "vertex-ai":
            system_prompt = f"""{base_system_prompt}

When you're done analyzing, provide your final answer as a JSON object matching this exact schema:

{json.dumps(output_schema, indent=2)}

Return ONLY the JSON object, no other text."""
        else:
            # For Anthropic API, use native structured output
            system_prompt = f"""{base_system_prompt}

When you're done analyzing, provide your final answer in the structured JSON format."""

        messages = [{"role": "user", "content": prompt}]

        if verbose:
            print(f"\n{'='*60}")
            print(f"Initial user prompt:")
            print(f"{'='*60}")
            # Truncate if very long
            if len(prompt) > 500:
                print(f"{prompt[:500]}...")
                print(f"[{len(prompt)} characters total]")
            else:
                print(prompt)
            print(f"{'='*60}")

        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            if verbose:
                print(f"\n{'='*60}")
                print(f"Iteration {iteration}/{max_iterations}")
                print(f"{'='*60}")

            # Make API call
            if self.backend == "vertex-ai":
                # Vertex AI doesn't support structured output, rely on prompt engineering
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=messages,
                    tools=tool_defs,
                )
            else:
                # Anthropic API uses native structured output
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=messages,
                    tools=tool_defs,
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": output_schema,
                        }
                    },
                )

            # Add assistant response to messages
            assistant_message = {
                "role": "assistant",
                "content": response.content,
            }
            messages.append(assistant_message)

            if verbose:
                print(f"\nResponse stop reason: {response.stop_reason}")
                # Count content blocks by type
                text_blocks = sum(1 for b in response.content if b.type == "text")
                tool_blocks = sum(1 for b in response.content if b.type == "tool_use")
                print(f"Response content: {text_blocks} text block(s), {tool_blocks} tool use block(s)")
                print(f"\n→ Assistant message:")
                print(f"{'-'*60}")
                # Print each content block
                for block in response.content:
                    if block.type == "text":
                        print(f"[Text block]")
                        if len(block.text) > 500:
                            print(f"{block.text[:500]}...")
                            print(f"[{len(block.text)} characters total]")
                        else:
                            print(block.text)
                    elif block.type == "tool_use":
                        print(f"[Tool use: {block.name}]")
                        print(f"ID: {block.id}")
                        print(f"Input: {json.dumps(block.input, indent=2)}")
                    print()
                print(f"{'-'*60}")
                print(f"Total messages in conversation: {len(messages)}")

            # Check if we have a final answer (text response with JSON)
            if response.stop_reason == "end_turn":
                # Extract JSON from response
                for block in response.content:
                    if block.type == "text":
                        text = block.text.strip()

                        if verbose:
                            print(f"\nAssistant response (text):")
                            print(f"{'-'*60}")
                            # Truncate if very long
                            if len(text) > 500:
                                print(f"{text[:500]}...")
                                print(f"[{len(text)} characters total]")
                            else:
                                print(text)
                            print(f"{'-'*60}")

                        # For Vertex AI, sanitize the response to extract JSON
                        if self.backend == "vertex-ai":
                            json_text = extract_json_from_text(text)
                        else:
                            # Anthropic API with output_config guarantees valid JSON
                            json_text = text

                        try:
                            result = json.loads(json_text)
                            if verbose:
                                print(f"\n✓ Successfully parsed JSON output")
                            return result
                        except json.JSONDecodeError as e:
                            # Print detailed error for debugging
                            print(f"\n{'='*60}")
                            print(f"ERROR: Failed to parse JSON from LLM response")
                            print(f"{'='*60}")
                            print(f"Error: {e}")
                            print(f"\nAttempted to parse:")
                            print(f"{'-'*60}")
                            print(json_text)
                            print(f"{'-'*60}")
                            if self.backend == "vertex-ai" and json_text != text:
                                print(f"\nOriginal response:")
                                print(f"{'-'*60}")
                                print(text)
                                print(f"{'-'*60}")
                            print()
                            raise RuntimeError(
                                f"Failed to parse JSON from LLM response. "
                                f"See output above for details."
                            )

                # If we get here without valid JSON, something is wrong
                raise RuntimeError("Response ended without valid JSON output")

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                # Execute all tool calls
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        if verbose:
                            print(f"\n→ Tool call: {block.name}")
                            print(f"  Input: {json.dumps(block.input, indent=2)}")

                        # Execute tool
                        result = mcp_server.execute_tool(
                            block.name,
                            block.input,
                        )

                        if verbose:
                            print(f"← Tool result:")
                            result_str = json.dumps(result, indent=2)
                            # Truncate if very long
                            if len(result_str) > 500:
                                print(f"  {result_str[:500]}...")
                                print(f"  [{len(result_str)} characters total]")
                            else:
                                print(f"  {result_str}")

                        # Add result
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })

                # Add tool results to messages
                tool_results_message = {
                    "role": "user",
                    "content": tool_results,
                }
                messages.append(tool_results_message)

                if verbose:
                    print(f"\n→ Tool results message:")
                    print(f"{'-'*60}")
                    for tr in tool_results:
                        print(f"[Tool result for: {tr['tool_use_id']}]")
                        content = tr['content']
                        if len(content) > 500:
                            print(f"{content[:500]}...")
                            print(f"[{len(content)} characters total]")
                        else:
                            print(content)
                        print()
                    print(f"{'-'*60}")
                    print(f"Total messages in conversation: {len(messages)}")

                continue

            # If we get here, something unexpected happened
            break

        raise RuntimeError(
            f"Failed to generate structured output after {max_iterations} iterations"
        )

    def validate(self) -> Dict[str, Any]:
        """
        Validate that the provider is configured correctly and can connect.

        Makes a minimal API call to verify credentials and connectivity.

        Returns:
            Dictionary with validation results
        """
        try:
            # Make a minimal API call to test connectivity
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1,  # Minimal token usage
                messages=[
                    {
                        "role": "user",
                        "content": "test",
                    }
                ],
            )

            # If we get here, the API call succeeded
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
            # Categorize the error
            error_type = type(e).__name__
            error_message = str(e)

            details = {
                "backend": self.backend,
                "model": self.model,
                "error_type": error_type,
                "error_message": error_message,
            }

            # Add specific guidance based on error type
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


def create_llm_provider(provider_name: str, provider_config: Union[AnthropicConfig, VertexAIConfig]) -> LLMProvider:
    if provider_name in ("anthropic", "vertex-ai"):
        return ClaudeProvider(provider_config)
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider_name}. "
            f"Supported providers: anthropic, vertex-ai"
        )
