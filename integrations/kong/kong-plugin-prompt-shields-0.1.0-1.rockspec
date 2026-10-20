package = "kong-plugin-prompt-shields"
version = "0.1.0-1"

local plugin_name = "prompt-shields"

source = {
  url = "git+https://github.com/Prompt-Shields/prompt-shields-sdk.git",
  tag = "kong-plugin-v0.1.0",
  dir = "prompt-shields-sdk/integrations/kong",
}

description = {
  summary = "Report Kong AI Gateway usage to the Prompt Shields AI asset registry.",
  detailed = [[
    Strips X-PS-* business headers before the request reaches the LLM
    provider, and sends a compact, metadata-only event per AI request
    (provider, models, tokens, cost, latency, consumer, route, business
    context) to the Prompt Shields collector, batched and fail-open.
    Never sends request or response bodies, credentials, the query string or
    the client IP.
  ]],
  homepage = "https://github.com/Prompt-Shields/prompt-shields-sdk/tree/main/integrations/kong",
  license = "Apache-2.0",
}

-- Kong itself provides lua-resty-http, lua-cjson and luasocket.
dependencies = {
  "lua >= 5.1",
}

build = {
  type = "builtin",
  modules = {
    ["kong.plugins." .. plugin_name .. ".handler"] = "kong/plugins/" .. plugin_name .. "/handler.lua",
    ["kong.plugins." .. plugin_name .. ".schema"] = "kong/plugins/" .. plugin_name .. "/schema.lua",
  },
}
