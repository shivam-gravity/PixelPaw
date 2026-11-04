--[[
  PixelPaw for OBS Studio (28+). No compiling, no installers, no terminal.

  OBS → Tools → Scripts → "+" → pick this file. Then:
    ☑ Start PixelPaw when OBS starts   (PixelPaw runs in the background while you stream)
    [Add overlay to current scene]      (on-stream alerts)
    [Open PixelPaw]                     (setup + dashboard in your browser)

  The dashboard can also live inside OBS: View → Docks → Custom Browser Docks… → URL http://127.0.0.1:8765/
]]

obs = obslua

local SOURCE_NAME = "PixelPaw Overlay"
local url = "http://127.0.0.1:8765/overlay"
local autostart = true
local folder = ""
local is_windows = package.config:sub(1, 1) == "\\"

local function default_folder()
  local dir = script_path and script_path() or ""
  dir = dir:gsub("[/\\]+$", "")
  local parent = dir:match("^(.*)[/\\][^/\\]+$") or dir  -- this script lives in <PixelPaw>/obs/
  if is_windows then parent = parent:gsub("/", "\\") end
  return parent
end

local function file_exists(path)
  local f = io.open(path, "r")
  if f then f:close() return true end
  return false
end

local function launch(open_dashboard)
  if folder == "" then folder = default_folder() end
  local sep = is_windows and "\\" or "/"
  local pyw = folder .. sep .. "PixelPaw.pyw"
  if not file_exists(pyw) then
    obs.script_log(obs.LOG_WARNING, "PixelPaw: PixelPaw.pyw not found in " .. folder .. " (set the folder in the script settings)")
    return
  end
  local flag = open_dashboard and "" or " --no-browser"
  local cmd
  if is_windows then
    local pythonw = folder .. "\\.venv\\Scripts\\pythonw.exe"
    if file_exists(pythonw) then
      cmd = 'start "" /D "' .. folder .. '" "' .. pythonw .. '" "' .. pyw .. '"' .. flag
    else  -- first run: the installer window appears once
      cmd = 'start "" /D "' .. folder .. '" "' .. folder .. '\\PixelPaw.cmd"' .. flag
    end
  else
    cmd = 'cd "' .. folder .. '" && sh ./PixelPaw.command' .. flag .. ' >/dev/null 2>&1 &'
  end
  os.execute(cmd)  -- PixelPaw is single-instance: launching it again only opens the dashboard
  obs.script_log(obs.LOG_INFO, "PixelPaw: started" .. (open_dashboard and " (opening dashboard)" or ""))
end

function script_description()
  return [[<h2>🐾 PixelPaw</h2>
<p>Your YouTube bot inside OBS: on-stream alerts for Super Chats, new members and the bot's <code>!ask</code> answers.</p>
<p><b>1.</b> Keep <i>Start PixelPaw when OBS starts</i> ticked.<br>
<b>2.</b> Click <b>Add overlay to current scene</b>.<br>
<b>3.</b> Click <b>Open PixelPaw</b> to finish setup or see the dashboard.</p>]]
end

function script_properties()
  local props = obs.obs_properties_create()
  obs.obs_properties_add_bool(props, "autostart", "Start PixelPaw when OBS starts")
  obs.obs_properties_add_button(props, "open", "Open PixelPaw", on_open)
  obs.obs_properties_add_button(props, "add", "Add overlay to current scene", on_add)
  obs.obs_properties_add_button(props, "reload", "Reload overlay", on_reload)
  obs.obs_properties_add_text(props, "url", "Overlay URL", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_path(props, "folder", "PixelPaw folder", obs.OBS_PATH_DIRECTORY, nil, nil)
  return props
end

function script_defaults(settings)
  obs.obs_data_set_default_string(settings, "url", url)
  obs.obs_data_set_default_bool(settings, "autostart", true)
  obs.obs_data_set_default_string(settings, "folder", default_folder())
end

function script_update(settings)
  url = obs.obs_data_get_string(settings, "url")
  autostart = obs.obs_data_get_bool(settings, "autostart")
  folder = obs.obs_data_get_string(settings, "folder")
end

function script_load(settings)
  script_update(settings)
  if autostart then launch(false) end
end

local function canvas_size()
  local ovi = obs.obs_video_info()
  if obs.obs_get_video_info(ovi) then
    return ovi.base_width, ovi.base_height
  end
  return 1920, 1080
end

local function browser_settings()
  local w, h = canvas_size()
  local s = obs.obs_data_create()
  obs.obs_data_set_bool(s, "is_local_file", false)  -- the overlay is a URL, never a local file
  obs.obs_data_set_string(s, "url", url)
  obs.obs_data_set_int(s, "width", w)
  obs.obs_data_set_int(s, "height", h)
  obs.obs_data_set_int(s, "fps", 30)
  obs.obs_data_set_bool(s, "shutdown", false)
  obs.obs_data_set_bool(s, "restart_when_active", false)
  obs.obs_data_set_string(s, "css", "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }")
  return s
end

function on_open(props, prop)
  launch(true)
  return false
end

function on_add(props, prop)
  local scene_source = obs.obs_frontend_get_current_scene()
  if scene_source == nil then
    obs.script_log(obs.LOG_WARNING, "PixelPaw: no current scene")
    return false
  end
  local scene = obs.obs_scene_from_source(scene_source)
  local settings = browser_settings()

  local source = obs.obs_get_source_by_name(SOURCE_NAME)
  if source == nil then
    source = obs.obs_source_create("browser_source", SOURCE_NAME, settings, nil)
  else
    obs.obs_source_update(source, settings)
  end

  if source ~= nil then
    if obs.obs_scene_find_source(scene, SOURCE_NAME) == nil then
      obs.obs_scene_add(scene, source)
    end
    obs.script_log(obs.LOG_INFO, "PixelPaw: overlay added (" .. url .. ")")
    obs.obs_source_release(source)
  else
    obs.script_log(obs.LOG_WARNING, "PixelPaw: could not create a Browser Source (is the obs-browser plugin installed?)")
  end

  obs.obs_data_release(settings)
  obs.obs_source_release(scene_source)
  return false
end

function on_reload(props, prop)
  local source = obs.obs_get_source_by_name(SOURCE_NAME)
  if source == nil then
    obs.script_log(obs.LOG_WARNING, "PixelPaw: add the overlay first")
    return false
  end
  local settings = browser_settings()
  obs.obs_source_update(source, settings)
  obs.obs_data_release(settings)

  local sprops = obs.obs_source_properties(source)
  local refresh = obs.obs_properties_get(sprops, "refreshnocache")
  if refresh ~= nil then
    obs.obs_property_button_clicked(refresh, source)
  end
  obs.obs_properties_destroy(sprops)
  obs.obs_source_release(source)
  return false
end
