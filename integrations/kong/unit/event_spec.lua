local cjson = require "cjson"
local mock = require "mock_kong"
local fixtures = require "fixtures"

-- The helpers live in handler.lua (Konnect streams one handler + one schema).
mock.install()
local event = require("kong.plugins.prompt-shields.handler")._event


local PS = {
  ["x-ps-business-unit"] = "Payments",
  ["x-ps-use-case"] = "fraud-explainer",
  ["x-ps-owner"] = "owner@example.com",
  ["x-ps-data-classification"] = "confidential",
  ["x-ps-environment"] = "production",
}


local function read_json(path)
  local f = assert(io.open(path, "rb"))
  local data = f:read("*a")
  f:close()
  return cjson.decode(data)
end


-- Every key anywhere in a nested table, joined by ".".
local function all_paths(t, prefix, out)
  out = out or {}
  for k, v in pairs(t) do
    local path = prefix and (prefix .. "." .. tostring(k)) or tostring(k)
    out[#out + 1] = path
    if type(v) == "table" then
      all_paths(v, path, out)
    end
  end
  return out
end


describe("prompt-shields event", function()

  describe("collect_ps_headers()", function()
    it("keeps the five mapped X-PS-* headers, lower-cased", function()
      local got = event.collect_ps_headers({
        ["X-PS-Business-Unit"] = "Payments",
        ["x-ps-use-case"] = "fraud-explainer",
        ["x-ps-owner"] = "owner@example.com",
        ["x-ps-data-classification"] = "confidential",
        ["x-ps-environment"] = "production",
        ["x-ps-max-cost"] = "5",          -- an X-PS-* header the collector does not map
        authorization = "Bearer sk-secret",
        ["content-type"] = "application/json",
      })
      assert.same(PS, got)
    end)

    it("takes the first value of a repeated header", function()
      assert.same({ ["x-ps-owner"] = "a@example.com" },
                  event.collect_ps_headers({ ["x-ps-owner"] = { "a@example.com", "b@example.com" } }))
    end)

    it("drops empty, oversized and non-string values", function()
      assert.is_nil(event.collect_ps_headers({
        ["x-ps-owner"] = "",
        ["x-ps-use-case"] = string.rep("a", 257),
        ["x-ps-environment"] = 5,
        ["x-ps-business-unit"] = {},
      }))
      assert.same({ ["x-ps-use-case"] = string.rep("a", 256) },
                  event.collect_ps_headers({ ["x-ps-use-case"] = string.rep("a", 256) }))
    end)

    it("returns nil without headers", function()
      assert.is_nil(event.collect_ps_headers(nil))
      assert.is_nil(event.collect_ps_headers({ accept = "*/*" }))
    end)
  end)

  describe("ps_header_names()", function()
    it("lists every X-PS-* header in any case, and nothing else", function()
      local names = event.ps_header_names({
        ["x-ps-owner"] = "a",
        ["X-PS-Max-Cost"] = "5",
        ["x-ps-route"] = "fast",
        ["x-psx"] = "not ours",
        ["x-portkey-provider"] = "openai",
        authorization = "Bearer x",
      })
      table.sort(names)
      assert.same({ "X-PS-Max-Cost", "x-ps-owner", "x-ps-route" }, names)
    end)

    it("handles a missing header table", function()
      assert.same({}, event.ps_header_names(nil))
    end)
  end)

  describe("build()", function()
    it("matches the contract fixture the collector test maps (Kong 3.14)", function()
      local built = event.build(fixtures.serialized_3_14(), PS, "0.1.0")
      -- Round-trip through JSON, as the queue does.
      local sent = cjson.decode(cjson.encode(built))
      assert.same(read_json("unit/contract/event-3.14.json"), sent)
    end)

    it("never carries bodies, auth, query string, client IP or other headers", function()
      local built = event.build(fixtures.serialized_3_14(), PS, "0.1.0")
      local encoded = cjson.encode(built)
      for _, needle in ipairs({
        "IBAN", "payload", "REDACTED", "authorization", "apikey", "consumer-secret",
        "api-version", "querystring", "203.0.113.9", "client_ip", "x-ratelimit",
        "upstream_uri", "tries", "10.0.0.7", "plugin_id", "workspace",
        "authenticated_entity", "cred-1", "api.openai.com", "prompt_tokens_details",
      }) do
        assert.is_nil(encoded:find(needle, 1, true), "event leaked " .. needle)
      end

      local allowed = {
        ["ai"] = true, ["ai.proxy"] = true, ["ai.proxy.meta"] = true, ["ai.proxy.usage"] = true,
        ["consumer"] = true, ["consumer.username"] = true, ["consumer.custom_id"] = true,
        ["route"] = true, ["route.name"] = true, ["service"] = true, ["service.name"] = true,
        ["latencies"] = true, ["latencies.proxy"] = true,
        ["request"] = true, ["request.headers"] = true,
        ["prompt_shields"] = true, ["prompt_shields.plugin_version"] = true,
      }
      for _, path in ipairs(all_paths(built)) do
        local ok = allowed[path]
          or path:match("^ai%.proxy%.meta%.[a-z_]+$")
          or path:match("^ai%.proxy%.usage%.[a-z_]+$")
          or path:match("^request%.headers%.x%-ps%-[a-z-]+$")
        assert.truthy(ok, "unexpected field " .. path)
      end
    end)

    it("uses the request headers stashed by the access phase, not the logged ones", function()
      -- The access phase strips X-PS-* headers, so the logged request no longer has them.
      local built = event.build(fixtures.serialized_3_14(), nil, nil)
      assert.is_nil(built.request)
      assert.is_nil(built.prompt_shields)
    end)

    it("keeps each 3.6-style per-plugin namespace", function()
      local built = event.build(fixtures.serialized_3_6(), nil, "0.1.0")
      assert.same({
        ["ai-proxy"] = {
          meta = { provider_name = "openai", request_model = "gpt-4o-mini", response_model = "gpt-4o-mini" },
          usage = { prompt_tokens = 30, completion_tokens = 12 },
        },
        ["ai-request-transformer"] = {
          meta = { provider_name = "azure", request_model = "gpt-35-turbo", response_model = "gpt-35-turbo" },
          usage = { prompt_tokens = 10, completion_tokens = 5 },
        },
      }, built.ai)
      assert.same({ name = "summarise" }, built.route)
      assert.is_nil(built.consumer)
    end)

    it("maps 3.14 input/output token names to the chat names", function()
      local built = event.build(fixtures.serialized_embeddings(), nil, nil)
      assert.same({ prompt_tokens = 64, completion_tokens = 0 }, built.ai.proxy.usage)
    end)

    it("prefers the chat token names when both are present", function()
      local log = fixtures.serialized_embeddings()
      log.ai.proxy.usage.prompt_tokens = 70
      local built = event.build(log, nil, nil)
      assert.same({ prompt_tokens = 70, completion_tokens = 0 }, built.ai.proxy.usage)
    end)

    it("returns nil for a request without AI analytics", function()
      assert.is_nil(event.build(fixtures.serialized_non_ai(), PS, "0.1.0"))
      assert.is_nil(event.build({ ai = { payload = { request = "x" } } }, nil, nil))
      assert.is_nil(event.build({ ai = { proxy = { payload = {} } } }, nil, nil))
      assert.is_nil(event.build(nil, nil, nil))
    end)

    it("caps the namespaces at the collector's limit, proxy first", function()
      local ai = { proxy = { meta = { provider_name = "openai" } } }
      for i = 1, 12 do
        ai[string.format("ns%02d", i)] = { meta = { provider_name = "openai" } }
      end
      local built = event.build({ ai = ai }, nil, nil)
      local names = {}
      for name in pairs(built.ai) do names[#names + 1] = name end
      table.sort(names)
      assert.equal(event.MAX_NAMESPACES, #names)
      assert.truthy(built.ai.proxy)
      assert.truthy(built.ai.ns01)
      assert.is_nil(built.ai.ns08)
    end)

    it("drops non-scalar and oversized values", function()
      local built = event.build({
        ai = { proxy = { meta = { provider_name = "openai", request_model = { "x" },
                                  response_model = string.rep("m", 257) },
                         usage = { prompt_tokens = "12", cost = { 1 } } } },
        route = { name = string.rep("r", 257) },
        consumer = { username = 7, custom_id = "app" },
        latencies = { proxy = "slow" },
      }, nil, nil)
      assert.same({ meta = { provider_name = "openai" }, usage = { prompt_tokens = "12" } }, built.ai.proxy)
      assert.is_nil(built.route)
      assert.same({ custom_id = "app" }, built.consumer)
      assert.is_nil(built.latencies)
    end)
  end)

  describe("sampled()", function()
    it("always samples at 1 and never at 0", function()
      local never = function() error("must not draw") end
      assert.is_true(event.sampled(1, never))
      assert.is_true(event.sampled(nil, never))
      assert.is_false(event.sampled(0, never))
    end)

    it("samples below the rate", function()
      assert.is_true(event.sampled(0.25, function() return 0.1 end))
      assert.is_false(event.sampled(0.25, function() return 0.25 end))
      assert.is_false(event.sampled(0.25, function() return 0.9 end))
    end)
  end)

  describe("ingest_url()", function()
    it("appends /ingest/kong to the base URL", function()
      assert.equal("https://c.example.com/ingest/kong", event.ingest_url("https://c.example.com"))
      assert.equal("https://c.example.com/ingest/kong", event.ingest_url("https://c.example.com/"))
      assert.equal("https://c.example.com/ps/ingest/kong", event.ingest_url("https://c.example.com/ps//"))
    end)
  end)
end)
