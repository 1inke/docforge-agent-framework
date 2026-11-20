-- Two-pass Pandoc Lua filter for cross-references, [@hypothesis] removal, and source-level citation rewriting.
local fig_count = 0
local tbl_count = 0
local eq_count = 0

local labels = {}
local registered_tbl = {}
local registered_fig = {}
local registered_eq = {}
local evidence_map = {}

local function is_crossref_id(cid)
  return cid:match("^fig:") or cid:match("^tbl:") or cid:match("^eq:") or cid:match("^sec:")
end

local function is_page_locator(loc)
  if not loc or loc == "" then
    return false
  end
  -- Normalize UTF-8 NBSP (\xc2\xa0) inserted by Pandoc YAML parsing after "p."
  local clean = loc:gsub("\194\160", " ")
  local low = clean:lower()
  if low:match("^p%.?%s*%d") or low:match("^pp%.?%s*%d") or low:match("^page%s*%d") or clean:match("^第%s*%d+.*页") then
    return clean
  end
  return nil
end

local function prepend_caption_prefix(caption, prefix)
  if not caption or not caption.long or #caption.long == 0 then
    return
  end
  local first = caption.long[1]
  if first and (first.t == "Plain" or first.t == "Para") then
    table.insert(first.content, 1, pandoc.Str(prefix))
  end
end

local function register_table_caption(tbl, id)
  if registered_tbl[id] then
    return
  end
  tbl_count = tbl_count + 1
  registered_tbl[id] = true
  labels[id] = "表 " .. tostring(tbl_count)
  prepend_caption_prefix(tbl.caption, "表 " .. tostring(tbl_count) .. " ")
  if tbl.identifier == id then
    tbl.identifier = ""
  end
end

local function register_figure_caption(fig, id)
  if registered_fig[id] then
    return
  end
  fig_count = fig_count + 1
  registered_fig[id] = true
  labels[id] = "图 " .. tostring(fig_count)
  prepend_caption_prefix(fig.caption, "图 " .. tostring(fig_count) .. " ")
  if fig.identifier == id then
    fig.identifier = ""
  end
end

-- Pass 1: Read metadata map & register all labels/captions across the whole document
local pass1 = {
  Meta = function(meta)
    local raw_map = meta["daf-evidence-map"]
    if type(raw_map) == "table" then
      for ev_id, info in pairs(raw_map) do
        if type(info) == "table" then
          local src = info.source and pandoc.utils.stringify(info.source) or ""
          local loc = info.locator and pandoc.utils.stringify(info.locator) or ""
          if src ~= "" then
            evidence_map[tostring(ev_id)] = { source = src, locator = loc }
          end
        end
      end
    end
  end,

  Div = function(el)
    if el.identifier and el.identifier:match("^tbl:") then
      local found_table = false
      for _, blk in ipairs(el.content) do
        if blk.t == "Table" then
          register_table_caption(blk, el.identifier)
          found_table = true
          break
        end
      end
      if not found_table and not registered_tbl[el.identifier] then
        tbl_count = tbl_count + 1
        registered_tbl[el.identifier] = true
        labels[el.identifier] = "表 " .. tostring(tbl_count)
      end
      return el.content
    end
    if el.identifier and el.identifier:match("^fig:") then
      for _, blk in ipairs(el.content) do
        if blk.t == "Figure" then
          register_figure_caption(blk, el.identifier)
          break
        end
      end
      return el.content
    end
    return el
  end,

  Table = function(el)
    if el.identifier and el.identifier:match("^tbl:") then
      register_table_caption(el, el.identifier)
    end
    return el
  end,

  Figure = function(el)
    if el.identifier and el.identifier:match("^fig:") then
      register_figure_caption(el, el.identifier)
      -- Clear matching identifier on inner Image so Pass 1 Image doesn't double-count
      el = el:walk({
        Image = function(img)
          if img.identifier == "" or img.identifier:match("^fig:") then
            img.identifier = ""
          end
          return img
        end
      })
    end
    return el
  end,

  Image = function(el)
    if el.identifier and el.identifier:match("^fig:") and not registered_fig[el.identifier] then
      fig_count = fig_count + 1
      registered_fig[el.identifier] = true
      labels[el.identifier] = "图 " .. tostring(fig_count)
      el.identifier = ""
    end
    return el
  end,

  Span = function(el)
    if el.identifier and el.identifier:match("^eq:") and not registered_eq[el.identifier] then
      eq_count = eq_count + 1
      registered_eq[el.identifier] = true
      labels[el.identifier] = "式 (" .. tostring(eq_count) .. ")"
      local out = pandoc.List()
      for _, item in ipairs(el.content) do
        out:insert(item)
      end
      out:insert(pandoc.Str(" (" .. tostring(eq_count) .. ")"))
      return out
    end
    return el
  end,

  Header = function(el)
    if el.identifier and el.identifier:match("^sec:") then
      labels[el.identifier] = pandoc.utils.stringify(el.content)
    end
    return el
  end,
}

local function prev_non_space_text(out_list)
  for idx = #out_list, 1, -1 do
    local item = out_list[idx]
    if item.t ~= "Space" and item.t ~= "SoftBreak" then
      if item.t == "Str" then
        return item.text
      end
      return ""
    end
  end
  return ""
end

local function ends_with_utf8(s, suffix)
  return #s >= #suffix and s:sub(-#suffix) == suffix
end

-- Pass 2: Strip [@hypothesis], resolve crossrefs (forward & backward), rewrite @ev_* -> source_id
local pass2 = {
  Inlines = function(inlines)
    local out = pandoc.List()
    for i, el in ipairs(inlines) do
      if el.t == "Cite" then
        local crossrefs = {}
        local new_cites = pandoc.List()
        local seen_sources = {}

        for _, cit in ipairs(el.citations) do
          local cid = cit.id
          if cid == "hypothesis" then
            -- Strip hypothesis tag completely
          elseif is_crossref_id(cid) then
            local label_text = labels[cid] or cid
            table.insert(crossrefs, label_text)
          else
            local mapped = evidence_map[cid]
            local target_id = mapped and mapped.source or cid
            local loc = mapped and mapped.locator or ""
            if not seen_sources[target_id] then
              seen_sources[target_id] = true
              local suffix = cit.suffix
              local page_loc = is_page_locator(loc)
              if page_loc and (#suffix == 0) then
                suffix = pandoc.Inlines({ pandoc.Str(", "), pandoc.Str(page_loc) })
              end
              new_cites:insert(
                pandoc.Citation(target_id, cit.mode, cit.prefix, suffix, cit.note_num, cit.hash)
              )
            end
          end
        end

        -- Emit resolved crossrefs
        if #crossrefs > 0 then
          local joined = table.concat(crossrefs, ", ")
          local prev_txt = prev_non_space_text(out)
          if ends_with_utf8(prev_txt, "表") and joined:sub(1, #"表 ") == "表 " then
            joined = joined:sub(#"表 " + 1)
          elseif ends_with_utf8(prev_txt, "图") and joined:sub(1, #"图 ") == "图 " then
            joined = joined:sub(#"图 " + 1)
          elseif ends_with_utf8(prev_txt, "式") and joined:sub(1, #"式 ") == "式 " then
            joined = joined:sub(#"式 " + 1)
          end
          out:insert(pandoc.Str(joined))
        end

        -- Emit deduplicated evidence citations
        if #new_cites > 0 then
          el.citations = new_cites
          out:insert(el)
        elseif #crossrefs == 0 then
          -- Cite had ONLY hypothesis; drop preceding space if followed by punctuation
          local next_el = inlines[i + 1]
          if #out > 0 and out[#out].t == "Space" and next_el and next_el.t == "Str" and next_el.text:match("^[%.,;。！？]") then
            out:remove(#out)
          end
        end
      else
        out:insert(el)
      end
    end
    return out
  end,
}

return { pass1, pass2 }
