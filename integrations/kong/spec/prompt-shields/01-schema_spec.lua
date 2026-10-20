-- Pongo: the plugin schema, validated by Kong's own schema library.
local PLUGIN_NAME = "prompt-shields"

local schema_def = require("kong.plugins." .. PLUGIN_NAME .. ".schema")
local v = require("spec.helpers").validate_plugin_config_schema


describe(PLUGIN_NAME .. ": (schema)", function()

  it("accepts a minimal https config and fills the defaults", function()
    local ok, err = v({
      collector_url = "https://collector.example.com",
      ingest_key = "ps-test-key",
    }, schema_def)
    assert.is_nil(err)
    assert.is_truthy(ok)
    local config = ok.config
    assert.equal(1, config.sample_rate)
    assert.is_true(config.strip_ps_headers)
    assert.is_false(config.allow_http)
    assert.is_true(config.ssl_verify)
    assert.equal(10000, config.timeout)
    assert.equal(200, config.queue.max_batch_size)
    assert.equal(5, config.queue.max_coalescing_delay)
    assert.equal(10000, config.queue.max_entries)
  end)

  it("accepts a vault reference for the ingest key", function()
    local ok, err = v({
      collector_url = "https://collector.example.com",
      ingest_key = "{vault://env/ps-ingest-key}",
    }, schema_def)
    assert.is_nil(err)
    assert.is_truthy(ok)
  end)

  it("requires the collector URL and the ingest key", function()
    local ok, err = v({}, schema_def)
    assert.is_falsy(ok)
    assert.same("required field missing", err.config.collector_url)
    assert.same("required field missing", err.config.ingest_key)
  end)

  it("rejects an http collector unless allow_http is set", function()
    local ok, err = v({
      collector_url = "http://collector.internal:8000",
      ingest_key = "ps-test-key",
    }, schema_def)
    assert.is_falsy(ok)
    assert.matches("must use https", err.config)

    ok, err = v({
      collector_url = "http://collector.internal:8000",
      ingest_key = "ps-test-key",
      allow_http = true,
    }, schema_def)
    assert.is_nil(err)
    assert.is_truthy(ok)
  end)

  it("rejects credentials or a query string in the collector URL", function()
    local ok = v({
      collector_url = "https://user:pass@collector.example.com",
      ingest_key = "ps-test-key",
    }, schema_def)
    assert.is_falsy(ok)
    ok = v({
      collector_url = "https://collector.example.com/?key=ps-test-key",
      ingest_key = "ps-test-key",
    }, schema_def)
    assert.is_falsy(ok)
  end)

  it("bounds sample_rate and the batch size", function()
    local ok, err = v({
      collector_url = "https://collector.example.com",
      ingest_key = "ps-test-key",
      sample_rate = 1.5,
    }, schema_def)
    assert.is_falsy(ok)
    assert.is_truthy(err.config.sample_rate)

    ok, err = v({
      collector_url = "https://collector.example.com",
      ingest_key = "ps-test-key",
      queue = { max_batch_size = 1001 },
    }, schema_def)
    assert.is_falsy(ok)
    assert.matches("1000 or less", err.config)
  end)
end)
