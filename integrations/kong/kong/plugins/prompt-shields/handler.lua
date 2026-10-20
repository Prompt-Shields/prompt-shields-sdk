-- prompt-shields: report Kong AI Gateway usage to the Prompt Shields AI
-- asset registry.
--
-- access: stash the X-PS-* business headers in kong.ctx.plugin and, by
--         default, remove every X-PS-* header from the upstream request.
-- log:    build a compact event (event.build below) from Kong's AI analytics
--         and the stashed headers, and queue it.
-- queue:  kong.tools.queue batches events per plugin instance and posts them
--         to <collector_url>/ingest/kong from a timer, never from the
--         request. Delivery is fail-open: an error here is logged, and the
--         proxied request is never delayed or failed by it.

local Queue = require "kong.tools.queue"
local cjson = require "cjson.safe"
local http = require "resty.http"

local type = type
local pairs = pairs
local ipairs = ipairs
local next = next
local select = select
local lower = string.lower
local sub = string.sub

-- Event building: pure helpers (no PDK calls), exposed as handler._event for
-- the unit tests. They live in this file because Konnect's streamed custom
-- plugins take exactly one handler.lua and one schema.lua.
--
-- The event is a cut-down Kong `http-log` entry: the JSON shape the
-- collector's POST /ingest/kong already maps
-- (packages/collector/collector/kong_ingest.py), holding only the fields that
-- route reads. It is built from an allow-list, so request and response
-- bodies, AI payloads, headers other than X-PS-*, the query string and the
-- client IP can never end up in it.
local event = {}

-- The X-PS-* business headers the collector maps (the gateway's contract,
-- gateway/src/middlewares/ps-telemetry.ts). Lower case, as Kong returns
-- header names.
event.PS_HEADERS = {
  "x-ps-business-unit",
  "x-ps-use-case",
  "x-ps-owner",
  "x-ps-data-classification",
  "x-ps-environment",
}

local PS_HEADER_SET = {}
for _, name in ipairs(event.PS_HEADERS) do
  PS_HEADER_SET[name] = true
end

-- Same caps as the collector (kong_ingest.py): longer values are dropped
-- there, so there is no point queueing them.
event.MAX_HEADER_VALUE_LENGTH = 256
event.MAX_NAME_LENGTH = 256
-- AI namespaces copied per request (the collector reads at most 8).
event.MAX_NAMESPACES = 8

local PS_PREFIX = "x-ps-"
local PS_PREFIX_LEN = #PS_PREFIX


local function is_ps_header(name)
  return type(name) == "string" and lower(sub(name, 1, PS_PREFIX_LEN)) == PS_PREFIX
end
event.is_ps_header = is_ps_header


local function first_value(value)
  -- A repeated header comes back as a list; the collector reads the first.
  if type(value) == "table" then
    value = value[1]
  end
  return value
end


local function short_string(value, max_length)
  if type(value) ~= "string" or value == "" or #value > max_length then
    return nil
  end
  return value
end


--- Pick the X-PS-* business headers out of a request header table.
-- @tparam table headers as returned by kong.request.get_headers()
-- @treturn table|nil lower-case name -> value, or nil when there are none
function event.collect_ps_headers(headers)
  if type(headers) ~= "table" then
    return nil
  end
  local out
  for name, value in pairs(headers) do
    if type(name) == "string" then
      local lname = lower(name)
      if PS_HEADER_SET[lname] then
        value = short_string(first_value(value), event.MAX_HEADER_VALUE_LENGTH)
        if value then
          out = out or {}
          out[lname] = value
        end
      end
    end
  end
  return out
end


--- Every X-PS-* header name on the request (not only the five mapped ones),
-- so all of them can be removed before the request goes upstream.
-- @tparam table headers as returned by kong.request.get_headers()
-- @treturn table list of header names (possibly empty)
function event.ps_header_names(headers)
  local names = {}
  if type(headers) ~= "table" then
    return names
  end
  for name in pairs(headers) do
    if is_ps_header(name) then
      names[#names + 1] = name
    end
  end
  return names
end


local function scalar(value)
  local t = type(value)
  if t == "string" or t == "number" or t == "boolean" then
    return value
  end
  return nil
end


-- meta and usage fields the collector reads. Anything else Kong writes there
-- (plugin_id, Azure deployment ids, token details, ...) is left out.
local META_FIELDS = { "provider_name", "request_model", "response_model", "llm_latency" }
local USAGE_FIELDS = { "prompt_tokens", "completion_tokens", "cost" }


local function copy_fields(src, fields)
  local out
  for _, key in ipairs(fields) do
    local value = scalar(src[key])
    if value ~= nil then
      out = out or {}
      out[key] = value
    end
  end
  return out
end


local function ai_namespaces(ai)
  if type(ai) ~= "table" then
    return nil
  end

  -- Sort the names so the MAX_NAMESPACES cut is stable, "proxy" first as
  -- in the collector.
  local names = {}
  for name, ns in pairs(ai) do
    if type(name) == "string" and name ~= "payload" and type(ns) == "table"
      and (type(ns.meta) == "table" or type(ns.usage) == "table")
    then
      names[#names + 1] = name
    end
  end
  table.sort(names, function(a, b)
    if a == "proxy" then return b ~= "proxy" end
    if b == "proxy" then return false end
    return a < b
  end)

  local out, count = nil, 0
  for _, name in ipairs(names) do
    if count >= event.MAX_NAMESPACES then
      break
    end
    local ns = ai[name]
    local meta = type(ns.meta) == "table" and copy_fields(ns.meta, META_FIELDS) or nil
    local usage
    if type(ns.usage) == "table" then
      usage = copy_fields(ns.usage, USAGE_FIELDS)
      -- Kong 3.14 reports non-chat calls (embeddings and the like) as
      -- input_tokens / output_tokens; the collector reads the chat names.
      local input = scalar(ns.usage.input_tokens)
      local output = scalar(ns.usage.output_tokens)
      if input ~= nil and (usage == nil or usage.prompt_tokens == nil) then
        usage = usage or {}
        usage.prompt_tokens = input
      end
      if output ~= nil and (usage == nil or usage.completion_tokens == nil) then
        usage = usage or {}
        usage.completion_tokens = output
      end
    end
    if meta or usage then
      out = out or {}
      out[name] = { meta = meta, usage = usage }
      count = count + 1
    end
  end
  return out
end


local function named(entity, ...)
  if type(entity) ~= "table" then
    return nil
  end
  local out
  for i = 1, select("#", ...) do
    local key = select(i, ...)
    local value = short_string(entity[key], event.MAX_NAME_LENGTH)
    if value then
      out = out or {}
      out[key] = value
    end
  end
  return out
end


--- Build the compact event for one proxied request.
-- @tparam table log the table returned by kong.log.serialize()
-- @tparam table|nil ps_headers the X-PS-* values stashed in the access phase
-- @tparam string|nil version the plugin version, sent for support
-- @treturn table|nil the event, or nil when the request carried no AI
-- analytics (the collector would ignore it)
function event.build(log, ps_headers, version)
  if type(log) ~= "table" then
    return nil
  end
  local ai = ai_namespaces(log.ai)
  if not ai then
    return nil
  end

  local entry = {
    ai = ai,
    consumer = named(log.consumer, "username", "custom_id"),
    route = named(log.route, "name"),
    service = named(log.service, "name"),
  }

  local latencies = type(log.latencies) == "table" and log.latencies or nil
  if latencies and type(latencies.proxy) == "number" then
    entry.latencies = { proxy = latencies.proxy }
  end

  if type(ps_headers) == "table" and next(ps_headers) then
    local headers = {}
    for _, name in ipairs(event.PS_HEADERS) do
      headers[name] = short_string(ps_headers[name], event.MAX_HEADER_VALUE_LENGTH)
    end
    if next(headers) then
      entry.request = { headers = headers }
    end
  end

  if version then
    -- Ignored by the collector today; tells support which build sent it.
    entry.prompt_shields = { plugin_version = version }
  end

  return entry
end


--- Should this request be sampled?
-- @tparam number rate between 0 and 1
-- @tparam function random returns a float in [0, 1)
function event.sampled(rate, random)
  if rate == nil or rate >= 1 then
    return true
  end
  if rate <= 0 then
    return false
  end
  return random() < rate
end


--- The ingest URL for a collector base URL.
function event.ingest_url(collector_url)
  return (collector_url:gsub("/+$", "")) .. "/ingest/kong"
end


---------------------------------------------------------------------------
-- Plugin
---------------------------------------------------------------------------

local kong = kong
local pcall = pcall
local concat = table.concat
local random = math.random
local tostring = tostring

local VERSION = "0.1.0"
local USER_AGENT = "kong-plugin-prompt-shields/" .. VERSION
-- Stops kong.log.serialize() from copying the request and response headers
-- (including Authorization), which the event never uses. Versions of Kong
-- that do not know the flag ignore it.
local SERIALIZE_OPTS = { __skip_fetch_headers__ = true }
-- Up to Kong's own cap, so a request cannot hide an X-PS-* header behind 100
-- others.
local MAX_HEADERS = 1000

-- Statuses that will never succeed on retry: drop the batch instead of
-- retrying it until queue.max_retry_time.
local DROP_STATUSES = {
  [400] = "bad request",
  [401] = "invalid ingest key",
  [403] = "ingest key not allowed",
  [404] = "collector_url has no /ingest/kong route",
  [413] = "batch too large",
  [415] = "unsupported media type",
  [422] = "unprocessable",
}


local PromptShieldsHandler = {
  -- Log phase after the AI plugins (ai-proxy is 770) have recorded their
  -- analytics, next to the other loggers (http-log is 12).
  PRIORITY = 11,
  VERSION = VERSION,
}


-- Queue handler: post one batch of JSON-encoded events as a JSON array.
-- Returns true when the batch is done with (delivered or dropped for good),
-- or nil and an error to have the queue retry it.
local function send_batch(conf, entries)
  local httpc = http.new()
  httpc:set_timeout(conf.timeout)

  local res, err = httpc:request_uri(event.ingest_url(conf.collector_url), {
    method = "POST",
    headers = {
      ["Content-Type"] = "application/json",
      ["Authorization"] = "Bearer " .. conf.ingest_key,
      ["User-Agent"] = USER_AGENT,
    },
    body = "[" .. concat(entries, ",") .. "]",
    keepalive_timeout = conf.keepalive,
    ssl_verify = conf.ssl_verify,
  })
  if not res then
    return nil, "could not reach the collector: " .. tostring(err)
  end

  local status = res.status
  if status >= 200 and status < 300 then
    return true
  end

  local reason = DROP_STATUSES[status]
  if reason then
    -- Never logs the key or the body: only the status.
    kong.log.err("collector returned ", status, " (", reason, "); dropping ",
                 #entries, " event(s)")
    return true
  end

  -- 408, 429 and 5xx: let the queue back off and retry.
  return nil, "collector returned " .. status
end
PromptShieldsHandler._send_batch = send_batch -- for tests
PromptShieldsHandler._event = event        -- for tests


local function access(conf)
  local headers = kong.request.get_headers(MAX_HEADERS)
  kong.ctx.plugin.ps_headers = event.collect_ps_headers(headers)

  if conf.strip_ps_headers then
    local clear_header = kong.service.request.clear_header
    for _, name in ipairs(event.ps_header_names(headers)) do
      clear_header(name)
    end
  end
end


local function log(conf)
  if not event.sampled(conf.sample_rate, random) then
    return
  end

  local entry = event.build(kong.log.serialize(SERIALIZE_OPTS),
                            kong.ctx.plugin.ps_headers, VERSION)
  if not entry then
    return -- no AI analytics on this request: nothing the collector maps
  end

  local body, err = cjson.encode(entry)
  if not body then
    kong.log.err("could not encode event: ", err)
    return
  end

  local queue_conf = Queue.get_plugin_params("prompt-shields", conf)
  local ok
  ok, err = Queue.enqueue(queue_conf, send_batch, conf, body)
  if not ok then
    kong.log.err("could not queue event: ", err)
  end
end


function PromptShieldsHandler:access(conf)
  local ok, err = pcall(access, conf)
  if not ok then
    kong.log.err("access phase failed, request continues: ", err)
  end
end


function PromptShieldsHandler:log(conf)
  local ok, err = pcall(log, conf)
  if not ok then
    kong.log.err("log phase failed: ", err)
  end
end


return PromptShieldsHandler
