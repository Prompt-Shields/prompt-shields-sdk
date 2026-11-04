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


-- Sorted, de-duplicated copy of a list of names.
local function set_of(list)
  local seen, out = {}, {}
  for _, v in ipairs(list) do
    if not seen[v] then seen[v] = true; out[#out + 1] = v end
  end
  table.sort(out)
  return out
end

local PS_FIVE = {
  "x-ps-business-unit", "x-ps-data-classification", "x-ps-environment",
  "x-ps-owner", "x-ps-use-case",
}


describe("prompt-shields handler", function()
  local state, handler, kong_state, req

  -- Load the handler against a fresh fake `kong` for one request.
  local function load(request)
    req = request
    state = mock.install()
    local k, n
    k, kong_state, n = mock.new_kong(req)
    _G.kong = k
    _G.ngx = n
    handler = require "kong.plugins.prompt-shields.handler"
  end

  after_each(function()
    _G.kong = nil
    _G.ngx = nil
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
      -- The five mapped names are cleared by name, then every other X-PS-*.
      local expected = set_of({ "x-ps-max-cost", unpack(PS_FIVE) })
      assert.same(expected, set_of(kong_state.cleared))
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
      assert.matches("could not read the request headers: .*boom", kong_state.logs.err[1])
    end)

    it("still strips every X-PS-* header when reading the headers fails", function()
      load({ get_headers_error = "boom", headers = {
        ["x-ps-owner"] = "a@example.com",
        ["x-ps-max-cost"] = "5",
        accept = "*/*",
      } })
      handler:access(conf())
      assert.is_nil(kong.ctx.plugin.ps_headers)
      assert.same({ accept = "*/*" }, req.headers)
      assert.same(set_of({ "x-ps-max-cost", unpack(PS_FIVE) }), set_of(kong_state.cleared))
    end)

    it("still strips when stashing the values fails", function()
      load({ headers = { ["x-ps-owner"] = "a@example.com", ["x-ps-extra"] = "1" } })
      kong.ctx = nil -- makes the stash throw
      assert.has_no.errors(function() handler:access(conf()) end)
      assert.matches("could not stash", kong_state.logs.err[1])
      assert.same({}, req.headers)
    end)

    it("falls back to clearing the five mapped names when the strip fails", function()
      load({ clear_header_error = "nope", headers = {
        ["x-ps-owner"] = "a@example.com",
        ["x-ps-use-case"] = "chat",
        accept = "*/*",
      } })
      assert.has_no.errors(function() handler:access(conf()) end)
      assert.matches("could not strip every X%-PS%-%* header", kong_state.logs.err[1])
      assert.same(PS_FIVE, set_of(kong_state.ngx_cleared))
      assert.same({ accept = "*/*" }, req.headers)
      -- The values were stashed before the strip.
      assert.same({ ["x-ps-owner"] = "a@example.com", ["x-ps-use-case"] = "chat" },
                  kong.ctx.plugin.ps_headers)
    end)

    it("finds X-PS-* headers past Kong's 1,000-header cap", function()
      local headers, order = {}, {}
      for i = 1, 1000 do
        local name = string.format("x-filler-%04d", i)
        headers[name] = "x"
        order[#order + 1] = name
      end
      headers["x-ps-owner"] = "a@example.com"
      headers["x-ps-hidden"] = "1"
      order[#order + 1] = "x-ps-owner"
      order[#order + 1] = "x-ps-hidden"
      load({ headers = headers, order = order, truncated = 1000 })
      handler:access(conf())

      assert.same({ ["x-ps-owner"] = "a@example.com" }, kong.ctx.plugin.ps_headers)
      assert.is_nil(req.headers["x-ps-hidden"])
      assert.is_nil(req.headers["x-ps-owner"])
      assert.equal("x", req.headers["x-filler-0001"])
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

    it("splits a batch into POSTs under the collector's body limit", function()
      load({})
      local max = handler._MAX_BATCH_BYTES
      assert.is_true(max <= 4 * 1024 * 1024)
      -- 1,000 events of ~10 KB: one POST would be ~10 MB.
      local big = {}
      for i = 1, 1000 do
        big[i] = cjson.encode({ i = i, pad = string.rep("x", 10000) })
      end
      assert.is_true(handler._send_batch(conf(), big))

      assert.is_true(#state.http_calls > 1)
      local seen = 0
      for _, call in ipairs(state.http_calls) do
        assert.is_true(#call.params.body <= max, tostring(#call.params.body))
        for _, e in ipairs(cjson.decode(call.params.body)) do
          seen = seen + 1
          assert.equal(seen, e.i)
        end
      end
      assert.equal(1000, seen)
    end)

    it("does not post the delivered part of a split batch again on retry", function()
      load({})
      local big = {}
      for i = 1, 3 do
        big[i] = cjson.encode({ i = i, pad = string.rep("x", 1500000) })
      end
      -- The first POST (events 1 and 2) succeeds, the second fails.
      local calls = 0
      package.loaded["resty.http"].new = function()
        return {
          set_timeout = function() end,
          request_uri = function(_, _, params)
            calls = calls + 1
            state.http_calls[#state.http_calls + 1] = { params = params }
            if calls == 2 then return { status = 503 } end
            return { status = 200 }
          end,
        }
      end
      local ok, err = handler._send_batch(conf(), big)
      assert.is_nil(ok)
      assert.equal("collector returned 503", err)
      assert.is_true(handler._send_batch(conf(), big))

      local posted = {}
      for _, call in ipairs(state.http_calls) do
        local ids = {}
        for _, e in ipairs(cjson.decode(call.params.body)) do ids[#ids + 1] = e.i end
        posted[#posted + 1] = ids
      end
      assert.same({ { 1, 2 }, { 3 }, { 3 } }, posted)
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

    for _, status in ipairs({ 302, 400, 401, 403, 404, 405, 409, 413, 415, 422 }) do
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
