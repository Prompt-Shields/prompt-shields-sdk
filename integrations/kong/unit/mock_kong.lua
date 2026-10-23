-- Minimal stand-ins for the Kong PDK and the Kong modules the plugin
-- requires, so the plugin can be loaded and exercised with plain LuaJIT.
-- Only what the plugin calls is mocked.

local M = {}


-- kong.db.schema.typedefs: each typedef is a table that can be called with
-- overrides, like Kong's Schema.define.
local function typedef(base)
  return setmetatable(base, {
    __call = function(self, overrides)
      local out = {}
      for k, v in pairs(self) do out[k] = v end
      for k, v in pairs(overrides or {}) do out[k] = v end
      return typedef(out)
    end,
  })
end

local typedefs = {
  url = typedef({ type = "string" }),
  protocols_http = typedef({ type = "set" }),
  queue = typedef({ type = "record" }),
}


--- Install the module mocks. Call before requiring a plugin module.
-- @treturn table state the queue and http mocks record into
function M.install()
  local state = {
    enqueued = {},     -- { queue_conf, handler, handler_conf, value }
    http_calls = {},   -- { url, params }
    http_response = { status = 200, body = "{}" },
    http_error = nil,
    timeout = nil,
  }

  package.loaded["kong.db.schema.typedefs"] = typedefs

  package.loaded["kong.tools.queue"] = {
    get_plugin_params = function(plugin_name, config)
      local q = {}
      for k, v in pairs(config.queue or {}) do q[k] = v end
      q.name = q.name or ("plugin-id-" .. plugin_name)
      return q
    end,
    enqueue = function(queue_conf, handler, handler_conf, value)
      if state.enqueue_error then
        return nil, state.enqueue_error
      end
      state.enqueued[#state.enqueued + 1] = {
        queue_conf = queue_conf, handler = handler,
        handler_conf = handler_conf, value = value,
      }
      return true
    end,
  }

  package.loaded["resty.http"] = {
    new = function()
      return {
        set_timeout = function(_, ms) state.timeout = ms end,
        request_uri = function(_, url, params)
          state.http_calls[#state.http_calls + 1] = { url = url, params = params }
          if state.http_error then
            return nil, state.http_error
          end
          return state.http_response
        end,
      }
    end,
  }

  -- Reload the plugin with these mocks.
  package.loaded["kong.plugins.prompt-shields.handler"] = nil
  package.loaded["kong.plugins.prompt-shields.schema"] = nil

  return state
end


--- A fake `kong` global (and the `ngx.req` calls the plugin makes) for one
-- request.
-- @tparam table req { headers = {...}, serialized = {...} }. Optional:
--   truncated = n: get_headers() returns only n headers and "truncated";
--   get_headers_error, get_all_headers_error, clear_header_error,
--   serialize_error: error() from that call.
-- @treturn table kong, table state, table ngx
function M.new_kong(req)
  local logs = { err = {}, debug = {} }
  local cleared = {}
  local headers = req.headers or {}

  local function logger(level)
    return function(...)
      local parts = {}
      for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
      logs[level][#logs[level] + 1] = table.concat(parts)
    end
  end

  local kong = {
    ctx = { plugin = {} },
    request = {
      get_headers = function(max)
        assert(max == nil or (max >= 1 and max <= 1000), "max_headers out of range")
        if req.get_headers_error then
          error(req.get_headers_error)
        end
        if req.truncated then
          -- Like ngx: the first n headers in order, and "truncated".
          local out, n = {}, 0
          for _, name in ipairs(req.order or {}) do
            if n >= req.truncated then break end
            out[name] = headers[name]
            n = n + 1
          end
          return out, "truncated"
        end
        return headers
      end,
    },
    service = {
      request = {
        clear_header = function(name)
          if req.clear_header_error then
            error(req.clear_header_error)
          end
          cleared[#cleared + 1] = name
          headers[name] = nil
        end,
      },
    },
    log = {
      err = logger("err"),
      debug = logger("debug"),
      warn = logger("err"),
      serialize = function(opts)
        req.serialize_opts = opts
        if req.serialize_error then
          error(req.serialize_error)
        end
        return req.serialized
      end,
    },
  }

  local ngx_cleared = {}
  local ngx = {
    req = {
      get_headers = function(max)
        assert(max == 0, "expected an unlimited read")
        if req.get_all_headers_error then
          error(req.get_all_headers_error)
        end
        return headers
      end,
      clear_header = function(name)
        ngx_cleared[#ngx_cleared + 1] = name
        headers[name] = nil
      end,
    },
  }

  return kong, { logs = logs, cleared = cleared, ngx_cleared = ngx_cleared }, ngx
end


return M
