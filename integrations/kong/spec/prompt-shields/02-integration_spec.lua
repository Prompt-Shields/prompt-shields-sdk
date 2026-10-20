-- Pongo: a real Kong with ai-proxy in front of a mock OpenAI endpoint, and
-- the plugin delivering to a mock collector.
local helpers = require "spec.helpers"
local http_mock = require "spec.helpers.http_mock"
local cjson = require "cjson"

local PLUGIN_NAME = "prompt-shields"
local KEY = "ps-test-key-12345"

local LLM_RESPONSE = cjson.encode({
  id = "chatcmpl-1",
  object = "chat.completion",
  created = 1760000000,
  model = "gpt-4o-2024-08-06",
  choices = { { index = 0, finish_reason = "stop",
                message = { role = "assistant", content = "hi" } } },
  usage = { prompt_tokens = 12, completion_tokens = 3, total_tokens = 15 },
})


local function lower_keys(t)
  local out = {}
  for k, v in pairs(t or {}) do out[k:lower()] = v end
  return out
end


for _, strategy in helpers.all_strategies() do
  if strategy ~= "cassandra" then
    describe(PLUGIN_NAME .. ": (integration) [#" .. strategy .. "]", function()
      local client, llm, collector

      lazy_setup(function()
        local llm_port = helpers.get_available_port()
        local collector_port = helpers.get_available_port()

        llm = http_mock.new(llm_port, {
          ["/"] = {
            access = [[
              ngx.print("ok")
              ngx.exit(200)
            ]],
          },
          ["/v1/chat/completions"] = {
            access = [[
              ngx.header["Content-Type"] = "application/json"
              ngx.print(']] .. LLM_RESPONSE .. [[')
              ngx.exit(200)
            ]],
          },
        }, { prefix = "servroot_llm" })

        collector = http_mock.new(collector_port, {
          ["/ingest/kong"] = {
            access = [[
              ngx.header["Content-Type"] = "application/json"
              ngx.print('{"ingested":1}')
              ngx.exit(200)
            ]],
          },
        }, { prefix = "servroot_collector", log_opts = { req = true, req_body = true } })

        assert(llm:start())
        assert(collector:start())

        local bp = helpers.get_db_utils(strategy == "off" and "postgres" or strategy, nil, { PLUGIN_NAME })

        local service = bp.services:insert({ url = "http://127.0.0.1:" .. llm_port })
        local ai_route = bp.routes:insert({
          name = "hr-chat", service = service, paths = { "/chat" }, protocols = { "http", "https" },
        })
        local plain_route = bp.routes:insert({
          name = "plain", service = service, paths = { "/plain" }, protocols = { "http", "https" },
        })

        bp.plugins:insert({
          name = "ai-proxy",
          route = { id = ai_route.id },
          config = {
            route_type = "llm/v1/chat",
            auth = { header_name = "Authorization", header_value = "Bearer sk-provider-secret" },
            logging = { log_statistics = true, log_payloads = true },
            model = {
              provider = "openai",
              name = "gpt-4o",
              options = {
                upstream_url = "http://127.0.0.1:" .. llm_port .. "/v1/chat/completions",
                input_cost = 2.5,
                output_cost = 10,
              },
            },
          },
        })

        for _, route in ipairs({ ai_route, plain_route }) do
          bp.plugins:insert({
            name = PLUGIN_NAME,
            route = { id = route.id },
            config = {
              collector_url = "http://127.0.0.1:" .. collector_port,
              allow_http = true,
              ingest_key = KEY,
              queue = { max_batch_size = 10, max_coalescing_delay = 0.1 },
            },
          })
        end

        assert(helpers.start_kong({
          database = strategy,
          nginx_conf = "spec/fixtures/custom_nginx.template",
          plugins = "bundled," .. PLUGIN_NAME,
          declarative_config = strategy == "off" and helpers.make_yaml_file() or nil,
        }))
      end)

      lazy_teardown(function()
        helpers.stop_kong()
        if llm then llm:stop() end
        if collector then collector:stop() end
      end)

      before_each(function()
        client = helpers.proxy_client()
      end)

      after_each(function()
        if client then client:close() end
      end)

      it("strips X-PS-* upstream and reports the AI call to the collector", function()
        local res = client:post("/chat", {
          headers = {
            ["Content-Type"] = "application/json",
            ["X-PS-Business-Unit"] = "Payments",
            ["X-PS-Use-Case"] = "fraud-explainer",
            ["X-PS-Owner"] = "owner@example.com",
            ["X-PS-Data-Classification"] = "confidential",
            ["X-PS-Environment"] = "production",
            ["X-PS-Max-Cost"] = "5",
          },
          body = { messages = { { role = "user", content = "my IBAN is DE00SECRET" } } },
        })
        assert.res_status(200, res)

        -- The provider never saw an X-PS-* header.
        local upstream = llm:get_request()
        for name in pairs(upstream.headers) do
          assert.is_nil(name:lower():match("^x%-ps%-"), "forwarded " .. name)
        end

        local sent = collector:get_request()
        local headers = lower_keys(sent.headers)
        assert.equal("Bearer " .. KEY, headers["authorization"])
        assert.matches("application/json", headers["content-type"], nil, true)

        local body = assert(sent.body)
        for _, secret in ipairs({ "IBAN", "SECRET", "sk-provider", "127.0.0.1", "querystring" }) do
          assert.is_nil(body:find(secret, 1, true), "collector body leaked " .. secret)
        end

        local batch = cjson.decode(body)
        assert.equal(1, #batch)
        local entry = batch[1]
        assert.same({
          ["x-ps-business-unit"] = "Payments",
          ["x-ps-use-case"] = "fraud-explainer",
          ["x-ps-owner"] = "owner@example.com",
          ["x-ps-data-classification"] = "confidential",
          ["x-ps-environment"] = "production",
        }, entry.request.headers)
        assert.equal("hr-chat", entry.route.name)
        local meta, usage = entry.ai.proxy.meta, entry.ai.proxy.usage
        assert.equal("openai", meta.provider_name)
        assert.equal("gpt-4o", meta.request_model)
        assert.equal("gpt-4o-2024-08-06", meta.response_model)
        assert.equal(12, usage.prompt_tokens)
        assert.equal(3, usage.completion_tokens)
        assert.is_nil(entry.ai.proxy.payload)
      end)

      it("strips X-PS-* on a non-AI route and reports nothing", function()
        llm:clean()
        collector:clean()
        local res = client:post("/plain", {
          headers = { ["Content-Type"] = "application/json", ["X-PS-Owner"] = "owner@example.com" },
          body = {},
        })
        assert.response(res).has.status(200)

        local upstream = llm:get_request()
        for name in pairs(upstream.headers) do
          assert.is_nil(name:lower():match("^x%-ps%-"), "forwarded " .. name)
        end

        ngx.sleep(1) -- well past max_coalescing_delay
        assert.same({}, collector:retrieve_mocking_logs())
      end)
    end)
  end
end
