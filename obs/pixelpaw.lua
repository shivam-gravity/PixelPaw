--[[
  PixelPaw for OBS Studio (28+). No compiling, no installers.

  OBS → Tools → Scripts → "+" → pick this file.
  Then click "Add overlay to current scene". Start PixelPaw first:  python -m pixelpaw run

  The dashboard can also live inside OBS as a dock:
  View → Docks → Custom Browser Docks… → Name: PixelPaw, URL: the "Dashboard" link PixelPaw prints.
]]

obs = obslua

local SOURCE_NAME = "PixelPaw Overlay"
local url = "http://127.0.0.1:8765/overlay"

function script_description()
  return [[<h2>🐾 PixelPaw</h2>
<p>Live alerts on stream: Super Chats, new members and the bot's <code>!ask</code> answers.</p>
<p>1. Start PixelPaw: <code>python -m pixelpaw run</code><br>
2. Click <b>Add overlay to current scene</b>.<br>
3. Click <b>Reload overlay</b> after changing the language or position in <code>persona.yaml</code>.</p>
<p>Tip: add <code>?lang=hi&amp;pos=bottom-right&amp;scale=1.3</code> to the URL to change language, corner and size.</p>]]
end

function script_properties()
  local props = obs.obs_properties_create()
  obs.obs_properties_add_text(props, "url", "Overlay URL", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_button(props, "add", "Add overlay to current scene", on_add)
  obs.obs_properties_add_button(props, "reload", "Reload overlay", on_reload)
  return props
end

function script_defaults(settings)
  obs.obs_data_set_default_string(settings, "url", url)
end

function script_update(settings)
  url = obs.obs_data_get_string(settings, "url")
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
