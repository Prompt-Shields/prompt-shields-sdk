std = "ngx_lua"
unused_args = false
redefined = false
max_line_length = 120

globals = {
  "kong",
}

files["spec/**/*.lua"] = {
  std = "ngx_lua+busted",
}

files["unit/**/*.lua"] = {
  std = "ngx_lua+busted",
}
