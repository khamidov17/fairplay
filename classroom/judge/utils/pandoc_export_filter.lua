-- Pandoc Lua filter used by the assignment PDF/Word export (judge/utils/contest_export.py).
--
-- Problem statements are written by instructors and reach pandoc as HTML. Pandoc copies TeX math
-- ($...$, \(...\), <script type="math/tex">) and raw TeX verbatim into the LaTeX file that
-- lualatex compiles, so a statement could otherwise run \input{/path}, \directlua{...},
-- \openin, \pdfobj file{...} and similar primitives on the server. This filter:
--
--   * turns every Math element that uses a file, Lua, definition or catcode primitive (or the
--     ^^ character notation that can spell any of them) into literal code text;
--   * turns raw TeX/LaTeX into literal code text;
--   * keeps only images that the server resolved beforehand (local files listed in the image map
--     given as metadata `fairplay-image-map`); every other image becomes its alt text, so pandoc
--     never fetches a URL or reads an arbitrary path.
--
-- lualatex additionally runs with openin_any=p, openout_any=p and shell escape off.

local BLOCKED_SUBSTRINGS = {
  'input', 'include', 'lua', 'open', 'read', 'write', 'file', 'font', 'catcode', 'csname',
  'package', 'documentclass', 'special', 'immediate', 'scantokens', 'everyeof', 'command',
  'primitive', 'imageresource', 'boxresource', 'shipout', 'makeat', 'verbatim', 'listing',
  'graphics', 'jobname', 'directory', 'system', 'shell', 'escape', 'expandafter', 'pdf',
  'filecontents',
}

local BLOCKED_EXACT = {
  def = true, edef = true, gdef = true, xdef = true, let = true, futurelet = true,
  uccode = true, lccode = true, mathcode = true, uppercase = true, lowercase = true,
  global = true, afterassignment = true, aftergroup = true, everypar = true, everymath = true,
  everydisplay = true, everyhbox = true, everyvbox = true, everyjob = true, everycr = true,
  output = true, string = true, detokenize = true, unexpanded = true, noexpand = true,
  endinput = true, message = true, errmessage = true, newtoks = true, toks = true,
  begingroup = true, endgroup = true, char = true, chardef = true, mathchardef = true,
  countdef = true, dimendef = true, skipdef = true, toksdef = true, the = true, showtokens = true,
}

local function name_is_blocked(name)
  if name:find('@', 1, true) then
    return true
  end
  if BLOCKED_EXACT[name] then
    return true
  end
  local lower = name:lower()
  for _, word in ipairs(BLOCKED_SUBSTRINGS) do
    if lower:find(word, 1, true) then
      return true
    end
  end
  return false
end

local function tex_is_unsafe(text)
  if text == nil then
    return false
  end
  -- ^^ notation (^^69 is "i", ^^5c is a backslash) can spell any control sequence.
  if text:find('^^', 1, true) then
    return true
  end
  for name in text:gmatch('\\([%a@]+)') do
    if name_is_blocked(name) then
      return true
    end
  end
  -- Environment names (\begin{filecontents}, \begin{luacode}, ...).
  for env in text:gmatch('\\begin%s*{([^}]*)}') do
    if name_is_blocked((env:gsub('%s', ''))) then
      return true
    end
  end
  return false
end

local image_map = {}

local function load_image_map(meta)
  local entry = meta['fairplay-image-map']
  if entry == nil then
    return nil
  end
  local path = pandoc.utils.stringify(entry)
  local handle = io.open(path, 'r')
  if handle == nil then
    return nil
  end
  for line in handle:lines() do
    local src, local_path, mime = line:match('^([^\t]+)\t([^\t]+)\t([^\t]+)$')
    if src ~= nil then
      image_map[src] = { path = local_path, mime = mime }
    end
  end
  handle:close()
  return nil
end

local function math_filter(el)
  if tex_is_unsafe(el.text) then
    return pandoc.Code(el.text)
  end
  return nil
end

local function is_tex_format(format)
  local lower = (format or ''):lower()
  return lower == 'tex' or lower == 'latex' or lower == 'context' or lower == 'beamer'
end

local function raw_inline_filter(el)
  if is_tex_format(el.format) then
    return pandoc.Code(el.text)
  end
  return nil
end

local function raw_block_filter(el)
  if is_tex_format(el.format) then
    return pandoc.CodeBlock(el.text)
  end
  return nil
end

local inserted = {}

local function image_filter(el)
  local entry = image_map[el.src]
  if entry ~= nil then
    local name = entry.path:match('([^/]+)$')
    if not inserted[name] then
      local handle = io.open(entry.path, 'rb')
      if handle == nil then
        return pandoc.Span(el.caption)
      end
      local contents = handle:read('a')
      handle:close()
      pandoc.mediabag.insert(name, entry.mime, contents)
      inserted[name] = true
    end
    el.src = name
    return el
  end
  return pandoc.Span(el.caption)
end

return {
  { Meta = load_image_map },
  {
    Math = math_filter,
    RawInline = raw_inline_filter,
    RawBlock = raw_block_filter,
    Image = image_filter,
  },
}
