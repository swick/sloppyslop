"""LLM provider integration."""

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from anthropic import Anthropic

from llm_sandbox.mcp_tools import MCPServer


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
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Args:
            prompt: User prompt
            mcp_server: MCP server instance for tool execution
            output_schema: JSON schema for structured output
            max_iterations: Maximum tool use iterations

        Returns:
            Structured output matching schema
        """
        pass


class ClaudeProvider(LLMProvider):
    """Claude API provider with MCP tool support and structured output."""

    def __init__(self, provider_config):
        """
        Initialize Claude provider.

        Args:
            provider_config: Provider configuration with api_key_env and model

        Raises:
            ValueError: If API key not found in environment
        """
        import os

        # Get API key from environment
        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            raise ValueError(
                f"API key not found. Set {provider_config.api_key_env} environment variable."
            )

        self.api_key = api_key
        self.model = provider_config.model
        self.provider_config = provider_config
        self.client = Anthropic(api_key=api_key)

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
        max_iterations: int = 25,
    ) -> Dict[str, Any]:
        """
        Generate structured output with MCP tool access.

        Uses Claude's native JSON schema output format.

        Args:
            prompt: User prompt
            mcp_server: MCP server instance for tool execution
            output_schema: JSON schema for structured output
            max_iterations: Maximum tool use iterations

        Returns:
            Structured output matching schema
        """
        # Get available tools
        tools = mcp_server.get_tools()
        tool_defs = [tool.to_dict() for tool in tools]

        # Build system prompt
        system_prompt = """You are working in an isolated container environment. You have access to tools for executing commands and git operations.

The container has two mounts:
- /project (read-only): The original project code
- /worktrees (read-write): A folder in which you can checkout specific commits/branches in sub-directories, using the checkout_commit tool

Your task is to analyze the project and provide the requested information.

Use the tools available to explore the project, run commands, and gather information as needed.
When you're done analyzing, provide your final answer in the structured JSON format."""

        messages = [{"role": "user", "content": prompt}]

        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            # Make API call with JSON schema output format
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system_prompt,
                messages=messages,
                tools=tool_defs,
                response_format={
                    "type": "json_schema",
                    "json_schema": output_schema,
                },
            )

            # Add assistant response to messages
            assistant_message = {
                "role": "assistant",
                "content": response.content,
            }
            messages.append(assistant_message)

            # Check if we have a final answer (text response with JSON)
            if response.stop_reason == "end_turn":
                # Extract JSON from response
                for block in response.content:
                    if block.type == "text":
                        text = block.text.strip()
                        try:
                            # Parse JSON directly (Claude guarantees valid JSON with json_schema)
                            result = json.loads(text)
                            return result
                        except json.JSONDecodeError as e:
                            # This shouldn't happen with json_schema format, but handle it
                            raise RuntimeError(f"Failed to parse JSON from response: {e}")

                # If we get here without valid JSON, something is wrong
                raise RuntimeError("Response ended without valid JSON output")

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                # Execute all tool calls
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        # Execute tool
                        result = mcp_server.execute_tool(
                            block.name,
                            block.input,
                        )

                        # Add result
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })

                # Add tool results to messages
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })
                continue

            # If we get here, something unexpected happened
            break

        raise RuntimeError(
            f"Failed to generate structured output after {max_iterations} iterations"
        )


def create_llm_provider(provider_name: str, provider_config) -> LLMProvider:
    if provider_name == "anthropic":
        return ClaudeProvider(provider_config)
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider_name}. "
            f"Supported providers: anthropic"
        )
