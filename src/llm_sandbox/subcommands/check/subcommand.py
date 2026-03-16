"""Check LLM provider configuration and connectivity."""

import asyncio
import sys
from pathlib import Path

import click

from llm_sandbox.config import load_config, get_provider_config, VertexAIConfig, AnthropicConfig
from llm_sandbox.llm_provider import create_llm_provider
from llm_sandbox.output import OutputService
from llm_sandbox.subcommand import Subcommand


class CheckSubcommand(Subcommand):
    """Check LLM provider configuration and connectivity."""

    name = "check"
    help = "Check LLM provider configuration and connectivity."

    def add_arguments(self, command):
        """Add check-specific arguments."""
        command = click.option(
            "--provider",
            type=str,
            help="Provider to test (defaults to default_provider from config)",
        )(command)
        return command

    def execute(self, **kwargs):
        """Execute the check command."""
        from llm_sandbox.output import create_output_service

        provider = kwargs.get("provider")
        output = create_output_service(format="text", verbose=False)

        output.info("Checking LLM provider configuration...\n")

        # Load config (merged global + project)
        config = load_config(Path.cwd())

        try:
            # Get provider config
            provider_name, provider_config = get_provider_config(config, provider)

            output.info(f"Provider: {provider_name}")
            output.info(f"Model: {provider_config.model}")

            if isinstance(provider_config, VertexAIConfig):
                output.info(f"Region: {provider_config.region}")
                output.info(f"Project ID: {provider_config.project_id}")
            elif isinstance(provider_config, AnthropicConfig):
                output.info(f"API Key Env: {provider_config.api_key_env}")
            else:
                raise ValueError(f"Unknown provider config type: {type(provider_config)}")

            output.info("\nValidating provider...")

            # Create provider and set system prompt
            llm_provider = create_llm_provider(provider_name, provider_config)
            llm_provider.set_system_prompt("You are a helpful AI assistant.")

            # Validate
            result = asyncio.run(llm_provider.validate())

            if result["success"]:
                output.success(result['message'])
                if "details" in result and "response_id" in result["details"]:
                    output.info(f"  Response ID: {result['details']['response_id']}")
                sys.exit(0)
            else:
                output.error(result['message'])
                if "details" in result:
                    details = result["details"]
                    if "error_type" in details:
                        output.error(f"  Error Type: {details['error_type']}")
                    if "error_message" in details:
                        output.error(f"  Error: {details['error_message']}")
                    if "guidance" in details:
                        output.info(f"  Suggestion: {details['guidance']}")
                sys.exit(1)

        except ValueError as e:
            output.error(f"Configuration error: {e}")
            sys.exit(1)
        except Exception as e:
            output.error(f"Unexpected error: {e}")
            sys.exit(1)
