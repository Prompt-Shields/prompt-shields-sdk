"""Framework integrations — emit telemetry from calls made through a framework.

    import prompt_shields
    prompt_shields.instrument(ps_api_key="ps_...")

    # LangChain
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler
    chain.invoke(inputs, config={"callbacks": [PromptShieldsCallbackHandler()]})

    # LiteLLM
    from prompt_shields.integrations.litellm import register
    register()

Each submodule imports its framework; this package imports none of them, so
`import prompt_shields` never pulls in a framework.

Delivery and discovery metadata come from `instrument()`; without it the
integrations do nothing. `instrument(vendors=())` configures telemetry
without patching any provider SDK.

No double events: when the framework reaches a provider SDK that
`instrument()` patched, that call emits the event (tagged with the framework
and its use-case hint) and the integration emits nothing. Otherwise the
integration builds the event from the framework's own usage data.

Every event carries `framework`. The use-case hint (the outermost LangChain
chain or agent name, or `ps_use_case` in the call's metadata) fills
`use_case_name` only when no use case was configured. Tool calls the model
made are reported in `tool_calls_used`. Integration errors are logged and
swallowed.
"""
