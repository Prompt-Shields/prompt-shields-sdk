local cjson = require "cjson"
local mock = require "mock_kong"
local fixtures = require "fixtures"


local function conf(overrides)
  local c = {
    collector_url = "https://collector.example.com",
    ingest_key = "ps-test-key",
    allow_http = false,
    ssl_verify = true,
    sample_rate = 1,
    strip_ps_headers = true,
    timeout = 2500,
    keepalive = 60000,
    queue = { max_batch_size = 200, max_coalescing_delay = 5 },
  }
  for k, v in pairs(overrides or {}) do c[k] = v end
  return c
end


describe("prompt-shields handler", function()
  local state, handler, kong_state, req

  -- Load the handler against a fresh fake `kong` for one request.
  local function load(request)
    req = request
    state = mock.install()
    local k
    k, kong_state = mock.new_kong(req)
    _G.kong = k
    handler = require "kong.plugins.prompt-shields.handler"
  end

  after_each(function()
    _G.kong = nil
  end)

  describe("access phase", function()
    it("stashes the X-PS-* values and strips every X-PS-* header", function()
      load({ headers = {
        ["x-ps-business-unit"] = "Payments",
        ["x-ps-use-case"] = "fraud-explainer",
        ["x-ps-max-cost"] = "5",
        authorization = "Bearer sk-provider",
        ["content-type"] = "application/json",
      } })
      handler:access(conf())

      assert.same({ ["x-ps-business-unit"] = "Payments", ["x-ps-use-case"] = "fraud-explainer" },
                  kong.ctx.plugin.ps_headers)
      table.sort(kong_state.cleared)
      assert.same({ "x-ps-business-unit", "x-ps-max-cost", "x-ps-use-case" }, kong_state.cleared)
      assert.same({ authorization = "Bearer sk-provider", ["content-type"] = "application/json" },
                  req.headers)
    end)

    it("leaves the headers in place when strip_ps_headers is false", function()
      load({ headers = { ["x-ps-owner"] = "a@example.com" } })
      handler:access(conf({ strip_ps_headers = false }))
      assert.same({ ["x-ps-owner"] = "a@example.com" }, kong.ctx.plugin.ps_headers)
      assert.same({}, kong_state.cleared)
    end)

    it("fails open: an error is logged and the request continues", function()
      load({ get_headers_error = "boom" })
      assert.has_no.errors(function() handler:access(conf()) end)
      assert.matches("access phase failed", kong_state.logs.err[1])
    end)
  end)

  describe("log phase", function()
    it("queues one compact JSON event per AI request", function()
      load({ headers = { ["x-ps-use-case"] = "fraud-explainer" }, serialized = fixtures.serialized_3_14() })
      local c = conf()
      handler:access(c)
      handler:log(c)

      assert.equal(1, #state.enqueued)
      local q = state.enqueued[1]
      assert.equal(c, q.handler_conf)
      assert.equal(200, q.queue_conf.max_batch_size)
      assert.equal("string", type(q.value))
      local entry = cjson.decode(q.value)
      assert.same({ ["x-ps-use-case"] = "fraud-explainer" }, entry.request.headers)
      assert.equal("gpt-4o", entry.ai.proxy.meta.request_model)
      assert.equal("0.1.0", entry.prompt_shields.plugin_version)
      assert.is_nil(entry.client_ip)
      -- Kong is asked not to copy the request and response headers.
      assert.same({ __skip_fetch_headers__ = true }, req.serialize_opts)
    end)

    it("queues nothing for a request without AI analytics", function()
      load({ serialized = fixtures.serialized_non_ai() })
      handler:log(conf())
      assert.equal(0, #state.enqueued)
    end)

    it("queues nothing when the request is not sampled", function()
      load({ serialized = fixtures.serialized_3_14() })
      handler:log(conf({ sample_rate = 0 }))
      assert.equal(0, #state.enqueued)
    end)

    it("samples roughly at the configured rate", function()
      load({ serialized = fixtures.serialized_3_14() })
      math.randomseed(42)
      local c = conf({ sample_rate = 0.3 })
      for _ = 1, 2000 do handler:log(c) end
      assert.is_true(#state.enqueued > 450 and #state.enqueued < 750, tostring(#state.enqueued))
    end)

    it("fails open when serialize() throws", function()
      load({ serialize_error = "no ctx" })
      assert.has_no.errors(function() handler:log(conf()) end)
      assert.matches("log phase failed", kong_state.logs.err[1])
      assert.equal(0, #state.enqueued)
    end)

    it("logs when the queue refuses the entry", function()
      load({ serialized = fixtures.serialized_3_14() })
      state.enqueue_error = "queue full"
      handler:log(conf())
      assert.matches("could not queue event: queue full", kong_state.logs.err[1])
    end)
  end)

  describe("delivery", function()
    local entries = { '{"a":1}', '{"b":2}' }

    it("posts a JSON array with the bearer key to /ingest/kong", function()
      load({})
      local ok, err = handler._send_batch(conf(), entries)
      assert.is_true(ok)
      assert.is_nil(err)
      assert.equal(2500, state.timeout)

      local call = state.http_calls[1]
      assert.equal("https://collector.example.com/ingest/kong", call.url)
      assert.equal("POST", call.params.method)
      assert.equal("Bearer ps-test-key", call.params.headers.Authorization)
      assert.equal("application/json", call.params.headers["Content-Type"])
      assert.matches("^kong%-plugin%-prompt%-shields/", call.params.headers["User-Agent"])
      assert.is_true(call.params.ssl_verify)
      assert.equal(60000, call.params.keepalive_timeout)
      assert.same({ { a = 1 }, { b = 2 } }, cjson.decode(call.params.body))
    end)

    it("passes ssl_verify through", function()
      load({})
      handler._send_batch(conf({ ssl_verify = false }), entries)
      assert.is_false(state.http_calls[1].params.ssl_verify)
    end)

    for _, status in ipairs({ 408, 429, 500, 502, 503 }) do
      it("asks the queue to retry on " .. status, function()
        load({})
        state.http_response = { status = status, body = "" }
        local ok, err = handler._send_batch(conf(), entries)
        assert.is_nil(ok)
        assert.equal("collector returned " .. status, err)
      end)
    end

    it("asks the queue to retry when the collector is unreachable", function()
      load({})
      state.http_error = "connection refused"
      local ok, err = handler._send_batch(conf(), entries)
      assert.is_nil(ok)
      assert.matches("connection refused", err)
    end)

    for _, status in ipairs({ 400, 401, 403, 404, 413, 415, 422 }) do
      it("drops the batch on " .. status .. " without logging the key", function()
        load({})
        state.http_response = { status = status, body = "nope" }
        local ok = handler._send_batch(conf(), entries)
        assert.is_true(ok)
        local msg = kong_state.logs.err[1]
        assert.matches("collector returned " .. status, msg)
        assert.matches("dropping 2 event", msg)
        assert.is_nil(msg:find("ps-test-key", 1, true))
      end)
    end
  end)
end)
