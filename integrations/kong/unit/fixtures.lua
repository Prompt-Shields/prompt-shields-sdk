-- kong.log.serialize() outputs, shaped after Kong's source:
-- kong/pdk/log.lua and kong/llm/plugin/shared-filters/serialize-analytics.lua
-- (3.10 and 3.14), and the 3.6/3.7 per-plugin namespaces.

local M = {}


-- Kong 3.14 ai-proxy chat call with log_statistics and log_payloads on.
function M.serialized_3_14()
  return {
    request = {
      id = "d3c1",
      uri = "/chat?api-version=1",
      url = "https://kong.example.com:443/chat?api-version=1",
      querystring = { ["api-version"] = "1" },
      method = "POST",
      headers = {
        authorization = "REDACTED",
        apikey = "consumer-secret",
        ["x-ps-use-case"] = "fraud-explainer",
      },
      size = 812,
    },
    upstream_uri = "/v1/chat/completions",
    response = { status = 200, headers = { ["x-ratelimit"] = "9" }, size = 1450 },
    latencies = { kong = 3, proxy = 812, request = 820, receive = 1 },
    tries = { { ip = "10.0.0.7", port = 443 } },
    authenticated_entity = { id = "cred-1" },
    route = { id = "r1", name = "hr-chat", paths = { "/chat" } },
    service = { id = "s1", name = "openai", host = "api.openai.com" },
    consumer = { id = "c1", username = "payments-api", custom_id = "app-42" },
    client_ip = "203.0.113.9",
    started_at = 1760000000000,
    workspace_name = "default",
    ai = {
      proxy = {
        meta = {
          plugin_id = "6e7c",
          provider_name = "openai",
          otel_provider_name = "openai",
          request_model = "gpt-4o",
          response_model = "gpt-4o-2024-08-06",
          request_mode = "oneshot",
          operation_name = "chat",
          llm_latency = 790,
        },
        usage = {
          total_tokens = 160,
          prompt_tokens = 120,
          completion_tokens = 40,
          prompt_tokens_details = { cached_tokens = 0 },
          cost = 0.0007,
          time_per_token = 19,
        },
        payload = {
          request = '{"messages":[{"role":"user","content":"my IBAN is ..."}]}',
          response = '{"choices":[]}',
        },
      },
    },
  }
end


-- Kong 3.6: analytics keyed by plugin name, two AI plugins on the route.
function M.serialized_3_6()
  return {
    latencies = { proxy = 400 },
    route = { name = "summarise" },
    service = { name = "azure-upstream" },
    ai = {
      ["ai-request-transformer"] = {
        meta = { provider_name = "azure", request_model = "gpt-35-turbo",
                 response_model = "gpt-35-turbo" },
        usage = { prompt_tokens = 10, completion_tokens = 5 },
      },
      ["ai-proxy"] = {
        meta = { provider_name = "openai", request_model = "gpt-4o-mini",
                 response_model = "gpt-4o-mini" },
        usage = { prompt_tokens = 30, completion_tokens = 12 },
      },
    },
  }
end


-- Kong 3.14 embeddings call: non-chat usage names.
function M.serialized_embeddings()
  return {
    latencies = { proxy = 50 },
    service = { name = "embeddings" },
    ai = {
      proxy = {
        meta = { provider_name = "gemini", request_model = "text-embedding-004" },
        usage = { input_tokens = 64, output_tokens = 0, total_tokens = 64 },
      },
    },
  }
end


-- A non-AI request (or log_statistics off).
function M.serialized_non_ai()
  return {
    latencies = { proxy = 5 },
    route = { name = "health" },
    request = { headers = { ["x-ps-owner"] = "a@b.c" } },
  }
end


return M
