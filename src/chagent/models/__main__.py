"""Inspect configuration and presets without transmitting credentials."""

import argparse
import json
import os

from chagent.models.settings import ModelConfigurationError, ModelSettings, PRESETS


def main():
    parser = argparse.ArgumentParser(description="Inspect provider configuration; never makes API requests")
    parser.add_argument("command", choices=("list", "check"))
    parser.add_argument("--provider", choices=(*PRESETS, "custom"))
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.command == "list":
        result = [{"provider": name, "key_variable": item.key_env, "default_model": item.model,
                   "base_url": item.base_url, "api": item.api} for name, item in PRESETS.items()]
    else:
        environment = dict(os.environ)
        if args.provider:
            environment["CHAGENT_PROVIDER"] = args.provider
        if args.model:
            environment["CHAGENT_MODEL"] = args.model
        try:
            settings = ModelSettings.from_env(environment)
        except ModelConfigurationError as error:
            parser.error(str(error))
        result = {"provider": settings.provider, "model": settings.model_id, "base_url": settings.base_url,
                  "api": settings.api, "context_budget": settings.context_window,
                  "output_budget": settings.max_tokens, "key_configured": True,
                  "thinking": settings.thinking, "thinking_source": settings.thinking_source,
                  "network_checked": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
