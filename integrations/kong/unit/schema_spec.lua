local mock = require "mock_kong"


local function config_field(schema, name)
  for _, field in ipairs(schema.fields) do
    if field.config then
      for _, f in ipairs(field.config.fields) do
        if f[name] then
          return f[name]
        end
      end
    end
  end
end


describe("prompt-shields schema", function()
  local schema, validate

  setup(function()
    mock.install()
    schema = require "kong.plugins.prompt-shields.schema"
    for _, field in ipairs(schema.fields) do
      if field.config then
        validate = field.config.custom_validator
      end
    end
  end)

  it("is named prompt-shields", function()
    assert.equal("prompt-shields", schema.name)
  end)

  it("makes the ingest key required, encrypted and referenceable", function()
    local key = config_field(schema, "ingest_key")
    assert.is_true(key.required)
    assert.is_true(key.encrypted)
    assert.is_true(key.referenceable)
  end)

  it("has safe defaults", function()
    assert.equal(1, config_field(schema, "sample_rate").default)
    assert.same({ 0, 1 }, config_field(schema, "sample_rate").between)
    assert.is_true(config_field(schema, "strip_ps_headers").default)
    assert.is_false(config_field(schema, "allow_http").default)
    assert.is_true(config_field(schema, "ssl_verify").default)
    assert.equal(10000, config_field(schema, "timeout").default)
    assert.same({ max_batch_size = 200, max_coalescing_delay = 5 },
                config_field(schema, "queue").default)
  end)

  describe("custom_validator", function()
    it("accepts an https collector", function()
      assert.is_true(validate({ collector_url = "https://collector.example.com", allow_http = false }))
      assert.is_true(validate({ collector_url = "HTTPS://collector.example.com:8443/ps" }))
    end)

    it("rejects http unless allow_http is set", function()
      local ok, err = validate({ collector_url = "http://collector.internal:8000", allow_http = false })
      assert.is_nil(ok)
      assert.matches("must use https", err)
      assert.is_true(validate({ collector_url = "http://collector.internal:8000", allow_http = true }))
    end)

    it("rejects other schemes", function()
      local ok, err = validate({ collector_url = "ftp://collector.example.com", allow_http = true })
      assert.is_nil(ok)
      assert.matches("http%(s%) URL", err)
    end)

    it("rejects credentials, query strings and fragments in the URL", function()
      assert.is_nil((validate({ collector_url = "https://u:p@collector.example.com" })))
      assert.is_nil((validate({ collector_url = "https://collector.example.com/?key=ps-x" })))
      assert.is_nil((validate({ collector_url = "https://collector.example.com/#x" })))
    end)

    it("caps the batch size at the collector's limit", function()
      local base = "https://collector.example.com"
      assert.is_true(validate({ collector_url = base, queue = { max_batch_size = 1000 } }))
      local ok, err = validate({ collector_url = base, queue = { max_batch_size = 1001 } })
      assert.is_nil(ok)
      assert.matches("1000 or less", err)
    end)
  end)
end)
