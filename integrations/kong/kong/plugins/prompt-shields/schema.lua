local typedefs = require "kong.db.schema.typedefs"
local url = require "socket.url"


-- The collector rejects requests with more than 1,000 entries (413), so a
-- bigger batch could never be delivered.
local MAX_BATCH_SIZE = 1000


local function validate_config(config)
  local parsed = url.parse(config.collector_url or "")
  local scheme = parsed and parsed.scheme and parsed.scheme:lower()
  if scheme ~= "https" and scheme ~= "http" then
    return nil, "collector_url must be an http(s) URL"
  end
  if scheme == "http" and config.allow_http ~= true then
    return nil, "collector_url must use https; set allow_http = true to send"
      .. " the ingest key in clear text (only for a collector on a trusted network)"
  end
  if parsed.userinfo then
    return nil, "collector_url must not contain credentials; use ingest_key"
  end
  if parsed.query or parsed.fragment then
    return nil, "collector_url must not have a query string or fragment"
  end

  local queue = config.queue
  if type(queue) == "table" and type(queue.max_batch_size) == "number"
    and queue.max_batch_size > MAX_BATCH_SIZE
  then
    return nil, "queue.max_batch_size must be " .. MAX_BATCH_SIZE
      .. " or less (the collector's per-request limit)"
  end

  return true
end


return {
  name = "prompt-shields",
  fields = {
    { protocols = typedefs.protocols_http },
    { config = {
        type = "record",
        fields = {
          { collector_url = typedefs.url {
              required = true,
              description = "Base URL of the Prompt Shields collector, such as"
                .. " https://collector.example.com. Events are posted to"
                .. " <collector_url>/ingest/kong.",
          } },
          { ingest_key = {
              type = "string",
              required = true,
              encrypted = true,      -- Kong Enterprise keyring; no-op elsewhere
              referenceable = true,  -- {vault://...}
              len_min = 1,
              description = "Prompt Shields tenant API key (ps-...), sent as"
                .. " `Authorization: Bearer <key>`. Use a vault reference.",
          } },
          { allow_http = {
              type = "boolean",
              required = true,
              default = false,
              description = "Allow a plain http:// collector_url. The ingest key"
                .. " is then sent unencrypted.",
          } },
          { ssl_verify = {
              type = "boolean",
              required = true,
              default = true,
              description = "Verify the collector's TLS certificate against"
                .. " Kong's lua_ssl_trusted_certificate.",
          } },
          { sample_rate = {
              type = "number",
              required = true,
              default = 1,
              between = { 0, 1 },
              description = "Fraction of AI requests to report, 0 to 1.",
          } },
          { strip_ps_headers = {
              type = "boolean",
              required = true,
              default = true,
              description = "Remove every X-PS-* request header before the"
                .. " request is proxied, so business metadata never reaches"
                .. " the LLM provider. The values are still reported.",
          } },
          { timeout = {
              type = "integer",
              required = true,
              default = 10000,
              between = { 1, 600000 },
              description = "Connect, send and read timeout for one request"
                .. " to the collector, in milliseconds.",
          } },
          { keepalive = {
              type = "integer",
              required = true,
              default = 60000,
              between = { 0, 3600000 },
              description = "How long an idle connection to the collector is"
                .. " kept open, in milliseconds.",
          } },
          { queue = typedefs.queue {
              default = {
                max_batch_size = 200,
                max_coalescing_delay = 5,
              },
          } },
        },
        custom_validator = validate_config,
      },
    },
  },
}
