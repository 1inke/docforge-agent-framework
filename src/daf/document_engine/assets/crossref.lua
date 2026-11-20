-- Minimal built-in Lua filter for {#fig:x}, {#tbl:x}, {#eq:x} numbering when pandoc-crossref is absent.
local fig_count = 0
local tbl_count = 0
local eq_count = 0
local labels = {}

local function register_label(id, prefix, count)
  if id and id ~= "" then
    labels[id] = prefix .. tostring(count)
  end
end

function Figure(el)
  if el.identifier and el.identifier:match("^fig:") then
    fig_count = fig_count + 1
    register_label(el.identifier, "图 ", fig_count)
    if el.caption and el.caption.long and #el.caption.long > 0 then
      local first = el.caption.long[1]
      if first.t == "Plain" or first.t == "Para" then
        table.insert(first.content, 1, pandoc.Str("图 " .. tostring(fig_count) .. " "))
      end
    end
  end
  return el
end

function Table(el)
  if el.identifier and el.identifier:match("^tbl:") then
    tbl_count = tbl_count + 1
    register_label(el.identifier, "表 ", tbl_count)
    if el.caption and el.caption.long and #el.caption.long > 0 then
      local first = el.caption.long[1]
      if first.t == "Plain" or first.t == "Para" then
        table.insert(first.content, 1, pandoc.Str("表 " .. tostring(tbl_count) .. " "))
      end
    end
  end
  return el
end

function Span(el)
  if el.identifier and el.identifier:match("^eq:") then
    eq_count = eq_count + 1
    register_label(el.identifier, "式 (", eq_count)
    labels[el.identifier] = "式 (" .. tostring(eq_count) .. ")"
    table.insert(el.content, pandoc.Str(" (" .. tostring(eq_count) .. ")"))
  end
  return el
end

function Cite(el)
  if #el.citations == 1 then
    local cid = el.citations[1].id
    if labels[cid] then
      return pandoc.Str(labels[cid])
    end
  end
  return el
end
